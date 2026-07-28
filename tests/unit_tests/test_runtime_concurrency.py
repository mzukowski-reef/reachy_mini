"""Tests for native runtime concurrency limits."""

from __future__ import annotations

import pytest

from reachy_mini.runtime_concurrency import (
    NUMERIC_THREAD_ENV_VARS,
    configure_runtime_concurrency,
)


def test_runtime_concurrency_uses_realtime_defaults() -> None:
    """Use conservative defaults when no explicit limits are provided."""
    environment: dict[str, str] = {}

    configure_runtime_concurrency(environment)

    assert all(environment[name] == "1" for name in NUMERIC_THREAD_ENV_VARS)
    assert environment["TOKIO_WORKER_THREADS"] == "1"


def test_runtime_concurrency_supports_explicit_reachy_limits() -> None:
    """Allow deployments to override both native runtime budgets."""
    environment = {
        "REACHY_MINI_NUMERIC_THREADS": "3",
        "REACHY_MINI_TOKIO_WORKER_THREADS": "4",
        "OPENBLAS_NUM_THREADS": "64",
        "TOKIO_WORKER_THREADS": "64",
    }

    configure_runtime_concurrency(environment)

    assert all(environment[name] == "3" for name in NUMERIC_THREAD_ENV_VARS)
    assert environment["TOKIO_WORKER_THREADS"] == "4"


@pytest.mark.parametrize(
    "name",
    ["REACHY_MINI_NUMERIC_THREADS", "REACHY_MINI_TOKIO_WORKER_THREADS"],
)
def test_runtime_concurrency_rejects_invalid_limits(name: str) -> None:
    """Reject limits that cannot represent a usable worker pool."""
    with pytest.raises(ValueError, match=name):
        configure_runtime_concurrency({name: "0"})
