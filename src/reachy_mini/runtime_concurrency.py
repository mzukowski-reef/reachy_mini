"""Bound native runtime concurrency before numerical and media imports."""

from __future__ import annotations

import os
from collections.abc import MutableMapping

NUMERIC_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "OPENCV_FOR_THREADS_NUM",
)
REACHY_NUMERIC_THREADS_ENV = "REACHY_MINI_NUMERIC_THREADS"
REACHY_TOKIO_THREADS_ENV = "REACHY_MINI_TOKIO_WORKER_THREADS"


def configure_runtime_concurrency(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Apply conservative defaults for the robot's small realtime workloads."""
    target = os.environ if environment is None else environment
    numeric_threads = _positive_int(
        target.get(REACHY_NUMERIC_THREADS_ENV, "1"),
        name=REACHY_NUMERIC_THREADS_ENV,
    )
    tokio_threads = _positive_int(
        target.get(REACHY_TOKIO_THREADS_ENV, "1"),
        name=REACHY_TOKIO_THREADS_ENV,
    )
    for name in NUMERIC_THREAD_ENV_VARS:
        target[name] = str(numeric_threads)
    target["TOKIO_WORKER_THREADS"] = str(tokio_threads)


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed
