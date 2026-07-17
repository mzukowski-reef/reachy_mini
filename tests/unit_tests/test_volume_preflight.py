"""Tests for fail-fast Reachy output preparation."""

import logging
import subprocess
from unittest.mock import Mock, patch

import pytest

from reachy_mini.daemon.app.main import Args, _prepare_audio_output
from reachy_mini.daemon.app.routers.volume_control import (
    AudioDevice,
    AudioDeviceType,
)
from reachy_mini.daemon.app.routers.volume_control_linux import VolumeControlLinux
from reachy_mini.daemon.app.routers.volume_control_macos import VolumeControlMacOS


def _linux_control() -> VolumeControlLinux:
    control = object.__new__(VolumeControlLinux)
    control.logger = logging.getLogger("test-volume-preflight")
    control._alsa_output_device = AudioDevice(
        1,
        "Reachy Mini Audio",
        AudioDeviceType.OUTPUT,
    )
    control.output_device = AudioDevice(
        "reachy_sink",
        "Reachy Mini Audio Analog Stereo",
        AudioDeviceType.OUTPUT,
    )
    control.get_output_volume = Mock(side_effect=[72, 72])
    control.set_output_volume = Mock(return_value=True)
    return control


def test_linux_preflight_sets_hidden_playback_stage_to_unity() -> None:
    """Linux preflight should force and verify the hidden playback stage."""
    control = _linux_control()
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1] == "scontents":
            stdout = (
                "Simple mixer control 'PCM',0\n"
                "  Capabilities: pvolume\n"
                "Simple mixer control 'PCM',1\n"
                "  Capabilities: pvolume\n"
            )
        elif "sget" in command:
            stdout = "  Mono: Playback 60 [100%] [0.00dB]\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with patch(
        "reachy_mini.daemon.app.routers.volume_control_linux.subprocess.run",
        side_effect=run,
    ):
        control.prepare_output_device()

    assert ["amixer", "-c", "1", "sset", "PCM,1", "100%"] in commands
    assert ["amixer", "-c", "1", "sget", "PCM,1"] in commands
    control.set_output_volume.assert_called_once_with(72)


def test_linux_preflight_rejects_failed_hardware_readback() -> None:
    """Linux startup should fail when the hidden stage remains attenuated."""
    control = _linux_control()

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "scontents":
            stdout = (
                "Simple mixer control 'PCM',1\n"
                "  Capabilities: pvolume\n"
            )
        elif "sget" in command:
            stdout = "  Mono: Playback 40 [67%] [-20.00dB]\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with (
        patch(
            "reachy_mini.daemon.app.routers.volume_control_linux.subprocess.run",
            side_effect=run,
        ),
        pytest.raises(RuntimeError, match="verification failed"),
    ):
        control.prepare_output_device()


def test_preflight_rejects_host_output_fallback() -> None:
    """A host speaker fallback must not masquerade as Reachy output."""
    control = _linux_control()
    control.output_device = AudioDevice(
        "host_sink",
        "MacBook Pro Speakers",
        AudioDeviceType.OUTPUT,
    )
    control._set_and_verify_hardware_output_unity = Mock()

    with pytest.raises(RuntimeError, match="was not selected"):
        control.prepare_output_device()


def test_macos_preflight_preserves_and_verifies_global_volume() -> None:
    """MacOS should verify CoreAudio without changing the configured limit."""
    control = object.__new__(VolumeControlMacOS)
    control.logger = logging.getLogger("test-macos-volume-preflight")
    control.output_device = AudioDevice(
        42,
        "Reachy Mini Audio",
        AudioDeviceType.OUTPUT,
    )
    control.get_output_volume = Mock(side_effect=[64, 64])
    control.set_output_volume = Mock(return_value=True)

    control.prepare_output_device()

    control.set_output_volume.assert_called_once_with(64)


def test_daemon_audio_preflight_runs_before_real_hardware_start() -> None:
    """Real hardware startup should execute output preparation."""
    control = Mock()
    with patch(
        "reachy_mini.daemon.app.main.get_volume_control",
        return_value=control,
    ):
        _prepare_audio_output(Args())

    control.prepare_output_device.assert_called_once_with()


@pytest.mark.parametrize(
    "args",
    [
        Args(no_media=True),
        Args(sim=True),
        Args(mockup_sim=True),
    ],
)
def test_daemon_audio_preflight_skips_non_hardware_modes(args: Args) -> None:
    """Modes without real media hardware should not run the preflight."""
    with patch("reachy_mini.daemon.app.main.get_volume_control") as factory:
        _prepare_audio_output(args)

    factory.assert_not_called()
