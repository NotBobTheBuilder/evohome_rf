"""The entities for Honeywell's RAMSES II / Residential Network Protocol."""
# import asyncio
from datetime import datetime as dt, timedelta
import json
import logging
from typing import Any, Optional

from .command import (
    Command,
    PAUSE_DEFAULT,
    PRIORITY_DEFAULT,
    PRIORITY_HIGH,
    PRIORITY_LOW,
)
from .const import (
    # CODE_SCHEMA,
    DEVICE_CLASSES,
    DEVICE_LOOKUP,
    DEVICE_TABLE,
    DEVICE_TYPES,
    SYSTEM_MODE_LOOKUP,
    SYSTEM_MODE_MAP,
    __dev_mode__,
)

_LOGGER = logging.getLogger(__name__)
if True or __dev_mode__:
    _LOGGER.setLevel(logging.DEBUG)
else:
    _LOGGER.setLevel(logging.WARNING)


def dev_hex_to_id(device_hex: str, friendly_id=False) -> str:
    """Convert (say) '06368E' to '01:145038' (or 'CTL:145038')."""
    if device_hex == "FFFFFE":  # aka '63:262142'
        return ">null dev<" if friendly_id else "63:262142"
    if not device_hex.strip():  # aka '--:------'
        return f"{'':10}" if friendly_id else "--:------"
    _tmp = int(device_hex, 16)
    dev_type = f"{(_tmp & 0xFC0000) >> 18:02d}"
    if friendly_id:
        dev_type = DEVICE_TYPES.get(dev_type, f"{dev_type:<3}")
    return f"{dev_type}:{_tmp & 0x03FFFF:06d}"


def dev_id_to_hex(device_id: str) -> str:
    """Convert (say) '01:145038' (or 'CTL:145038') to '06368E'."""
    if len(device_id) == 9:  # e.g. '01:123456'
        dev_type = device_id[:2]
    else:  # len(device_id) == 10, e.g. 'CTL:123456', or ' 63:262142'
        dev_type = DEVICE_LOOKUP.get(device_id[:3], device_id[1:3])
    return f"{(int(dev_type) << 18) + int(device_id[-6:]):0>6X}"  # sans preceding 0x


def _dtm(value) -> str:
    def dtm_to_hex(tm_year, tm_mon, tm_mday, tm_hour, tm_min, tm_sec, *args):
        return f"{tm_min:02X}{tm_hour:02X}{tm_mday:02X}{tm_mon:02X}{tm_year:04X}"

    if value is None:
        return "FF" * 6

    if isinstance(value, str):
        try:
            value = dt.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid datetime isoformat string")
    elif not isinstance(value, dt):
        raise TypeError("Invalid datetime object")

    if value < dt.now() + timedelta(minutes=1):
        raise ValueError("Invalid datetime")

    return dtm_to_hex(*value.timetuple())


