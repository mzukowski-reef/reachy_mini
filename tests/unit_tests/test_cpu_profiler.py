"""Tests for daemon-local CPU profiling."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reachy_mini.daemon.cpu_profiler import (
    CpuThreadProfiler,
    _parse_schedstat,
    _parse_task_stat,
    _parse_task_status,
)


def test_parse_task_stat_handles_spaces_in_thread_name() -> None:
    """Linux task names may contain spaces and parentheses delimit the field."""
    fields = ["S"] + ["0"] * 36
    fields[11] = "123"
    fields[12] = "45"
    fields[36] = "7"

    parsed = _parse_task_stat(f"321 (media worker thread) {' '.join(fields)}")

    assert parsed == {
        "name": "media worker thread",
        "user_ticks": 123,
        "system_ticks": 45,
        "processor": 7,
    }


def test_parse_task_status_and_schedstat() -> None:
    """Scheduler and context-switch counters retain their kernel values."""
    assert _parse_task_status(
        "Name:\ttest\nvoluntary_ctxt_switches:\t12\n"
        "nonvoluntary_ctxt_switches:\t4\n"
    ) == (12, 4)
    assert _parse_schedstat("123000000 45000000 9\n") == (123000000, 45000000)


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="requires Linux /proc")
def test_profiler_writes_process_and_thread_samples(tmp_path: Path) -> None:
    """A running profiler emits lifecycle markers and usable CPU samples."""
    output_path = tmp_path / "cpu.jsonl"
    profiler = CpuThreadProfiler(output_path, interval_seconds=0.02)

    profiler.start()
    for _ in range(5):
        sum(index * index for index in range(200))
        time.sleep(0.02)
    profiler.stop()

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    samples = [record for record in records if record["kind"] == "cpuSample"]
    assert records[0]["kind"] == "profileStart"
    assert records[-1]["kind"] == "profileStop"
    assert samples
    assert samples[0]["pid"] == os.getpid()
    assert samples[0]["process"]["cpuPercent"] >= 0
    assert samples[0]["process"]["threadCount"] >= 1
    assert any(
        thread["name"] == "reachy-cpu-profiler"
        for sample in samples
        for thread in sample["threads"]
    )
