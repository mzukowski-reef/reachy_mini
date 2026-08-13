"""Tests for independent XL330 antenna motion profiles."""

import struct
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from reachy_mini import AntennaMotionProfile, ReachyMini
from reachy_mini.daemon.backend.mockup_sim.backend import MockupSimBackend
from reachy_mini.daemon.backend.robot.backend import RobotBackend
from reachy_mini.io.protocol import (
    AntennaProfileType,
    SetAntennasCmd,
    SetProfiledAntennasCmd,
)


def _client_only_mini() -> ReachyMini:
    mini = ReachyMini.__new__(ReachyMini)
    mini.client = MagicMock()
    return mini


def _hardware_free_robot_backend() -> RobotBackend:
    backend = RobotBackend.__new__(RobotBackend)
    backend.c = MagicMock()
    backend.name2id = {"right_antenna": 17, "left_antenna": 18}
    backend._antenna_target_revision = 0
    backend._pending_antenna_command = None
    backend._applied_antenna_target_revision = -1
    backend._applied_antenna_profiles = None
    return backend


def test_profile_factories_quantize_time_to_milliseconds() -> None:
    """Profile factories convert public seconds into register milliseconds."""
    step = AntennaMotionProfile.step()
    rectangular = AntennaMotionProfile.rectangular(duration=2.5)
    trapezoidal = AntennaMotionProfile.trapezoidal(
        duration=3.0,
        acceleration_duration=1.09,
    )

    assert (step.duration_ms, step.acceleration_duration_ms) == (0, 0)
    assert (rectangular.duration_ms, rectangular.acceleration_duration_ms) == (
        2500,
        0,
    )
    assert (trapezoidal.duration_ms, trapezoidal.acceleration_duration_ms) == (
        3000,
        1090,
    )


@pytest.mark.parametrize(
    "profile",
    [
        {
            "profile_type": "step",
            "duration": 1.0,
        },
        {
            "profile_type": "rectangular",
        },
        {
            "profile_type": "rectangular",
            "duration": 1.0,
            "acceleration_duration": 0.1,
        },
        {
            "profile_type": "trapezoidal",
            "duration": 1.0,
        },
        {
            "profile_type": "trapezoidal",
            "duration": 1.0,
            "acceleration_duration": 0.501,
        },
        {
            "profile_type": "trapezoidal",
            "duration": 40.0,
            "acceleration_duration": 1.0,
        },
    ],
)
def test_profile_rejects_unrepresentable_timing(profile: dict[str, object]) -> None:
    """Reject timings that cannot be represented by an XL330 profile."""
    with pytest.raises(ValueError):
        AntennaMotionProfile.model_validate(profile)


def test_public_api_broadcasts_one_profile_to_both_antennas() -> None:
    """One profile value applies to both antennas."""
    mini = _client_only_mini()
    profile = AntennaMotionProfile.trapezoidal(
        duration=3.0,
        acceleration_duration=1.09,
    )

    mini.set_target_antenna_joint_positions(
        [-0.75, 0.75],
        profiles=profile,
    )

    command = mini.client.send_command.call_args.args[0]
    assert isinstance(command, SetProfiledAntennasCmd)
    assert command.antennas == [-0.75, 0.75]
    assert command.profiles == [profile, profile]


def test_public_api_accepts_different_named_profiles() -> None:
    """Named right and left profiles may be configured independently."""
    mini = _client_only_mini()

    mini.set_target_antenna_joint_positions(
        [-0.25, 0.75],
        profiles={
            "right": {
                "profile_type": "rectangular",
                "duration": 2.5,
            },
            "left": {
                "profile_type": "trapezoidal",
                "duration": 3.0,
                "acceleration_duration": 1.0,
            },
        },
    )

    command = mini.client.send_command.call_args.args[0]
    assert isinstance(command, SetProfiledAntennasCmd)
    assert command.profiles[0].profile_type is AntennaProfileType.RECTANGULAR
    assert command.profiles[1].profile_type is AntennaProfileType.TRAPEZOIDAL


def test_public_api_without_profiles_keeps_legacy_command() -> None:
    """Omitting profiles preserves the legacy immediate-target command."""
    mini = _client_only_mini()

    mini.set_target_antenna_joint_positions([-0.25, 0.25])

    command = mini.client.send_command.call_args.args[0]
    assert isinstance(command, SetAntennasCmd)


def test_profiled_command_dispatches_on_simulation_backend() -> None:
    """Simulation accepts profiled commands without hardware registers."""
    backend = MockupSimBackend(use_audio=False)
    right = AntennaMotionProfile.rectangular(duration=2.5)
    left = AntennaMotionProfile.trapezoidal(
        duration=3.0,
        acceleration_duration=1.0,
    )
    responses: list[dict[str, object]] = []

    backend.process_command(
        SetProfiledAntennasCmd(
            antennas=[-0.25, 0.75],
            profiles=[right, left],
        ),
        send_response=responses.append,
    )

    np.testing.assert_array_equal(
        backend.target_antenna_joint_positions,
        [-0.25, 0.75],
    )
    assert backend._pending_antenna_command is not None
    assert backend._pending_antenna_command[1] == (right, left)
    assert responses == [{"status": "ok", "command": "set_profiled_antennas"}]