class Entity:
    """The Device/Domain/Zone base class."""

    def __init__(self, gateway, entity_id, controller=None) -> None:
        self._gwy = gateway
        self._evo = gateway.evo
        self._que = gateway.cmd_que
        self.id = entity_id
        self._controller = controller

        self._pkts = {}
        self.last_comms = None

    def __repr__(self) -> str:
        """Return a JSON dict of all the public atrributes of an entity."""

        result = {
            a: getattr(self, a)
            for a in dir(self)
            if not a.startswith("_") and not callable(getattr(self, a))
        }
        return json.dumps(result)

    def __str__(self) -> str:
        """Return the id of an entity."""

        return json.dumps({"entity_id": self.id})

    @property
    def controller(self) -> Optional[str]:
        """Return the id of the entity's controller, if known.

        If the controller is not known, try to find it.
        """
        if self._controller is not None:
            return self._controller.id

        for msg in self._pkts.values():
            if msg.dst.type == "01":  # msg.dst.is_controller
                self.controller = msg.dst  # useful for UFH
            # elif msg.src.type == "01":  # msg.src.is_controller
            #     self.controller = msg.src  # useful for TPI, not useful for OTB

        return self._controller.id if self._controller else None

    @controller.setter
    def controller(self, controller) -> None:
        """Set the entity's controller.

        It is assumed that, once set, it never changes.
        """
        assert type(controller) is not str, type(controller)  # TODO: remove

        if controller is None:
            raise ValueError
        elif controller is not None:
            assert type(controller) is Controller, f"{controller}"

        if self._controller is None:
            self._controller = controller

            if isinstance(self, DeviceBase):
                if self.id not in self._evo.device_by_id:
                    self._evo.devices.append(self)
                    self._evo.device_by_id[self.id] = self
            # else:
            #     self._evo.domains.append(self)
            #     self._evo.domain_by_id[self.id] = self
            #     if self.name is not None:
            #         self._controller.domain_by_name[self.name] = self

        elif self._controller != controller:
            raise ValueError

    def _command(self, code, **kwargs) -> None:
        temp = self._evo.ctl.id if self._evo and self._evo.ctl else None
        dest = kwargs.get("dest_addr", temp)
        assert dest is not None, "THIS NEEDS SORTING"  # HACK: a hack

        verb = kwargs.get("verb", "RQ")
        payload = kwargs.get("payload", "00")

        priority_default = PRIORITY_HIGH if verb == " W" else PRIORITY_DEFAULT
        kwargs = {
            "pause": kwargs.get("pause", PAUSE_DEFAULT),
            "priority": kwargs.get("priority", priority_default),
        }

        self._que.put_nowait(Command(verb, dest, code, payload, **kwargs))

    def _discover(self):
        # pass
        raise NotImplementedError

    def _get_pkt_value(self, code, key=None) -> Optional[Any]:
        if self._pkts.get(code):
            if isinstance(self._pkts[code].payload, list):
                return self._pkts[code].payload

            if key is not None:
                return self._pkts[code].payload.get(key)

            result = self._pkts[code].payload
            return {k: v for k, v in result.items() if k[:1] != "_"}

    def update(self, msg) -> None:
        _ = self.controller

        self.last_comms = f"{msg.date}T{msg.time}"
        if msg.verb == " W":
            if msg.code in self._pkts and self._pkts[msg.code].verb != msg.verb:
                return
        if msg.verb == "RQ":  # and msg.payload:
            if msg.code in self._pkts and self._pkts[msg.code].verb != msg.verb:
                return
        # may get an RQ/W initially, but RP/I will override
        self._pkts.update({msg.code: msg})

    @property
    def pkt_codes(self) -> list:
        return list(self._pkts.keys())


class Actuator:  # 3EF0, 3EF1
    """Some devices have a actuator."""

    @property
    def actuator_enabled(self) -> Optional[bool]:  # 3EF0, TODO: does 10: RP/3EF1?
        return self._get_pkt_value("3EF0", "actuator_enabled")

    @property
    def actuator_state(self) -> Optional[float]:  # 3EF1, TODO: not all actuators
        return self._get_pkt_value("3EF1")


class BatteryState:  # 1060
    """Some devices have a battery."""

    @property
    def battery_state(self):
        low_battery = self._get_pkt_value("1060", "low_battery")
        if low_battery is not None:
            battery_level = self._get_pkt_value("1060", "battery_level")
            return {"low_battery": low_battery, "battery_level": battery_level}


class HeatDemand:  # 3150
    """Some devices have heat demand."""

    @property
    def heat_demand(self) -> Optional[float]:  # 3150
        return self._get_pkt_value("3150", "heat_demand")


class Setpoint:  # 2309
    """Some devices have a setpoint."""

    @property
    def setpoint(self) -> Optional[Any]:  # 2309
        return self._get_pkt_value("2309", "setpoint")


class Temperature:  # 30C9
    """Some devices have a temperature sensor."""

    @property
    def temperature(self) -> Optional[float]:  # 30C9
        return self._get_pkt_value("30C9", "temperature")


# ######################################################################################


