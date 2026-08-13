"""Tests for PID and feedforward gains loaded from hardware configuration."""

import struct
from pathlib import Path
from unittest.mock import MagicMock, call

from reachy_mini.daemon.backend.robot.backend import RobotBackend
from reachy_mini.utils.hardware_config.parser import (
    MotorConfig,
    ReachyMiniConfig,
    SerialConfig,
    parse_yaml_config,
)


def _motor_config(
    motor_id: int,
    *,
    pid: tuple[int, int, int] | None,
    ff1: int | None,
    ff2: int | None,
) -> MotorConfig:
    return MotorConfig(
        id=motor_id,
        offset=0,
        angle_limit_min=0,
        angle_limit_max=4095,
        return_delay_time=0,
        shutdown_error=52,
        operating_mode=3,
        pid=pid,
        ff1=ff1,
        ff2=ff2,
    )


def test_packaged_antenna_gains_remain_hardware_defaults() -> None:
    """Application tuning should not leak into the packaged hardware YAML."""
    repository_root = Path(__file__).resolve().parents[2]
    config = parse_yaml_config(
        str(repository_root / "src/reachy_mini/assets/config/hardware_config.yaml")
    )

    right = config.motors["right_antenna"]
    left = config.motors["left_antenna"]
    assert right.pid == [200, 0, 0]
    assert (right.ff1, right.ff2) == (None, None)
    assert left.pid == [200, 0, 0]
    assert (left.ff1, left.ff2) == (None, None)


def test_backend_applies_pid_and_feedforward_in_xl330_register_order() -> None:
    """Backend startup should write PID plus contiguous FF2/FF1 registers."""
    backend = RobotBackend.__new__(RobotBackend)
    backend.c = MagicMock()
    backend.logger = MagicMock()
    backend.name2id = {"right_antenna": 17, "left_antenna": 18}
    config = ReachyMiniConfig(
        version="1",
        serial=SerialConfig(baudrate=1_000_000),
        motors={
            "right_antenna": _motor_config(
                17,
                pid=(350, 0, 400),
                ff1=4,
                ff2=0,
            ),
            "left_antenna": _motor_config(
                18,
                pid=(250, 0, 400),
                ff1=8,
                ff2=0,
            ),
        },
    )

    backend._apply_configured_motor_gains(config)

    assert backend.c.async_write_pid_gains.call_args_list == [
        call(17, 350, 0, 400),
        call(18, 250, 0, 400),
    ]
    assert backend.c.async_write_raw_bytes.call_args_list == [
        call(17, 88, list(struct.pack("<HH", 0, 4))),
        call(18, 88, list(struct.pack("<HH", 0, 8))),
    ]
