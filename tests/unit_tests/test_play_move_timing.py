"""Regression tests for daemon-side move playback timing."""

import numpy as np
import pytest

import reachy_mini.daemon.backend.abstract as backend_module
from reachy_mini.daemon.backend.mockup_sim.backend import MockupSimBackend
from reachy_mini.motion.move import Move
from reachy_mini.utils.interpolation import time_trajectory


class _RecordingMove(Move):
    """Move that exercises the same normalized-time validation as GotoMove."""

    def __init__(self, duration: float) -> None:
        self._duration = duration
        self.evaluated_at: list[float] = []

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(
        self, t: float
    ) -> tuple[np.ndarray | None, np.ndarray | None, float | None]:
        self.evaluated_at.append(t)
        progress = time_trajectory(t / self.duration)
        return None, np.array([progress, progress]), None


@pytest.mark.asyncio
async def test_play_move_clamps_clock_overshoot_to_exact_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick crossing the deadline must evaluate at duration, never beyond it."""
    backend = MockupSimBackend(use_audio=False)
    move = _RecordingMove(duration=0.18)
    clock_values = iter((10.0, 10.179999, 10.1799995, 10.180001))
    antenna_targets: list[np.ndarray] = []

    monkeypatch.setattr(backend_module, "monotonic", lambda: next(clock_values))

    monkeypatch.setattr(
        backend,
        "set_target_antenna_joint_positions",
        lambda antennas: antenna_targets.append(np.asarray(antennas)),
    )

    await backend.play_move(move)

    assert move.evaluated_at[-1] == move.duration
    assert all(0.0 <= t <= move.duration for t in move.evaluated_at)
    np.testing.assert_allclose(antenna_targets[-1], [1.0, 1.0])
    assert not backend.is_move_running