class DeviceBase(Entity):
    """The Device base class."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating a Device, %s", address.id)
        super().__init__(gateway, address.id)

        assert address.id not in gateway.device_by_id, address.id

        gateway.devices.append(self)
        gateway.device_by_id[address.id] = self

        self.addr = address
        self.type = address.type

        self.cls_type = DEVICE_TYPES.get(self.addr.type)
        self.cls_name = DEVICE_CLASSES.get(self.cls_type)

        self.hex_id = dev_id_to_hex(address.id)

        if self.addr.type in DEVICE_TABLE:
            self._has_battery = DEVICE_TABLE[self.addr.type].get("has_battery")
            self._is_actuator = DEVICE_TABLE[self.addr.type].get("is_actuator")
            self._is_sensor = DEVICE_TABLE[self.addr.type].get("is_sensor")
        else:
            self._has_battery = None
            self._is_actuator = None
            self._is_sensor = None

        self._zone = None  # parent zone object

        attrs = gateway.known_devices.get(address.id)
        self._friendly_name = attrs.get("friendly_name") if attrs else None
        self._ignored = attrs.get("ignored", False) if attrs else False

        self._discover()

    # def __str__():
    #     return self._friendly_name

    @property
    def parent_zone(self) -> Optional[str]:  # TODO: dev-only, remove at some stage
        """Return the id of the device's parent zone using parent_idx."""

        zone_id = None
        for msg in self._pkts.values():
            # assert "zone_idx" not in msg.payload, str(msg)
            if "parent_idx" in msg.payload:
                zone_id = msg.payload["parent_idx"]
                break

        if zone_id is not None and self._zone is not None:
            assert zone_id == self._zone.id
        return zone_id

    @property
    def parent_000c(self) -> Optional[str]:  # TODO: dev-only, remove at some stage
        """Return the id of the device's parent zone using 000C."""

        if not self._is_actuator:
            return None

        zone_id = None
        for z in self._evo.zones:
            value = z._get_pkt_value("000C", "actuators")
            if value and self.id in value:
                zone_id = z.id
            break

        if zone_id is not None and self._zone is not None:
            assert zone_id == self._zone.id
        return zone_id

    @property
    def zone(self) -> Optional[str]:
        """Return the id of the device's zone, if known.

        If the zone is not known, try to find it.
        """
        if self._zone is not None:
            return self._zone.id

        # try to determine the 'parent' domain/zone...
        zone_id = None
        if self.parent_000c is not None:
            zone_id = self.parent_000c
        elif self.parent_zone is not None:
            zone_id = self.parent_zone
        else:
            return

        self._zone = self._evo.zone_by_id.get(zone_id)
        return self._zone.id if self._zone else None

    @zone.setter
    def zone(self, zone: Entity) -> None:
        """Set the device's zone.

        It is assumed that, once set, it never changes.
        """
        if self._zone is zone:
            return

        if zone == "FC" and self.addr.type == "13":
            self._zone = None
            return

        if not isinstance(zone, Entity):
            raise ValueError(f"zone is not an Entity", type(zone))
        if self._zone is not None and self._zone != zone:
            raise ValueError
        if self.parent_000c is not None and self.parent_000c != zone.id:
            raise ValueError
        if self.parent_zone is not None and self.parent_zone != zone.id:
            raise ValueError

        self._zone = zone
        # self._zone.devices.append(self)
        # self._zone.device_by_id[self.id] == self

    def _discover(self):
        # do these even if battery-powered (e.g. device might be in rf_check mode)
        for code in ("1FC9",):
            self._command(code, dest_addr=self.id)
        for code in ("0016",):
            self._command(code, dest_addr=self.id, payload="0000")

        if self.has_battery is not True:
            self._command("10E0", dest_addr=self.id)

        # if self.addr.type not in ("01", "13") and not self.has_battery:  # TODO: dev
        #     for code in CODE_SCHEMA:
        #         if code == "0404":
        #             continue
        #         self._command(
        #             code, dest_addr=self.id, payload="0000" if code != "1F09" else "00"  # noqa
        #         )

    @property
    def description(self) -> Optional[str]:  # 10E0
        return self._get_pkt_value("10E0", "description")

    @property
    def has_battery(self) -> Optional[bool]:  # 1060
        """Return True if a device is battery powered.

        Devices with a battery-backup may still be mains-powered.
        """
        if self._has_battery is not None:
            return self._has_battery

        if "1060" in self._pkts:
            self._has_battery = True
        return self._has_battery

    @property
    def is_controller(self) -> Optional[bool]:  # 1F09
        if self.addr.type in ("01", "23"):
            return True
        elif "1F09" in self._pkts:
            return self._pkts["1F09"].verb == " I"
        return False

    @property
    def pkt_1fc9(self) -> list:  # TODO: make private
        return self._get_pkt_value("1FC9")  # we want the RPs

    @property
    def rf_signal(self) -> Optional[dict]:  # TODO: make 'current', else add dtm?
        return self._get_pkt_value("0016")