def test_real_backend_writes_independent_profiles_before_one_goal() -> None:
    """Hardware profile registers are written before the shared goal."""
    backend = _hardware_free_robot_backend()
    right = AntennaMotionProfile.trapezoidal(
        duration=3.0,
        acceleration_duration=1.09,
    )
    left = AntennaMotionProfile.rectangular(duration=2.5)
    backend.set_profiled_target_antenna_joint_positions(
        np.array([-0.75, 0.25]),
        (right, left),
    )

    backend._write_pending_antenna_command()

    assert backend.c.async_write_raw_bytes.call_args_list == [
        call(17, 108, list(struct.pack("<I", 1090))),
        call(17, 112, list(struct.pack("<I", 3000))),
        call(18, 108, list(struct.pack("<I", 0))),
        call(18, 112, list(struct.pack("<I", 2500))),
    ]
    backend.c.set_antennas_positions.assert_called_once_with([-0.75, 0.25])

    backend._write_pending_antenna_command()
    assert backend.c.async_write_raw_bytes.call_count == 4
    assert backend.c.set_antennas_positions.call_count == 1


def test_legacy_target_resets_both_profiles_to_step() -> None:
    """A legacy target restores immediate step profiles on both motors."""
    backend = _hardware_free_robot_backend()
    profiled = AntennaMotionProfile.trapezoidal(
        duration=3.0,
        acceleration_duration=1.09,
    )
    backend._applied_antenna_profiles = (profiled, profiled)

    backend.set_target_antenna_joint_positions(np.array([-0.3, 0.3]))
    backend._write_pending_antenna_command()

    assert backend.c.async_write_raw_bytes.call_args_list == [
        call(17, 108, [0, 0, 0, 0]),
        call(17, 112, [0, 0, 0, 0]),
        call(18, 108, [0, 0, 0, 0]),
        call(18, 112, [0, 0, 0, 0]),
    ]
    backend.c.set_antennas_positions.assert_called_once_with([-0.3, 0.3])


def test_drive_mode_setup_preserves_bits_and_restores_torque() -> None:
    """Time-profile setup preserves unrelated bits and restores torque."""
    backend = _hardware_free_robot_backend()
    backend._torque_enabled = True
    backend.c.get_last_position.return_value.antennas = [-0.4, 0.4]
    drive_modes = {17: 0b1001, 18: 0b0001}
    torque_enabled = True

    def write_raw(motor_id: int, address: int, data: list[int]) -> None:
        if address == 10:
            drive_modes[motor_id] = data[0]

    def disable_torque(_ids: list[int]) -> None:
        nonlocal torque_enabled
        torque_enabled = False

    def enable_torque(_ids: list[int]) -> None:
        nonlocal torque_enabled
        torque_enabled = True

    def read_raw(motor_id: int, address: int, _length: int) -> list[int]:
        if address == 10:
            return [drive_modes[motor_id]]
        if address == 64:
            return [int(torque_enabled)]
        return [0, 0, 0, 0]

    backend.c.async_write_raw_bytes.side_effect = write_raw
    backend.c.async_read_raw_bytes.side_effect = read_raw
    backend.c.disable_torque_on_ids.side_effect = disable_torque
    backend.c.enable_torque_on_ids.side_effect = enable_torque

    backend._configure_antenna_time_profiles()

    assert drive_modes == {17: 0b1101, 18: 0b0101}
    backend.c.disable_torque_on_ids.assert_called_once_with([17, 18])
    backend.c.enable_torque_on_ids.assert_called_once_with([17, 18])
    assert torque_enabled
    backend.c.set_antennas_positions.assert_called_with([-0.4, 0.4])


def test_drive_mode_failure_still_restores_antenna_torque() -> None:
    """A setup failure restores torque before propagating the error."""
    backend = _hardware_free_robot_backend()
    backend._torque_enabled = True
    backend.c.get_last_position.return_value.antennas = [-0.4, 0.4]
    torque_enabled = True

    def disable_torque(_ids: list[int]) -> None:
        nonlocal torque_enabled
        torque_enabled = False

    def enable_torque(_ids: list[int]) -> None:
        nonlocal torque_enabled
        torque_enabled = True

    def read_raw(_motor_id: int, address: int, _length: int) -> list[int]:
        if address == 10:
            return [0]
        if address == 64:
            return [int(torque_enabled)]
        return [0, 0, 0, 0]

    backend.c.async_read_raw_bytes.side_effect = read_raw
    backend.c.disable_torque_on_ids.side_effect = disable_torque
    backend.c.enable_torque_on_ids.side_effect = enable_torque

    with pytest.raises(RuntimeError, match="time-based profiles"):
        backend._configure_antenna_time_profiles()

    backend.c.enable_torque_on_ids.assert_called_once_with([17, 18])
    assert torque_enabled


def test_drive_mode_setup_preserves_each_antennas_prior_torque_state() -> None:
    """Setup restores each antenna's original torque state independently."""
    backend = _hardware_free_robot_backend()
    backend.c.get_last_position.return_value.antennas = [-0.4, 0.4]
    drive_modes = {17: 0, 18: 0}
    torque_states = {17: 1, 18: 0}

    def write_raw(motor_id: int, address: int, data: list[int]) -> None:
        if address == 10:
            drive_modes[motor_id] = data[0]

    def disable_torque(ids: list[int]) -> None:
        for motor_id in ids:
            torque_states[motor_id] = 0

    def enable_torque(ids: list[int]) -> None:
        for motor_id in ids:
            torque_states[motor_id] = 1

    def read_raw(motor_id: int, address: int, _length: int) -> list[int]:
        if address == 10:
            return [drive_modes[motor_id]]
        if address == 64:
            return [torque_states[motor_id]]
        return [0, 0, 0, 0]

    backend.c.async_write_raw_bytes.side_effect = write_raw
    backend.c.async_read_raw_bytes.side_effect = read_raw
    backend.c.disable_torque_on_ids.side_effect = disable_torque
    backend.c.enable_torque_on_ids.side_effect = enable_torque

    backend._configure_antenna_time_profiles()

    backend.c.disable_torque_on_ids.assert_called_once_with([17])
    backend.c.enable_torque_on_ids.assert_called_once_with([17])
    assert torque_states == {17: 1, 18: 0}
