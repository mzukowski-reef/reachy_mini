"""Low-overhead Linux CPU profiler for the Reachy Mini daemon."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _ThreadCounters:
    tid: int
    name: str
    kernel_name: str
    user_ticks: int
    system_ticks: int
    processor: int
    runtime_ns: int
    wait_ns: int
    voluntary_switches: int
    involuntary_switches: int

class CpuThreadProfiler:
    """Sample the daemon's own Linux scheduler counters into JSONL."""

    def __init__(
        self,
        output_path: Path,
        *,
        interval_seconds: float = 1.0,
        proc_root: Path = Path("/proc"),
    ) -> None:
        """Configure a profiler without starting its sampling thread."""
        if interval_seconds <= 0:
            raise ValueError("CPU profile interval must be greater than zero")
        if not (proc_root / "self" / "task").is_dir():
            raise RuntimeError("Daemon CPU profiling requires Linux /proc task data")
        self.output_path = output_path.expanduser().absolute()
        self.interval_seconds = interval_seconds
        self.proc_root = proc_root
        self.run_id = uuid.uuid4().hex
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start sampling unless the profiler is already running."""
        if self._thread is not None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run,
            name="reachy-cpu-profiler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and flush the final profile marker."""
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=max(2.0, self.interval_seconds * 2.0))
        self._thread = None

    def _run(self) -> None:
        started_wall = time.time()
        started_monotonic = time.monotonic()
        previous_process = self._read_process_cpu()
        previous_threads = self._read_threads()
        previous_monotonic = started_monotonic
        next_sample = started_monotonic + self.interval_seconds

        with self.output_path.open("a", encoding="utf-8", buffering=1) as output:
            self._write(
                output,
                {
                    "schemaVersion": 1,
                    "kind": "profileStart",
                    "runId": self.run_id,
                    "pid": os.getpid(),
                    "wallTime": started_wall,
                    "monotonicTime": started_monotonic,
                    "intervalMs": round(self.interval_seconds * 1000.0, 3),
                },
            )
            while not self._stop_event.wait(
                max(0.0, next_sample - time.monotonic())
            ):
                sample_started = time.monotonic()
                current_process = self._read_process_cpu()
                current_threads = self._read_threads()
                sample_finished = time.monotonic()
                elapsed = sample_started - previous_monotonic
                if elapsed > 0:
                    self._write(
                        output,
                        self._build_sample(
                            previous_process,
                            current_process,
                            previous_threads,
                            current_threads,
                            elapsed_seconds=elapsed,
                            wall_time=time.time(),
                            monotonic_time=sample_started,
                            sample_delay_seconds=max(0.0, sample_started - next_sample),
                            scan_duration_seconds=sample_finished - sample_started,
                        ),
                    )
                previous_process = current_process
                previous_threads = current_threads
                previous_monotonic = sample_started
                next_sample += self.interval_seconds
                if next_sample <= sample_finished:
                    skipped = int(
                        (sample_finished - next_sample) / self.interval_seconds
                    ) + 1
                    next_sample += skipped * self.interval_seconds

            self._write(
                output,
                {
                    "schemaVersion": 1,
                    "kind": "profileStop",
                    "runId": self.run_id,
                    "pid": os.getpid(),
                    "wallTime": time.time(),
                    "monotonicTime": time.monotonic(),
                },
            )

    @staticmethod
    def _write(output: Any, payload: dict[str, Any]) -> None:
        output.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _build_sample(
        self,
        previous_process: tuple[int, int],
        current_process: tuple[int, int],
        previous_threads: dict[int, _ThreadCounters],
        current_threads: dict[int, _ThreadCounters],
        *,
        elapsed_seconds: float,
        wall_time: float,
        monotonic_time: float,
        sample_delay_seconds: float,
        scan_duration_seconds: float,
    ) -> dict[str, Any]:
        ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
        threads: list[dict[str, Any]] = []
        thread_cpu_percent = 0.0
        for tid, counters in current_threads.items():
            old = previous_threads.get(tid)
            if old is None:
                continue
            user_percent = _counter_percent(
                counters.user_ticks - old.user_ticks,
                ticks_per_second,
                elapsed_seconds,
            )
            system_percent = _counter_percent(
                counters.system_ticks - old.system_ticks,
                ticks_per_second,
                elapsed_seconds,
            )
            cpu_percent = user_percent + system_percent
            thread_cpu_percent += cpu_percent
            run_ms = _nanoseconds_delta_ms(counters.runtime_ns, old.runtime_ns)
            wait_ms = _nanoseconds_delta_ms(counters.wait_ns, old.wait_ns)
            voluntary_switches = max(
                0, counters.voluntary_switches - old.voluntary_switches
            )
            involuntary_switches = max(
                0,
                counters.involuntary_switches - old.involuntary_switches,
            )
            if not any(
                (
                    cpu_percent,
                    run_ms,
                    wait_ms,
                    voluntary_switches,
                    involuntary_switches,
                )
            ):
                continue
            thread_payload = {
                "tid": tid,
                "name": counters.name,
                "cpuPercent": round(cpu_percent, 2),
                "userCpuPercent": round(user_percent, 2),
                "systemCpuPercent": round(system_percent, 2),
                "processor": counters.processor,
                "runMs": run_ms,
                "waitMs": wait_ms,
                "voluntaryContextSwitches": voluntary_switches,
                "involuntaryContextSwitches": involuntary_switches,
            }
            if counters.kernel_name != counters.name:
                thread_payload["kernelName"] = counters.kernel_name
            threads.append(thread_payload)
        threads.sort(key=lambda item: (-item["cpuPercent"], item["tid"]))
        process_user_percent = _counter_percent(
            current_process[0] - previous_process[0],
            ticks_per_second,
            elapsed_seconds,
        )
        process_system_percent = _counter_percent(
            current_process[1] - previous_process[1],
            ticks_per_second,
            elapsed_seconds,
        )
        process_cpu_percent = process_user_percent + process_system_percent
        return {
            "schemaVersion": 1,
            "kind": "cpuSample",
            "runId": self.run_id,
            "pid": os.getpid(),
            "wallTime": wall_time,
            "monotonicTime": monotonic_time,
            "elapsedMs": round(elapsed_seconds * 1000.0, 3),
            "sampleDelayMs": round(sample_delay_seconds * 1000.0, 3),
            "scanDurationMs": round(scan_duration_seconds * 1000.0, 3),
            "process": {
                "cpuPercent": round(process_cpu_percent, 2),
                "userCpuPercent": round(process_user_percent, 2),
                "systemCpuPercent": round(process_system_percent, 2),
                "threadCpuPercent": round(thread_cpu_percent, 2),
                "threadAccountingDeltaCpuPercent": round(
                    process_cpu_percent - thread_cpu_percent,
                    2,
                ),
                "unattributedCpuPercent": round(
                    max(0.0, process_cpu_percent - thread_cpu_percent),
                    2,
                ),
                "threadCount": len(current_threads),
                "activeThreadCount": len(threads),
                "newThreadCount": len(
                    current_threads.keys() - previous_threads.keys()
                ),
                "exitedThreadCount": len(
                    previous_threads.keys() - current_threads.keys()
                ),
            },
            "threads": threads,
        }

    def _read_process_cpu(self) -> tuple[int, int]:
        stat = _parse_task_stat(
            (self.proc_root / "self" / "stat").read_text(encoding="ascii")
        )
        return stat["user_ticks"], stat["system_ticks"]

    def _read_threads(self) -> dict[int, _ThreadCounters]:
        task_root = self.proc_root / "self" / "task"
        python_thread_names = {
            thread.native_id: thread.name
            for thread in threading.enumerate()
            if thread.native_id is not None
        }
        result: dict[int, _ThreadCounters] = {}
        for task_path in task_root.iterdir():
            try:
                tid = int(task_path.name)
                stat = _parse_task_stat(
                    (task_path / "stat").read_text(encoding="ascii")
                )
                status = _parse_task_status(
                    (task_path / "status").read_text(encoding="utf-8")
                )
                schedstat = _parse_schedstat(
                    (task_path / "schedstat").read_text(encoding="ascii")
                )
            except (FileNotFoundError, ProcessLookupError, ValueError):
                continue
            result[tid] = _ThreadCounters(
                tid=tid,
                name=python_thread_names.get(tid, stat["name"]),
                kernel_name=stat["name"],
                user_ticks=stat["user_ticks"],
                system_ticks=stat["system_ticks"],
                processor=stat["processor"],
                runtime_ns=schedstat[0],
                wait_ns=schedstat[1],
                voluntary_switches=status[0],
                involuntary_switches=status[1],
            )
        return result