# 18:
class Gateway(DeviceBase):
    """The Gateway class for a HGI80."""


# ??: used for unknown device types
class Device(DeviceBase, BatteryState):
    """The Device class."""


# 01:
class Controller(DeviceBase):
    """The Controller class."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating the Controller, %s", address.id)
        super().__init__(gateway, address)

        self._evo.ctl = self
        self._controller = self

        self._boiler_relay = None
        self._fault_log = {}
        self._prev_30c9 = None

    def _discover(self):
        super()._discover()

        # asyncio.create_task(  # TODO: test only
        #     self.async_set_mode(5, dt.now() + timedelta(minutes=120))
        #     # self.async_set_mode(5)
        #     # self.async_reset_mode()
        # )

        # NOTE: could use this to discover zones
        # for idx in range(12):
        #     self._command("0004", payload=f"{idx:02x}00")

        # system-related... (not working: 1280, 22D9, 2D49, 2E04, 3220, 3B00)
        self._command("1F09", payload="00")
        for code in ("313F", "0100", "0002"):
            self._command(code)

        for code in ("10A0", "1260", "1F41"):  # stored DHW
            self._command(code)

        self._command("0005", payload="0000")
        self._command("1100", payload="FC")
        self._command("2E04", payload="FF")

        # Get the three most recent fault log entries
        for log_idx in range(0, 0x3):  # max is 0x3C?
            self._command("0418", payload=f"{log_idx:06X}", priority=PRIORITY_LOW)

        # TODO: 1100(), 1290(00x), 0418(00x):
        # for code in ("000C"):
        #     for payload in ("F800", "F900", "FA00", "FB00", "FC00", "FF00"):
        #         self._command(code, payload=payload)

        # for code in ("3B00"):
        #     for payload in ("0000", "00", "F8", "F9", "FA", "FB", "FC", "FF"):
        #         self._command(code, payload=payload)

    def update(self, msg):
        def maintain_state_data():
            pass

        def update_zone_sensors() -> None:
            prev_msg, self._prev_30c9 = self._prev_30c9, msg
            if prev_msg is None:
                return

            # _LOGGER.debug(
            #     "System zones: %s", {z.id: z.temperature for z in self._evo.zones}
            # )
            # _LOGGER.debug(
            #     " - sensorless zones: %s",
            #     {z.id: z.temperature for z in self._evo.zones if z.sensor is None}
            # )

            old, new = prev_msg.payload, msg.payload
            zones = [self._evo.zone_by_id[z["zone_idx"]] for z in new if z not in old]
            # _LOGGER.debug(" - changed zones: %s", {z.id: z.temperature for z in zones})  # noqa
            if not zones:
                return  # no zones have changed their temp since the last cycle

            test_zones = [
                z
                for z in zones
                if z.sensor is None
                and z.temperature not in [x for x in zones if x != z] + [None]
            ]
            # _LOGGER.debug(
            #     "   - testable zones: %s", {z.id: z.temperature for z in test_zones}
            # )
            if not test_zones:
                return  # no changed zones have unique, non-null temps

            evo_sensors = [
                d
                for d in self._evo.devices
                if hasattr(d, "temperature")
                and d.temperature is not None
                and d.addr.type != "07"
            ]
            _LOGGER.debug(
                "System sensors: %s", {d.id: d.temperature for d in evo_sensors}
            )

            gwy_sensors = [
                d
                for d in self._gwy.devices
                if hasattr(d, "temperature")
                and d.temperature is not None
                and d.addr.type != "07"
                and d.zone is None
                and d not in [x for x in evo_sensors]
            ]
            _LOGGER.debug(
                " - orphan sensors: %s (those without a parent zone)",
                {d.id: d.temperature for d in gwy_sensors},
            )

            test_sensors = [
                d
                for d in evo_sensors + gwy_sensors
                if d._pkts["30C9"].dtm > prev_msg.dtm
            ]  # if have also changed their temp since the last cycle

            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Testable zones: %s (have changed and are sensorless)",
                    {z.idx: z.temperature for z in test_zones},
                )
                _LOGGER.debug(
                    " - testable sensors: %s (either orphans or from this system)",
                    {d.id: d.temperature for d in test_sensors},
                )

            for z in test_zones:
                sensors = [
                    d
                    for d in test_sensors
                    if d.temperature == z.temperature and d._zone in (z, None)
                ]
                _LOGGER.debug("Testing zone %s, temp: %s", z.idx, z.temperature)
                _LOGGER.debug(
                    " - possible sensors: %s (with same temp & not from another zone)",
                    {d.id: d.temperature for d in sensors},
                )

                if len(sensors) == 1:
                    _LOGGER.debug("   - matched sensor: %s", sensors[0].id)
                    z._sensor, sensors[0].zone = sensors[0].id, z
                    sensors[0].controller = self
                elif len(sensors) == 0:
                    _LOGGER.debug("   - no matching sensor (uses CTL?)")
                else:
                    _LOGGER.debug("   - multiple sensors: %s", sensors)

            # now see if we can allocate the controller as a sensor...
            zones = [z for z in self._evo.zones if z.sensor is None]
            if len(zones) != 1:
                return  # no single zone without a sensor

            _LOGGER.debug(
                "TESTING zone %s, temp: %s", zones[0].idx, zones[0].temperature
            )

            # TODO: this can't be used if their neighbouring sensors not ignored
            # if [d for d in evo_sensors if d.zone is None]:
            #     return  # >0 sensors without a zone

            # can safely(?) assume this zone is using the CTL as a sensor...
            assert self.zone is None, "Controller has already been allocated!"

            sensors = [d for d in evo_sensors if d.zone is None] + [self.id]
            _LOGGER.debug(
                " - zoneless sensors: %s (from this system, incl. controller)", sensors
            )
            if len(sensors) != 1:
                return
            _LOGGER.debug("   - sensor is CTL by exclusion: %s", self.id)
            zones[0]._sensor, self.zone = self.id, zones[0]

        if msg.code in ("000A", "2309", "30C9") and not isinstance(msg.payload, list):
            pass
        else:
            super().update(msg)

        if msg.code == "0418" and msg.verb in (" I", "RP"):  # this is a special case
            self._fault_log[msg.payload["log_idx"]] = msg

        if msg.code == "1F09" and msg.verb == " I":
            maintain_state_data()

        if msg.code == "30C9" and isinstance(msg.payload, list):  # msg.is_array:
            update_zone_sensors()

        if msg.code == "3EF1" and msg.verb == "RQ":  # relay attached to a burner
            if msg.dst.type == "13":  # this is the TPI relay
                pass
            if msg.dst.type == "10":  # this is the OTB
                pass

    async def async_reset_mode(self) -> bool:  # 2E04
        """Revert the system mode to Auto mode."""
        self._command("2E04", verb=" W", payload="00FFFFFFFFFFFF00")
        return False

    async def async_set_mode(self, mode, until=None) -> bool:  # 2E04
        """Set the system mode for a specified duration, or indefinitely."""

        if isinstance(mode, int):
            mode = f"{mode:02X}"
        elif not isinstance(mode, str):
            raise TypeError("Invalid system mode")
        elif mode in SYSTEM_MODE_LOOKUP:
            mode = SYSTEM_MODE_LOOKUP[mode]

        if mode not in SYSTEM_MODE_MAP:
            raise ValueError("Unknown system mode")

        until = _dtm(until) + "00" if until is None else "01"

        self._command("2E04", verb=" W", payload=f"{mode}{until}")
        return False

    async def update_fault_log(self) -> list:
        # WIP: try to discover fault codes
        for log_idx in range(0x00, 0x3C):  # 10 pages of 6
            self._command("0418", payload=f"{log_idx:06X}")
        return None

    @property
    def fault_log(self):  # 0418
        return [f.payload for f in self._fault_log.values()]

    @property
    def language(self) -> Optional[str]:  # 0100,
        return self._get_pkt_value("0100", "language")

    @property
    def system_mode(self):  # 2E04
        attrs = ["mode", "until"]
        return {x: self._get_pkt_value("2E04", x) for x in attrs}

    @property
    def dhw_sensor(self) -> Optional[str]:
        """Return the id of the DHW sensor (07:) for *this* system/CTL.

        There is only 1 way to find a controller's DHW sensor:
        1.  The 10A0 RQ/RP *from/to a 07:* (1x/4h)

        The RQ is initiated by the DHW, so is not authorative (the CTL will RP any RQ).
        The I/1260 is not to/from a controller, so is not useful.
        """

        # 07:38:39.124 047 RQ --- 07:030741 01:102458 --:------ 10A0 006 00181F0003E4
        # 07:38:39.140 062 RP --- 01:102458 07:030741 --:------ 10A0 006 0018380003E8

        if "10A0" in self._pkts:
            return self._pkts["10A0"].dst.addr


# 02: "10E0", "3150";; "0008", "22C9", "22D0"
class UfhController(DeviceBase, HeatDemand):
    """The UFH class, the HCE80 that controls the UFH heating zones."""

    # 12:27:24.398 067  I --- 02:000921 --:------ 01:191718 3150 002 0360
    # 12:27:24.546 068  I --- 02:000921 --:------ 01:191718 3150 002 065A
    # 12:27:24.693 067  I --- 02:000921 --:------ 01:191718 3150 002 045C
    # 12:27:24.824 059  I --- 01:191718 --:------ 01:191718 3150 002 FC5C
    # 12:27:24.857 067  I --- 02:000921 --:------ 02:000921 3150 006 0060015A025C

    def update(self, msg):
        def do_3150_magic() -> None:
            return

        super().update(msg)
        return

        # "0008|FA/FC", "22C9|array", "22D0|none", "3150|ZZ/array(/FC?)"

        if msg.code in ("22C9") and not isinstance(msg.payload, list):
            pass
        else:
            super().update(msg)

        if msg.code == "3150" and isinstance(msg.payload, list):  # msg.is_array:
            do_3150_magic()

    @property
    def zones(self):  # 22C9
        return self._get_pkt_value("22C9")


# 07: "1060";; "1260" "10A0"
class DhwSensor(Device):
    """The DHW class, such as a CS92."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating a DHW sensor, %s", address.id)
        super().__init__(gateway, address)

        # self._discover()

    def update(self, msg):
        super().update(msg)

        # if msg.code == "10A0":
        #     return self._pkts["10A0"].dst.addr

    @property
    def temperature(self):
        return self._get_pkt_value("1260", "temperature")


