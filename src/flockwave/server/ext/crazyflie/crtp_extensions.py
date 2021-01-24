"""Stuff that is not in the official ``aiocflib`` Crazyflie library and that is
related to our extensions that we added to the Crazyflie firmware.
"""

from dataclasses import dataclass
from enum import IntEnum, IntFlag
from struct import Struct
from typing import Optional, Tuple

from aiocflib.crtp.crtpstack import CRTPPort

from flockwave.server.utils import clamp

__all__ = (
    "DRONE_SHOW_PORT",
    "DroneShowCommand",
    "DroneShowStatus",
)


#: Constant representing the CRTP port where we can access droneshow-related
#: services on the Crazyflie if it is running our patched firmware
DRONE_SHOW_PORT = CRTPPort.UNUSED_1


class DroneShowCommand(IntEnum):
    """Enum representing the possible command codes we can send to the
    Crazyflie drone show port.
    """

    START = 0
    PAUSE = 1
    STOP = 2
    STATUS = 3
    DEFINE_LIGHT_PROGRAM = 4
    RESTART = 5
    TRIGGER_GCS_LIGHT_EFFECT = 6


class LightProgramLocation(IntEnum):
    """Location codes for light programs."""

    INVALID = 0
    MEM = 1


class LightProgramType(IntEnum):
    """Encoding types for light programs."""

    RGB = 0
    RGB565 = 1
    SKYBRUSH = 2


class GCSLightEffectType(IntEnum):
    """Enum representing the possible light effects that we can trigger on a
    Crazyflie LED ring from the GCS, overriding the light program or the
    preflight check light signals.
    """

    OFF = 0
    SOLID = 1


class PreflightCheckStatus(IntEnum):
    """Enum representing the possible results of an onboard preflight check."""

    OFF = 0
    FAIL = 1
    WAIT = 2
    PASS = 3


class DroneShowStatusFlag(IntFlag):
    """Flags for the status bits of the drone show status packet."""

    BATTERY_CHARGING = 1
    HIGH_LEVEL_COMMANDER_ENABLED = 2
    DRONE_SHOW_MODE_ENABLED = 4
    AIRBORNE = 8
    TESTING_MODE = 16
    DISARMED = 32


class DroneShowExecutionStage(IntEnum):
    """Enum representing the execution stages of the drone show.

    These are compatible with the status variable in the drone show module of
    the Crazyflie.
    """

    UNKNOWN = 0
    IDLE = 1
    WAIT_FOR_PREFLIGHT_CHECKS = 2
    WAIT_FOR_START_SIGNAL = 3
    WAIT_FOR_TAKEOFF_TIME = 4
    TAKEOFF = 5
    PERFORMING_SHOW = 6
    LANDING = 7
    LANDED = 8

    # Error states follow from here
    LANDING_LOW_BATTERY = 9
    EXHAUSTED = 10
    ERROR = 11

    def get_short_explanation(self) -> str:
        """Returns a short explanation of the execution stage, suitable to be
        shown in a debug message.
        """
        global _execution_stage_explanations
        return _execution_stage_explanations[self]

    @property
    def is_likely_airborne(self) -> bool:
        """Returns whether the given state is an airborne state, i.e. a state
        where the drone is likely to be airborne.
        """
        cls = DroneShowExecutionStage
        return self in (
            cls.TAKEOFF,
            cls.PERFORMING_SHOW,
            cls.LANDING,
            cls.LANDING_LOW_BATTERY,
        )

    @property
    def is_idle(self) -> bool:
        """Returns whether the given state is an idle state, i.e. a state
        where the drone is likely to be on the ground and it is safe to mess
        around with the show settings.
        """
        cls = DroneShowExecutionStage
        return self in (
            cls.IDLE,
            cls.WAIT_FOR_PREFLIGHT_CHECKS,
            cls.WAIT_FOR_START_SIGNAL,
            cls.LANDED,
        )


_execution_stage_explanations = [
    "",
    "",
    "Waiting for preflight checks",
    "Ready to start",
    "Waiting for takeoff time",
    "Takeoff",
    "Performing",
    "Landing",
    "Landed",
    "Low battery",
    "Battery exhausted",
    "Error",
    "",
    "",
    "",
    "",
]


@dataclass
class DroneShowStatus:
    """Data class representing the response to a `DroneShowCommand.STATUS`
    command.
    """

    battery_voltage: float = 0.0
    flags: int = 0
    preflight_checks: Tuple[PreflightCheckStatus, ...] = ()
    position: Optional[Tuple[float, float, float]] = None
    light: int = 0
    show_execution_stage: DroneShowExecutionStage = DroneShowExecutionStage.UNKNOWN
    yaw: float = 0.0

    _struct = Struct("<HhhhhH")

    @property
    def airborne(self) -> bool:
        """Returns whether the Crazyflie is probably airborne."""
        return self.flags & DroneShowStatusFlag.AIRBORNE

    @property
    def armed(self) -> bool:
        """Returns whether the Crazyflie is armed."""
        return not self.flags & DroneShowStatusFlag.DISARMED

    @property
    def battery_percentage(self) -> int:
        """Returns the approximate battery charge percentage."""
        percentage = round(100 * (self.battery_voltage - 3.0) / 1.2)
        return clamp(percentage, 0, 100)

    @property
    def charging(self) -> bool:
        """Returns whether the battery is charging."""
        return self.flags & DroneShowStatusFlag.BATTERY_CHARGING

    @classmethod
    def from_bytes(cls, data: bytes):
        """Constructs a DroneShowStatus_ object from the raw response to the
        `DroneShowCommand.STATUS` command.
        """
        version = data[0] >> 4
        if version != 0:
            # Unknown version of the drone show status packet
            return None

        try:
            stage = DroneShowExecutionStage(data[0] & 0x0F)
        except Exception:
            stage = DroneShowExecutionStage.ERROR

        checks, x, y, z, yaw, light = cls._struct.unpack(data[3:15])
        checks = tuple((checks >> (index * 2)) & 0x03 for index in range(8))

        return cls(
            battery_voltage=data[1] / 10.0,
            flags=data[2],
            preflight_checks=checks,
            position=(x / 1000.0, y / 1000.0, z / 1000.0),
            light=light,
            show_execution_stage=stage,
            yaw=yaw,
        )

    def has_flag(self, flag: DroneShowStatusFlag) -> bool:
        """Returns whether the status object has the given flag."""
        return bool(self.flags & flag)

    @property
    def mode(self) -> str:
        """Returns a flight mode code that can be inferred from the status
        packet.
        """
        if self.flags & DroneShowStatusFlag.DRONE_SHOW_MODE_ENABLED:
            return "show"
        elif self.flags & DroneShowStatusFlag.HIGH_LEVEL_COMMANDER_ENABLED:
            return "auto"
        else:
            return "----"

    @property
    def testing(self) -> bool:
        """Returns whether the drone is in testing mode is charging."""
        return self.flags & DroneShowStatusFlag.TESTING_MODE
