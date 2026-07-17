"""Volume control implementation for Linux systems."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

from .volume_control import (
    SOUND_CARD_NAMES,
    AudioDevice,
    AudioDeviceType,
    VolumeControl,
)

logger = logging.getLogger(__name__)

try:
    import pulsectl

    with pulsectl.Pulse("dummy"):
        pass
    _PULSECTL_AVAILABLE = True
except (ImportError, OSError, pulsectl.PulseError):
    _PULSECTL_AVAILABLE = False

# Constants
AUDIO_COMMAND_TIMEOUT = 2  # Timeout in seconds for audio commands


def _alsa_command_environment() -> dict[str, str]:
    """Return a stable locale for parsing ALSA command output."""
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return environment


@dataclass
class VolumeControlLinux(VolumeControl):
    """Volume control class for Linux systems.

    Uses pulsectl (PulseAudio/PipeWire) when available, falls back to ALSA (amixer/aplay/arecord) otherwise. Not using pyalsaaudio as fallback as the installation will fail if libasound2 is not present.
    """

    def __post_init__(self) -> None:
        """Initialize device IDs based on detected audio devices."""
        logger.info(
            f"Using {'pulsectl (PulseAudio/PipeWire)' if _PULSECTL_AVAILABLE else 'amixer (ALSA)'} backend"
        )
        # TODO: use a property instead to account for dynamic audio devices
        self.input_device, self.output_device = self._get_input_output_devices()

    # ---- Dispatch methods ----

    def _get_all_devices(self) -> dict[int | str, str]:
        """Get all available audio devices IDs and names.

        Returns:
            A dictionary mapping device IDs to device names.

        Raises:
            RuntimeError: If the audio device scan fails.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_get_all_devices()
        return self._alsa_get_all_devices()

    def _get_input_output_devices(self) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices corresponding to the Reachy Mini Audio sound card.

        Always resolves the ALSA card used by the hardware preflight, then
        returns the appropriate devices for the active global-volume backend.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        alsa_input, alsa_output = self._alsa_get_input_output_devices()
        self._alsa_output_device = alsa_output

        if _PULSECTL_AVAILABLE:
            return self._pulse_get_input_output_devices()
        return alsa_input, alsa_output

    def _get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 if the volume could not be read.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_get_device_volume(device)
        return self._alsa_get_device_volume(device)

    def _set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_set_device_volume(device, volume)
        return self._alsa_set_device_volume(device, volume)

    # ---- PulseAudio/PipeWire (pulsectl) backend ----

    def _pulse_get_all_devices(
        self, device_type: AudioDeviceType | None = None
    ) -> dict[int | str, str]:
        """Get all available audio devices IDs and names via pulsectl.

        Args:
            device_type: The type of device: INPUT or OUTPUT. If None, returns all devices.

        Returns:
            A dictionary containing the name of each audio device: {name: str, name: str, ...}. Monitor sources and sinks are not included.

        Raises:
            RuntimeError: If pulsectl fails when getting all audio devices.

        """
        devices: dict[int | str, str] = {}
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device_type == AudioDeviceType.OUTPUT or device_type is None:
                    for sink in pulse.sink_list():
                        devices[sink.name] = (
                            sink.description or f"Unknown device (id={sink.name})"
                        )
                if device_type == AudioDeviceType.INPUT or device_type is None:
                    for source in pulse.source_list():
                        if not source.monitor_of_sink_name:
                            devices[source.name] = (
                                source.description
                                or f"Unknown device (id={source.name})"
                            )
        except Exception as e:
            raise RuntimeError(
                f"Could not scan audio devices, pulsectl failed with error: {e}"
            )
        return devices

    def _pulse_get_input_output_devices(self) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices via pulsectl.

        If not found, falls back to the default sink/source.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        input_devices = self._pulse_get_all_devices(AudioDeviceType.INPUT)
        output_devices = self._pulse_get_all_devices(AudioDeviceType.OUTPUT)

        # Input and output devices will appear with different IDs
        input_device: AudioDevice | None = None
        output_device: AudioDevice | None = None
        for device_id, device_name in input_devices.items():
            if any(
                [sound_card in device_name.lower() for sound_card in SOUND_CARD_NAMES]
            ):
                input_device = AudioDevice(
                    device_id, device_name, AudioDeviceType.INPUT
                )
                break
        for device_id, device_name in output_devices.items():
            if any(
                [sound_card in device_name.lower() for sound_card in SOUND_CARD_NAMES]
            ):
                output_device = AudioDevice(
                    device_id, device_name, AudioDeviceType.OUTPUT
                )
                break

        # Fall back to default devices if no matching device found
        if input_device is None:
            default_id = self._pulse_get_default_device(AudioDeviceType.INPUT)
            input_device = AudioDevice(
                default_id,
                input_devices.get(default_id, f"Unknown device (id={default_id})"),
                AudioDeviceType.INPUT,
            )
        if output_device is None:
            default_id = self._pulse_get_default_device(AudioDeviceType.OUTPUT)
            output_device = AudioDevice(
                default_id,
                output_devices.get(default_id, f"Unknown device (id={default_id})"),
                AudioDeviceType.OUTPUT,
            )

        return input_device, output_device

    def _pulse_get_default_device(self, device_type: AudioDeviceType) -> str:
        """Get the default audio device ID for a given type via pulsectl.

        Args:
            device_type: The type of device: INPUT or OUTPUT.

        Returns:
            The default audio device ID.

        Raises:
            RuntimeError: If pulsectl fails when getting the default audio device.

        """
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                server_info = pulse.server_info()
                if device_type == AudioDeviceType.INPUT:
                    return str(server_info.default_source_name)
                return str(server_info.default_sink_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to get default {device_type.value} device via pulsectl: {e}"
            )

    def _pulse_get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device via pulsectl.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 on failure.

        """
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device.device_type == AudioDeviceType.INPUT:
                    pulse_device = pulse.get_source_by_name(device.id)
                else:
                    pulse_device = pulse.get_sink_by_name(device.id)
                return round(float(pulse.volume_get_all_chans(pulse_device)) * 100)
        except Exception as e:
            logger.error(
                f"Failed to get volume on device {device.id} - pulsectl error: {e}"
            )
            return -1

    def _pulse_set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device via pulsectl.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        # Clamp and convert to 0.0-1.0 for pulsectl API
        volume_scalar = max(0.0, min(1.0, volume / 100.0))
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device.device_type == AudioDeviceType.INPUT:
                    pulse_device = pulse.get_source_by_name(device.id)
                else:
                    pulse_device = pulse.get_sink_by_name(device.id)
                pulse.volume_set_all_chans(pulse_device, volume_scalar)
            return True
        except Exception as e:
            logger.error(
                f"Failed to set volume on device {device.id} - pulsectl error: {e}"
            )
            return False

    # ---- ALSA (amixer/aplay/arecord) backend ----

    def _alsa_get_device_controls(self, device: AudioDevice) -> list[str]:
        """Get the list of ALSA control names for a given device.

        Queries the card via ``amixer scontents`` and filters controls based on
        their capabilities: ``pvolume`` for output (playback) and ``cvolume``
        for input (capture).

        Args:
            device: The audio device.

        Returns:
            The list of ALSA control names matching the requested type.

        """
        capability = (
            "pvolume" if device.device_type == AudioDeviceType.OUTPUT else "cvolume"
        )

        command = (
            ["amixer", "-c", str(device.id), "scontents"]
            if device.id is not None
            else ["amixer", "scontents"]
        )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
                env=_alsa_command_environment(),
            )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            logger.warning(f"Failed to list controls for device {device.id}: {e}")
            return []

        controls: list[str] = []
        current_control: str | None = None
        control_pattern = re.compile(r"Simple mixer control '([^']+)',(\d+)")

        for line in result.stdout.splitlines():
            match = control_pattern.match(line)
            if match:
                current_control = match.group(1)
                continue
            if current_control and capability in line:
                if current_control not in controls:
                    controls.append(current_control)

        return controls

    def _alsa_get_indexed_controls(
        self,
        device: AudioDevice,
        *,
        index: int,
        capability: str,
    ) -> list[str]:
        """Return mixer controls at an exact index with the given capability."""
        if device.id is None:
            return []

        try:
            result = subprocess.run(
                ["amixer", "-c", str(device.id), "scontents"],
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
                env=_alsa_command_environment(),
            )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as error:
            raise RuntimeError(
                f"Could not inspect ALSA controls on card {device.id}: {error}"
            ) from error

        controls: list[str] = []
        current_control: str | None = None
        current_index: int | None = None
        control_pattern = re.compile(r"Simple mixer control '([^']+)',(\d+)")
        for line in result.stdout.splitlines():
            match = control_pattern.match(line)
            if match:
                current_control = match.group(1)
                current_index = int(match.group(2))
                continue
            if (
                current_control is not None
                and current_index == index
                and capability in line
                and current_control not in controls
            ):
                controls.append(current_control)
        return controls

    def _set_and_verify_hardware_output_unity(self) -> None:
        """Set hidden Reachy playback stages to unity and verify the result."""
        device = self._alsa_output_device
        if device.id is None or not any(
            sound_card in device.name.lower() for sound_card in SOUND_CARD_NAMES
        ):
            raise RuntimeError(
                f"Reachy ALSA output card was not found; selected={device.name!r}"
            )

        controls = self._alsa_get_indexed_controls(
            device,
            index=1,
            capability="pvolume",
        )
        if not controls:
            raise RuntimeError(
                "Reachy hidden ALSA playback control was not found; "
                f"card={device.id}, expected_index=1"
            )

        for control in controls:
            selector = f"{control},1"
            try:
                subprocess.run(
                    [
                        "amixer",
                        "-c",
                        str(device.id),
                        "sset",
                        selector,
                        "100%",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=AUDIO_COMMAND_TIMEOUT,
                    check=True,
                    env=_alsa_command_environment(),
                )
                result = subprocess.run(
                    ["amixer", "-c", str(device.id), "sget", selector],
                    capture_output=True,
                    text=True,
                    timeout=AUDIO_COMMAND_TIMEOUT,
                    check=True,
                    env=_alsa_command_environment(),
                )
            except (
                subprocess.TimeoutExpired,
                FileNotFoundError,
                subprocess.CalledProcessError,
            ) as error:
                raise RuntimeError(
                    "Could not initialize Reachy hidden ALSA playback control; "
                    f"card={device.id}, control={selector}, error={error}"
                ) from error

            percentages = [
                int(value) for value in re.findall(r"\[(\d+)%\]", result.stdout)
            ]
            if not percentages or any(value != 100 for value in percentages):
                raise RuntimeError(
                    "Reachy hidden ALSA playback control verification failed; "
                    f"card={device.id}, control={selector}, "
                    f"observed={percentages or 'unreadable'}"
                )
            logger.info(
                "Reachy hardware output stage ready: card=%s, control=%s, volume=100%%",
                device.id,
                selector,
            )

    def _alsa_get_all_devices(self) -> dict[int | str, str]:
        """Get all available audio devices IDs and names via ALSA.

        Returns:
            A dictionary containing the ID and name of each audio device: {id: int, name: str, ...}.

        Raises:
            RuntimeError: If aplay or arecord fail when getting all audio devices.

        """
        devices: dict[int | str, str] = {}
        try:
            pattern = re.compile(r"card\s+(\d+):\s+[^[]+\[([^\]]+)\]")
            for command in (["aplay", "-l"], ["arecord", "-l"]):
                scan_result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=AUDIO_COMMAND_TIMEOUT,
                    check=True,
                    env=_alsa_command_environment(),
                )
                for line in scan_result.stdout.splitlines():
                    match = pattern.search(line)
                    if not match:
                        continue
                    device_id = int(match.group(1))
                    device_name = match.group(2)
                    devices.setdefault(device_id, device_name)
            return devices
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            raise RuntimeError(
                f"Could not scan audio devices, aplay or arecord failed with error: {e}"
            )

    def _alsa_get_input_output_devices(self) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices via ALSA.

        If not found, returns default AudioDevices to fall back to default ALSA controls.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        devices = self._alsa_get_all_devices()

        for device_id, device_name in devices.items():
            if any(
                [sound_card in device_name.lower() for sound_card in SOUND_CARD_NAMES]
            ):
                # Input and output devices will appear with the same ID
                return AudioDevice(
                    device_id, device_name, AudioDeviceType.INPUT
                ), AudioDevice(device_id, device_name, AudioDeviceType.OUTPUT)

        return AudioDevice(None, "Default", AudioDeviceType.INPUT), AudioDevice(
            None, "Default", AudioDeviceType.OUTPUT
        )

    def _build_amixer_get_command(
        self,
        device: AudioDevice,
        controls: list[str] | None = None,
        index: int = 0,
    ) -> str:
        """Build the amixer command to get the volume of a specific device and control.

        Args:
            device: The audio device.
            controls: The list of ALSA control names to try. If None, resolved from the device.
            index: The ALSA control index. Defaults to 0.

        Returns:
            The amixer command to get the volume of the requested device.

        """
        if controls is None:
            controls = self._alsa_get_device_controls(device)
        sub_commands = []
        for control in controls:
            if device.id is not None:
                cmd = f"amixer -c {device.id} sget {control},{index}"
            else:
                cmd = f"amixer sget {control},{index}"
            sub_commands.append(cmd)

        full_command = " || ".join(sub_commands)
        return full_command

    def _build_amixer_set_command(
        self,
        device: AudioDevice,
        volume: int,
        controls: list[str] | None = None,
        index: int = 0,
    ) -> str:
        """Build the amixer command to set the volume of a specific device and control.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).
            controls: The list of ALSA control names to try. If None, resolved from the device.
            index: The ALSA control index. Defaults to 0.

        Returns:
            The amixer command to set the volume of the requested device.

        """
        if controls is None:
            controls = self._alsa_get_device_controls(device)
        volume_percent = max(0, min(100, volume))

        sub_commands = []
        for control in controls:
            if device.id is not None:
                cmd = f"amixer -c {device.id} sset {control},{index} {volume_percent}%"
            else:
                cmd = f"amixer sset {control},{index} {volume_percent}%"
            sub_commands.append(cmd)

        full_command = " || ".join(sub_commands)
        return full_command

    def _alsa_get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device via amixer.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 on failure.

        """
        try:
            result = subprocess.run(
                self._build_amixer_get_command(device),
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
                shell=True,
                env=_alsa_command_environment(),
            )
            for line in result.stdout.splitlines():
                # TODO: add support for other channels ?
                if "Left:" in line and "[" in line:
                    parts = line.split("[")
                    for part in parts:
                        if "%" in part:
                            volume_str = part.split("%")[0]
                            return int(volume_str)

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            ValueError,
            subprocess.CalledProcessError,
        ) as e:
            logger.error(
                f"Failed to get volume on device {device.id} - amixer failed with error: {e}"
            )

        return -1

    def _alsa_set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device via amixer.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        try:
            subprocess.run(
                self._build_amixer_set_command(device, volume),
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
                shell=True,
                env=_alsa_command_environment(),
            )
            return True

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            logger.error(
                f"Failed to set volume on device {device.id} - amixer failed with error: {e}"
            )
            return False

    # ---- Public API ----

    def prepare_output_device(self) -> None:
        """Prepare the Linux hardware stage, then validate global volume."""
        self._set_and_verify_hardware_output_unity()
        super().prepare_output_device()

    def get_output_volume(self) -> int:
        """Get the output volume.

        Returns:
            The output volume as a value between 0 (minimum volume) and 100 (maximum volume). Returns -1 on failure.

        """
        return self._get_device_volume(self.output_device)

    def set_output_volume(self, volume: int) -> bool:
        """Set the output volume.

        Args:
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        return self._set_device_volume(self.output_device, volume)

    def get_input_volume(self) -> int:
        """Get the input volume.

        Returns:
            The input volume as a value between 0 (minimum volume) and 100 (maximum volume). Returns -1 on failure.

        """
        return self._get_device_volume(self.input_device)

    def set_input_volume(self, volume: int) -> bool:
        """Set the input volume.

        Args:
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        return self._set_device_volume(self.input_device, volume)

    # ---- Debug / info ----

    def _get_device_controls(self, device_id: int) -> list[str]:
        """Get ALSA controls of an audio device given its ID.

        Args:
            device_id: The ALSA card number.

        Returns:
            A list of ALSA controls.

        """
        controls = []
        try:
            result = subprocess.run(
                ["amixer", "-c", str(device_id), "scontrols"],
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
                env=_alsa_command_environment(),
            )
            for line in result.stdout.splitlines():
                if "Simple mixer control" in line:
                    start = line.find("'") + 1
                    end = line.find("'", start)
                    if start > 0 and end > start:
                        controls.append(line[start:end])

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            logger.error(
                f"Failed to list controls for device {device_id} - amixer failed with error: {e}"
            )
            return []

        return controls

    def get_information(self) -> dict[str, int | str | None | list[str] | bool]:
        """Get information about the controlled audio devices.

        Returns:
            A dictionary containing the information about the controlled audio devices.

        """
        info: dict[str, int | str | None | list[str] | bool] = {
            "backend": "pulsectl" if _PULSECTL_AVAILABLE else "alsa",
            "input_device_id": self.input_device.id,
            "output_device_id": self.output_device.id,
        }
        if not _PULSECTL_AVAILABLE and isinstance(self.input_device.id, int):
            info["available_input_controls"] = self._get_device_controls(
                self.input_device.id
            )
        if not _PULSECTL_AVAILABLE and isinstance(self.output_device.id, int):
            info["available_output_controls"] = self._get_device_controls(
                self.output_device.id
            )
        return info
