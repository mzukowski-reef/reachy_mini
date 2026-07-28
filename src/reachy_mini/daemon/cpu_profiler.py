"""Low-overhead Linux CPU profiler for the Reachy Mini daemon."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _ProcessCounters:
    user_ticks: int
    system_ticks: int
    sampled_monotonic: float
    sampled_wall: float


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
    sampled_monotonic: float
    nice: int = 0
    scheduler_policy: int = 0


class CpuThreadProfiler:
    """Sample the daemon's own Linux scheduler counters into JSONL."""

    def __init__(
        self,
        output_path: Path,
        *,
        interval_seconds: float = 1.0,
        thread_interval_seconds: float = 5.0,
        proc_root: Path = Path("/proc"),
        target_pid: int | None = None,
    ) -> None:
        """Configure a profiler without starting its sampling thread."""
        if interval_seconds <= 0:
            raise ValueError("CPU profile interval must be greater than zero")
        if thread_interval_seconds < interval_seconds:
            raise ValueError(
                "CPU thread profile interval must be at least the process interval"
            )
        self.target_pid = target_pid or os.getpid()
        self.process_root = proc_root / str(self.target_pid)
        if not (self.process_root / "task").is_dir():
            raise RuntimeError("Daemon CPU profiling requires Linux /proc task data")
        self.output_path = output_path.expanduser().absolute()
        self.interval_seconds = interval_seconds
        self.thread_interval_seconds = thread_interval_seconds
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
        self._lower_current_thread_priority()
        started_wall = time.time()
        started_monotonic = time.monotonic()
        previous_process = self._read_process_cpu()
        previous_thread_process = previous_process
        previous_threads = self._read_threads()
        next_sample = started_monotonic + self.interval_seconds
        next_thread_sample = started_monotonic + self.thread_interval_seconds

        with self.output_path.open("a", encoding="utf-8", buffering=1) as output:
            self._write(
                output,
                {
                    "schemaVersion": 1,
                    "kind": "profileStart",
                    "runId": self.run_id,
                    "pid": self.target_pid,
                    "profilerPid": os.getpid(),
                    "wallTime": started_wall,
                    "monotonicTime": started_monotonic,
                    "intervalMs": round(self.interval_seconds * 1000.0, 3),
                    "threadIntervalMs": round(
                        self.thread_interval_seconds * 1000.0,
                        3,
                    ),
                },
            )
            while not self._stop_event.wait(max(0.0, next_sample - time.monotonic())):
                sample_started = time.monotonic()
                current_process = self._read_process_cpu()
                thread_sample = sample_started >= next_thread_sample
                current_threads = self._read_threads() if thread_sample else None
                sample_finished = time.monotonic()
                elapsed = (
                    current_process.sampled_monotonic
                    - previous_process.sampled_monotonic
                )
                if elapsed > 0:
                    self._write(
                        output,
                        self._build_sample(
                            previous_process,
                            current_process,
                            previous_threads,
                            current_threads,
                            previous_thread_process=previous_thread_process,
                            thread_sample=thread_sample,
                            elapsed_seconds=elapsed,
                            wall_time=current_process.sampled_wall,
                            monotonic_time=current_process.sampled_monotonic,
                            sample_delay_seconds=max(0.0, sample_started - next_sample),
                            scan_duration_seconds=sample_finished - sample_started,
                        ),
                    )
                previous_process = current_process
                if current_threads is not None:
                    previous_thread_process = current_process
                    previous_threads = current_threads
                    while next_thread_sample <= sample_finished:
                        next_thread_sample += self.thread_interval_seconds
                next_sample += self.interval_seconds
                if next_sample <= sample_finished:
                    skipped = (
                        int((sample_finished - next_sample) / self.interval_seconds) + 1
                    )
                    next_sample += skipped * self.interval_seconds

            self._write(
                output,
                {
                    "schemaVersion": 1,
                    "kind": "profileStop",
                    "runId": self.run_id,
                    "pid": self.target_pid,
                    "profilerPid": os.getpid(),
                    "wallTime": time.time(),
                    "monotonicTime": time.monotonic(),
                },
            )

    @staticmethod
    def _lower_current_thread_priority() -> None:
        """Keep diagnostics from competing with media processing."""
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _write(output: Any, payload: dict[str, Any]) -> None:
        output.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _build_sample(
        self,
        previous_process: _ProcessCounters,
        current_process: _ProcessCounters,
        previous_threads: dict[int, _ThreadCounters],
        current_threads: dict[int, _ThreadCounters] | None,
        *,
        thread_sample: bool = True,
        previous_thread_process: _ProcessCounters | None = None,
        elapsed_seconds: float,
        wall_time: float,
        monotonic_time: float,
        sample_delay_seconds: float,
        scan_duration_seconds: float,
    ) -> dict[str, Any]:
        ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
        threads: list[dict[str, Any]] = []
        thread_cpu_percent = 0.0
        for tid, counters in (current_threads or {}).items():
            old = previous_threads.get(tid)
            if old is None:
                continue
            thread_elapsed_seconds = counters.sampled_monotonic - old.sampled_monotonic
            if thread_elapsed_seconds <= 0:
                continue
            user_percent = _counter_percent(
                counters.user_ticks - old.user_ticks,
                ticks_per_second,
                thread_elapsed_seconds,
            )
            system_percent = _counter_percent(
                counters.system_ticks - old.system_ticks,
                ticks_per_second,
                thread_elapsed_seconds,
            )
            cpu_percent = _runtime_percent(
                counters.runtime_ns - old.runtime_ns,
                thread_elapsed_seconds,
            )
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
                "nice": counters.nice,
                "schedulerPolicy": counters.scheduler_policy,
                "elapsedMs": round(thread_elapsed_seconds * 1000.0, 3),
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
            current_process.user_ticks - previous_process.user_ticks,
            ticks_per_second,
            elapsed_seconds,
        )
        process_system_percent = _counter_percent(
            current_process.system_ticks - previous_process.system_ticks,
            ticks_per_second,
            elapsed_seconds,
        )
        process_cpu_percent = process_user_percent + process_system_percent
        process_payload: dict[str, Any] = {
            "cpuPercent": round(process_cpu_percent, 2),
            "userCpuPercent": round(process_user_percent, 2),
            "systemCpuPercent": round(process_system_percent, 2),
        }
        if thread_sample and current_threads is not None:
            thread_process_base = previous_thread_process or previous_process
            thread_process_elapsed = (
                current_process.sampled_monotonic
                - thread_process_base.sampled_monotonic
            )
            thread_process_user_percent = _counter_percent(
                current_process.user_ticks - thread_process_base.user_ticks,
                ticks_per_second,
                thread_process_elapsed,
            )
            thread_process_system_percent = _counter_percent(
                current_process.system_ticks - thread_process_base.system_ticks,
                ticks_per_second,
                thread_process_elapsed,
            )
            thread_process_cpu_percent = (
                thread_process_user_percent + thread_process_system_percent
            )
            process_payload.update(
                {
                    "threadWindowElapsedMs": round(
                        thread_process_elapsed * 1000.0,
                        3,
                    ),
                    "threadWindowCpuPercent": round(thread_process_cpu_percent, 2),
                    "threadCpuPercent": round(thread_cpu_percent, 2),
                    "threadAccountingDeltaCpuPercent": round(
                        thread_process_cpu_percent - thread_cpu_percent,
                        2,
                    ),
                    "unattributedCpuPercent": round(
                        max(0.0, thread_process_cpu_percent - thread_cpu_percent),
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
                }
            )
        return {
            "schemaVersion": 1,
            "kind": "cpuSample",
            "runId": self.run_id,
            "pid": self.target_pid,
            "wallTime": wall_time,
            "monotonicTime": monotonic_time,
            "elapsedMs": round(elapsed_seconds * 1000.0, 3),
            "sampleDelayMs": round(sample_delay_seconds * 1000.0, 3),
            "scanDurationMs": round(scan_duration_seconds * 1000.0, 3),
            "threadSample": thread_sample,
            "process": process_payload,
            "threads": threads,
        }

    def _read_process_cpu(self) -> _ProcessCounters:
        stat = _parse_task_stat(
            (self.process_root / "stat").read_text(encoding="ascii")
        )
        return _ProcessCounters(
            user_ticks=stat["user_ticks"],
            system_ticks=stat["system_ticks"],
            sampled_monotonic=time.monotonic(),
            sampled_wall=time.time(),
        )

    def _read_threads(self) -> dict[int, _ThreadCounters]:
        task_root = self.process_root / "task"
        python_thread_names = (
            {
                thread.native_id: thread.name
                for thread in threading.enumerate()
                if thread.native_id is not None
            }
            if self.target_pid == os.getpid()
            else {}
        )
        result: dict[int, _ThreadCounters] = {}
        for task_path in task_root.iterdir():
            try:
                tid = int(task_path.name)
                stat = _parse_task_stat(
                    (task_path / "stat").read_text(encoding="ascii")
                )
                schedstat = _parse_schedstat(
                    (task_path / "schedstat").read_text(encoding="ascii")
                )
                sampled_monotonic = time.monotonic()
                status = _parse_task_status(
                    (task_path / "status").read_text(encoding="utf-8")
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
                sampled_monotonic=sampled_monotonic,
                nice=stat["nice"],
                scheduler_policy=stat["scheduler_policy"],
            )
        return result


class CpuProfilerProcess:
    """Run daemon profiling outside the daemon's GIL and media process."""

    def __init__(
        self,
        output_path: Path,
        *,
        interval_seconds: float = 1.0,
        thread_interval_seconds: float = 5.0,
    ) -> None:
        """Configure the external profiler process."""
        self.output_path = output_path.expanduser().absolute()
        self.interval_seconds = interval_seconds
        self.thread_interval_seconds = thread_interval_seconds
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Start profiling the current process if no worker is active."""
        if self._process is not None and self._process.poll() is None:
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reachy_mini.daemon.cpu_profiler",
                "--worker-pid",
                str(os.getpid()),
                "--output",
                str(self.output_path),
                "--interval",
                str(self.interval_seconds),
                "--thread-interval",
                str(self.thread_interval_seconds),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def stop(self) -> None:
        """Stop the profiler worker and wait for it to exit."""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(2.0, self.interval_seconds * 2.0))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        self._process = None


def _run_worker(args: argparse.Namespace) -> int:
    stopping = threading.Event()

    def _request_stop(*_args: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    profiler = CpuThreadProfiler(
        Path(args.output),
        interval_seconds=args.interval,
        thread_interval_seconds=args.thread_interval,
        target_pid=args.worker_pid,
    )
    profiler.start()
    try:
        while not stopping.wait(0.5):
            if not Path(f"/proc/{args.worker_pid}").exists():
                break
    finally:
        profiler.stop()
    return 0


def _parse_worker_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--thread-interval", type=float, required=True)
    return parser.parse_args()


def _counter_percent(
    delta: int, ticks_per_second: float, elapsed_seconds: float
) -> float:
    return max(0.0, delta) / ticks_per_second / elapsed_seconds * 100.0


def _runtime_percent(delta_ns: int, elapsed_seconds: float) -> float:
    return max(0, delta_ns) / 1_000_000_000.0 / elapsed_seconds * 100.0


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
        "nice": int(fields[16]),
        "scheduler_policy": int(fields[38]) if len(fields) > 38 else 0,
    }


def _parse_task_status(value: str) -> tuple[int, int]:
    counters = {
        key: int(raw_value.strip())
        for key, raw_value in (
            line.split(":", 1) for line in value.splitlines() if ":" in line
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


if __name__ == "__main__":
    raise SystemExit(_run_worker(_parse_worker_args()))