# 10: "10E0", "3EF0", "3150";; "22D9", "3220" ("1FD4"), TODO: 3220
class OtbGateway(DeviceBase, Actuator, HeatDemand):
    """The OTB class, specifically an OpenTherm Bridge (R8810A Bridge)."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating an OTB gateway, %s", address.id)
        super().__init__(gateway, address)

    @property
    def boiler_setpoint(self) -> Optional[Any]:  # 22D9
        return self._get_pkt_value("22D9", "boiler_setpoint")

    @property
    def _last_opentherm_msg(self) -> Optional[Any]:  # 3220
        return self._get_pkt_value("3220")


# 03/12/22/34: 1060/2309/30C9;; (03/22: 0008/0009/3EF1, 2349?) (34: 000A/10E0/3120)
class Thermostat(Device, Setpoint, Temperature):
    """The STA class, such as a TR87RF."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating a XXX thermostat, %s", address.id)
        super().__init__(gateway, address)


# 13: "3EF0", "1100";; ("3EF1"?)
class BdrSwitch(DeviceBase, Actuator):
    """The BDR class, such as a BDR91."""

    def __init__(self, gateway, address) -> None:
        _LOGGER.debug("Creating a BDR relay, %s", address.id)
        super().__init__(gateway, address)

        self._is_tpi = None

    def _discover(self):
        super()._discover()

        self._command("1100", dest_addr=self.id, payload="00")

        # all relays seem the same, except for 0016, and 1100
        # for code in ("3B00", "3EF0", "3EF1"] + ["0008", "1100", "1260"):
        #     for payload in ("00", "FC", "FF", "0000", "000000"):
        #         self._command(code, dest_addr=self.id, payload=payload)

    def update(self, msg):
        super().update(msg)

        if self._is_tpi is None:
            _ = self.is_tpi

    @property
    def is_tpi(self) -> Optional[bool]:  # 3B00
        def make_tpi():
            self.__class__ = TpiSwitch
            self.cls_type = "TPI"
            _LOGGER.debug("Promoted device %s to %s", self.id, self.cls_type)

            self._is_tpi = True
            self.zone = "FC"
            self._discover()

        if self._is_tpi is not None:
            return self._is_tpi

        # try to cast a new type (must be a superclass of the current type)
        if "1FC9" in self._pkts and self._pkts["1FC9"].verb == "RP":
            if "3B00" in self._pkts["1FC9"].raw_payload:
                make_tpi()

        elif "3B00" in self._pkts and self._pkts["3B00"].verb == " I":
            make_tpi()

        return self._is_tpi

    @property
    def tpi_params(self) -> dict:  # 1100
        return self._get_pkt_value("1100")


