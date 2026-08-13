"""Reachy Mini SDK."""

from importlib.metadata import version

from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.io.protocol import (
    AntennaMotionProfile,
    AntennaMotorGains,
    AntennaMotorGainsPair,
    AntennaProfileType,
)
from reachy_mini.reachy_mini import ReachyMini

__version__ = version("reachy_mini")

__all__ = [
    "AntennaMotionProfile",
    "AntennaMotorGains",
    "AntennaMotorGainsPair",
    "AntennaProfileType",
    "ReachyMini",
    "ReachyMiniApp",
    "__version__",
]
