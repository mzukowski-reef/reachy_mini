"""Tests for unmuting the Reachy output on Windows.

pycaw exists only on Windows, so these tests stub it and the endpoint volume.
"""

import importlib
import logging
import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from reachy_mini.daemon.app.routers.volume_control import (
    AudioDevice,
    AudioDeviceType,
)

WINDOWS_MODULE = "reachy_mini.daemon.app.routers.volume_control_windows"


class FakeEndpointVolume:
    """IAudioEndpointVolume of a hardware endpoint ranging from -60 to 0 dB."""

    def __init__(self, level_db: float = -6.0, muted: bool = True) -> None:
        self.level_db = level_db
        self.muted = muted
        self.refuses_unmute = False
        self.mute_writes: list[int] = []

    def GetVolumeRange(self) -> tuple[float, float, float]:  # noqa: N802
        return (-60.0, 0.0, 1.0)

    def GetMasterVolumeLevel(self) -> float:  # noqa: N802
        return self.level_db

    def SetMasterVolumeLevel(self, level_db: float, context: Any) -> None:  # noqa: N802
        self.level_db = level_db

    def GetMute(self) -> int:  # noqa: N802
        return int(self.muted)

    def SetMute(self, muted: int, context: Any) -> None:  # noqa: N802
        self.mute_writes.append(muted)
        if not self.refuses_unmute:
            self.muted = bool(muted)


@pytest.fixture
def windows_module() -> Any:
    pycaw = ModuleType("pycaw")
    pycaw_pycaw = ModuleType("pycaw.pycaw")
    for name in ("DEVICE_STATE", "AudioUtilities", "EDataFlow", "ERole"):
        setattr(pycaw_pycaw, name, MagicMock())
    with patch.dict(sys.modules, {"pycaw": pycaw, "pycaw.pycaw": pycaw_pycaw}):
        sys.modules.pop(WINDOWS_MODULE, None)
        yield importlib.import_module(WINDOWS_MODULE)
    sys.modules.pop(WINDOWS_MODULE, None)


def _control(windows_module: Any, endpoint: FakeEndpointVolume) -> Any:
    control = object.__new__(windows_module.VolumeControlWindows)
    control.logger = logging.getLogger("test-volume-control-windows")
    control.output_device = AudioDevice(
        "{reachy-render}",
        "Echo Cancelling Speakerphone (Reachy Mini Audio)",
        AudioDeviceType.OUTPUT,
    )
    control._get_device_volume_interface = MagicMock(return_value=endpoint)
    return control


def test_setting_output_volume_unmutes_the_reachy_output(windows_module: Any) -> None:
    """A muted endpoint still reports its volume, so every write unmutes it."""
    endpoint = FakeEndpointVolume(level_db=-6.0, muted=True)
    control = _control(windows_module, endpoint)

    assert control.set_output_volume(100) is True

    assert endpoint.level_db == 0.0
    assert endpoint.muted is False


def test_setting_output_volume_leaves_an_unmuted_output_alone(
    windows_module: Any,
) -> None:
    """An output that is already unmuted gets no mute write."""
    endpoint = FakeEndpointVolume(muted=False)
    control = _control(windows_module, endpoint)

    assert control.set_output_volume(50) is True

    assert endpoint.mute_writes == []


def test_setting_output_volume_fails_while_the_output_stays_muted(
    windows_module: Any,
) -> None:
    """A device that refuses to unmute is a failed write, not a silent success."""
    endpoint = FakeEndpointVolume(muted=True)
    endpoint.refuses_unmute = True
    control = _control(windows_module, endpoint)

    assert control.set_output_volume(100) is False


def test_preflight_unmutes_and_keeps_the_global_volume(windows_module: Any) -> None:
    """Startup clears Windows' mute without changing the chosen volume."""
    endpoint = FakeEndpointVolume(level_db=-6.0, muted=True)
    control = _control(windows_module, endpoint)

    control.prepare_output_device()

    assert endpoint.muted is False
    assert endpoint.level_db == pytest.approx(-6.0)


def test_preflight_fails_while_the_output_stays_muted(windows_module: Any) -> None:
    """The daemon must not start with a Reachy output nobody can hear."""
    endpoint = FakeEndpointVolume(muted=True)
    endpoint.refuses_unmute = True
    control = _control(windows_module, endpoint)

    with pytest.raises(RuntimeError, match="global output volume"):
        control.prepare_output_device()