def _counter_percent(delta: int, ticks_per_second: float, elapsed_seconds: float) -> float:
    return max(0.0, delta) / ticks_per_second / elapsed_seconds * 100.0


def _nanoseconds_delta_ms(current: int, previous: int) -> float:
    return round(max(0, current - previous) / 1_000_000.0, 3)


def _parse_task_stat(value: str) -> dict[str, Any]:
    name_start = value.index("(")
    name_end = value.rindex(")")
    fields = value[name_end + 2 :].split()
    if len(fields) < 37:
        raise ValueError("Incomplete /proc task stat")
    return {
        "name": value[name_start + 1 : name_end],
        "user_ticks": int(fields[11]),
        "system_ticks": int(fields[12]),
        "processor": int(fields[36]),
    }


def _parse_task_status(value: str) -> tuple[int, int]:
    counters = {
        key: int(raw_value.strip())
        for key, raw_value in (
            line.split(":", 1)
            for line in value.splitlines()
            if ":" in line
        )
        if key in {"voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}
    }
    return (
        counters.get("voluntary_ctxt_switches", 0),
        counters.get("nonvoluntary_ctxt_switches", 0),
    )


def _parse_schedstat(value: str) -> tuple[int, int]:
    fields = value.split()
    if len(fields) < 2:
        raise ValueError("Incomplete /proc task schedstat")
    return int(fields[0]), int(fields[1])
