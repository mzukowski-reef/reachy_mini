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
    _ProcessCounters,
    _ThreadCounters,
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
        "Name:\ttest\nvoluntary_ctxt_switches:\t12\nnonvoluntary_ctxt_switches:\t4\n"
    ) == (12, 4)
    assert _parse_schedstat("123000000 45000000 9\n") == (123000000, 45000000)


def test_thread_cpu_uses_each_threads_actual_sampling_interval(tmp_path: Path) -> None:
    """A delayed task scan must not inflate a thread above its real runtime."""
    profiler = CpuThreadProfiler(tmp_path / "cpu.jsonl")
    previous_process = _ProcessCounters(0, 0, 10.0, 100.0)
    current_process = _ProcessCounters(0, 0, 11.0, 101.0)
    previous_thread = _ThreadCounters(
        tid=123,
        name="queue1:src",
        kernel_name="queue1:src",
        user_ticks=0,
        system_ticks=0,
        processor=0,
        runtime_ns=1_000_000_000,
        wait_ns=0,
        voluntary_switches=0,
        involuntary_switches=0,
        sampled_monotonic=10.0,
    )
    current_thread = _ThreadCounters(
        tid=123,
        name="queue1:src",
        kernel_name="queue1:src",
        user_ticks=0,
        system_ticks=0,
        processor=1,
        runtime_ns=2_500_000_000,
        wait_ns=0,
        voluntary_switches=0,
        involuntary_switches=0,
        sampled_monotonic=12.0,
    )

    sample = profiler._build_sample(
        previous_process,
        current_process,
        {123: previous_thread},
        {123: current_thread},
        elapsed_seconds=1.0,
        wall_time=101.0,
        monotonic_time=11.0,
        sample_delay_seconds=0.0,
        scan_duration_seconds=1.0,
    )

    assert sample["threads"][0]["cpuPercent"] == 75.0
    assert sample["threads"][0]["elapsedMs"] == 2000.0
    assert sample["process"]["threadCpuPercent"] == 75.0


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
