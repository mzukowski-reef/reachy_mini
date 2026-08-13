"""Tests for the public per-antenna motor-gain API."""

import struct
from unittest.mock import MagicMock, call, patch

import pytest

from reachy_mini import AntennaMotorGains, AntennaMotorGainsPair, ReachyMini
from reachy_mini.daemon.backend.robot.backend import RobotBackend


def _client_only_mini() -> ReachyMini:
    mini = ReachyMini.__new__(ReachyMini)
    mini._daemon_http_url = "http://robot.test:8010"
    return mini


def _gain_registers(gains: AntennaMotorGains) -> list[int]:
    data = bytearray(12)
    struct.pack_into("<HHH", data, 0, gains.d, gains.i, gains.p)
    struct.pack_into("<HH", data, 8, gains.ff2, gains.ff1)
    return list(data)


def test_public_api_posts_validated_independent_gains() -> None:
    """The public API posts distinct gains and returns validated readback."""
    mini = _client_only_mini()
    expected = AntennaMotorGainsPair(
        right=AntennaMotorGains(p=350, d=400, ff1=4),
        left=AntennaMotorGains(p=250, d=400, ff1=8),
    )
    response = MagicMock()
    response.json.return_value = expected.model_dump()

    with patch("reachy_mini.reachy_mini.requests.post", return_value=response) as post:
        observed = mini.set_antenna_motor_gains(
            right={"p": 350, "i": 0, "d": 400, "ff1": 4, "ff2": 0},
            left=expected.left,
        )

    assert observed == expected
    post.assert_called_once_with(
        "http://robot.test:8010/api/motors/antenna-gains",
        json=expected.model_dump(),
        timeout=5.0,
    )
    response.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("value", [-1, 16_384, 1.5, True])
def test_motor_gain_values_reject_invalid_register_values(value: object) -> None:
    """Gain fields only accept integer values supported by the registers."""
    with pytest.raises(ValueError):
        AntennaMotorGains(p=value)  # type: ignore[arg-type]


def test_real_backend_writes_and_verifies_both_antennas() -> None:
    """The robot backend writes and verifies each antenna independently."""
    backend = RobotBackend.__new__(RobotBackend)
    backend.c = MagicMock()
    backend.logger = MagicMock()
    backend.name2id = {"right_antenna": 17, "left_antenna": 18}
    right = AntennaMotorGains(p=350, d=400, ff1=4)
    left = AntennaMotorGains(p=250, d=400, ff1=8)
    backend.c.async_read_raw_bytes.side_effect = [
        _gain_registers(right),
        _gain_registers(left),
    ]

    backend.set_antenna_motor_gains(right, left)

    assert backend.c.async_write_pid_gains.call_args_list == [
        call(17, 350, 0, 400),
        call(18, 250, 0, 400),
    ]
    assert backend.c.async_write_raw_bytes.call_args_list == [
        call(17, 88, list(struct.pack("<HH", 0, 4))),
        call(18, 88, list(struct.pack("<HH", 0, 8))),
    ]
    assert backend.c.async_read_raw_bytes.call_args_list == [
        call(17, 80, 12),
        call(18, 80, 12),
    ]
    assert backend._antenna_motor_gains == (right, left)


def test_real_backend_rejects_readback_mismatch() -> None:
    """A hardware readback mismatch fails the operation."""
    backend = RobotBackend.__new__(RobotBackend)
    backend.c = MagicMock()
    backend.logger = MagicMock()
    backend.name2id = {"right_antenna": 17, "left_antenna": 18}
    requested = AntennaMotorGains(p=350, d=400, ff1=4)
    stale = AntennaMotorGains(p=200)
    backend.c.async_read_raw_bytes.side_effect = [
        _gain_registers(stale),
        _gain_registers(stale),
    ]

    with pytest.raises(RuntimeError, match="readback mismatch"):
        backend.set_antenna_motor_gains(requested, requested)