# 13: "3EF0", "1100"; ("3B00")
class TpiSwitch(BdrSwitch):  # TODO: superset of BDR switch?
    """The TPI class, the BDR91 that controls the boiler."""

    def _discover(self):  # NOTE: do not super()._discover()

        for code in ("1100",):
            self._command(code, dest_addr=self.id, payload="00")

        # doesn't like like TPIs respond to a 3B00
        # for payload in ("00", "C8"):
        #     for code in ("00", "FC", "FF"):
        #         self._command("3B00", dest_addr=self.id, payload=f"{code}{payload}")


# 04: "1060", "3150", "2309", "30C9";; "0100", "12B0" ("0004")
class TrvActuator(Device, HeatDemand, Setpoint, Temperature):
    """The TRV class, such as a HR92."""

    def __init__(self, gateway, device_id) -> None:
        # _LOGGER.debug("Creating a TRV actuator, %s", device_id)
        super().__init__(gateway, device_id)

    # @property
    # def language(self) -> Optional[str]:  # 0100,
    #     return self._get_pkt_value("0100", "language")

    @property
    def window_state(self) -> Optional[bool]:  # 12B0
        return self._get_pkt_value("12B0", "window_open")


DEVICE_CLASS = {
    DEVICE_LOOKUP["BDR"]: BdrSwitch,
    DEVICE_LOOKUP["CTL"]: Controller,
    DEVICE_LOOKUP["DHW"]: DhwSensor,
    DEVICE_LOOKUP["STA"]: Thermostat,
    DEVICE_LOOKUP["STa"]: Thermostat,
    DEVICE_LOOKUP["THM"]: Thermostat,
    DEVICE_LOOKUP["THm"]: Thermostat,
    DEVICE_LOOKUP["TRV"]: TrvActuator,
    DEVICE_LOOKUP["OTB"]: OtbGateway,
    DEVICE_LOOKUP["UFH"]: UfhController,
}


def create_device(gateway, device_address, zone_idx=None) -> DeviceBase:
    """Return a device, create it if required."""
    assert device_address.type not in ("63", "--")

    if device_address.id in gateway.device_by_id:
        device = gateway.device_by_id[device_address.id]
    else:
        device = DEVICE_CLASS.get(device_address.type, Device)(gateway, device_address)

    if zone_idx:
        zone = gateway.evo.zone_by_id.get(zone_idx)
        if device._zone is None:
            device._zone = gateway.device_by_id.get(zone_idx)
        else:
            assert device._zone is zone

    return device