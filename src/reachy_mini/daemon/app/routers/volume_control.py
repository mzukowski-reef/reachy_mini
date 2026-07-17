"""Volume control base class and factory for platform-specific implementations."""

import logging
import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

SOUND_CARD_NAMES = ["reachy mini audio", "respeaker"]


class AudioDeviceType(Enum):
    """Type of device: INPUT or OUTPUT."""

    INPUT = "input"
    OUTPUT = "output"


class AudioDevice(NamedTuple):
    """An audio device with its ID, name, and type."""

    id: int | str | None
    name: str
    device_type: AudioDeviceType


@dataclass
class VolumeControl(ABC):
    """Base class for volume control."""

    logger: logging.Logger = field(
        init=False,
        default_factory=lambda: logging.getLogger(
            f"[VolumeControl {platform.system()}]"
        ),
    )
    platform_name: str = field(init=False, default_factory=platform.system)
    input_device: AudioDevice = field(init=False)
    output_device: AudioDevice = field(init=False)

    @abstractmethod
    def set_output_volume(self, volume: int) -> bool:
        """Set the output volume to the provided value between 0 (minimum volume) and 100 (maximum volume)."""
        pass

    @abstractmethod
    def get_output_volume(self) -> int:
        """Get the output volume as a value between 0 (minimum volume) and 100 (maximum volume)."""
        pass

    @abstractmethod
    def set_input_volume(self, volume: int) -> bool:
        """Set the input volume to the provided value between 0 (minimum volume) and 100 (maximum volume)."""
        pass

    @abstractmethod
    def get_input_volume(self) -> int:
        """Get the input volume as a value between 0 (minimum volume) and 100 (maximum volume)."""
        pass

    def prepare_output_device(self) -> None:
        """Validate the Reachy output path without changing its global volume.

        Platform implementations may extend this check for hardware-specific
        mixer stages. The common check rejects a fallback to the host's default
        speakers and verifies that the selected global volume control supports
        a read/write/read round trip.
        """
        if not any(
            sound_card in self.output_device.name.lower()
            for sound_card in SOUND_CARD_NAMES
        ):
            raise RuntimeError(
                "Reachy audio output device was not selected; "
                f"selected={self.output_device.name!r}"
            )

        expected_volume = self.get_output_volume()
        if not 0 <= expected_volume <= 100:
            raise RuntimeError(
                "Could not read Reachy global output volume; "
                f"device={self.output_device.name!r}, value={expected_volume}"
            )
        if not self.set_output_volume(expected_volume):
            raise RuntimeError(
                "Could not write Reachy global output volume; "
                f"device={self.output_device.name!r}, value={expected_volume}"
            )

        observed_volume = self.get_output_volume()
        if abs(observed_volume - expected_volume) > 2:
            raise RuntimeError(
                "Reachy global output volume verification failed; "
                f"device={self.output_device.name!r}, expected={expected_volume}, "
                f"observed={observed_volume}"
            )

        self.logger.info(
            "Reachy output device ready: device=%r, global_volume=%d%%",
            self.output_device.name,
            observed_volume,
        )


def create_volume_control() -> VolumeControl:
    """Return the correct VolumeControl subclass for the current platform.

    Imports are lazy to avoid loading platform-specific dependencies on the wrong OS
    (e.g. CoreAudio on Linux, pycaw on macOS).

    Returns:
        A VolumeControl instance for the current platform.

    Raises:
        RuntimeError: If the current platform is not supported.

    """
    system = platform.system()

    if system == "Darwin":
        from .volume_control_macos import VolumeControlMacOS

        return VolumeControlMacOS()
    elif system == "Linux":
        from .volume_control_linux import VolumeControlLinux

        return VolumeControlLinux()
    elif system == "Windows":
        from .volume_control_windows import VolumeControlWindows

        return VolumeControlWindows()
    else:
        raise RuntimeError(f"Unsupported platform for volume control: {system}")


# Lazily-initialised process-wide singleton shared between the REST
# volume router and the backend's WebRTC command handler. Both paths
# must observe the same VolumeControl instance so that a remote volume
# change is immediately reflected on the next REST query and vice
# versa. See reachy_mini/daemon/backend/abstract.py and
# reachy_mini/daemon/app/routers/volume.py for the two callers.
_volume_control: VolumeControl | None = None


def get_volume_control() -> VolumeControl:
    """Return the shared VolumeControl, creating it on first call."""
    global _volume_control  # noqa: PLW0603
    if _volume_control is None:
        _volume_control = create_volume_control()
    return _volume_control
