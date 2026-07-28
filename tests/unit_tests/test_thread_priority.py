"""Tests for best-effort realtime media scheduling."""

from __future__ import annotations

import os

from reachy_mini.media import thread_priority


def test_high_priority_uses_direct_scheduler_permission(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(thread_priority.platform, "system", lambda: "Linux")
    monkeypatch.setattr(thread_priority, "get_native_id", lambda: 123)
    monkeypatch.setattr(os, "setpriority", lambda scope, tid, nice: calls.append((scope, tid, nice)))

    assert thread_priority.request_current_thread_high_priority(-5)
    assert calls == [(os.PRIO_PROCESS, 123, -5)]


def test_high_priority_falls_back_to_rtkit(monkeypatch) -> None:
    monkeypatch.setattr(thread_priority.platform, "system", lambda: "Linux")
    monkeypatch.setattr(thread_priority, "get_native_id", lambda: 123)
    monkeypatch.setattr(os, "setpriority", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        thread_priority,
        "_call_rtkit",
        lambda method, signature, tid, value: calls.append(
            (method, signature, tid, value)
        )
        is None,
    )

    assert thread_priority.request_current_thread_high_priority(-5)
    assert calls == [("MakeThreadHighPriority", "(ti)", 123, -5)]


def test_realtime_uses_rtkit_when_direct_scheduler_is_denied(monkeypatch) -> None:
    monkeypatch.setattr(thread_priority.platform, "system", lambda: "Linux")
    monkeypatch.setattr(thread_priority, "get_native_id", lambda: 321)
    monkeypatch.setattr(
        os,
        "sched_setscheduler",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        thread_priority,
        "_call_rtkit",
        lambda method, signature, tid, value: calls.append(
            (method, signature, tid, value)
        )
        is None,
    )

    assert thread_priority.request_current_thread_realtime(5)
    assert calls == [("MakeThreadRealtime", "(tu)", 321, 5)]


def test_realtime_falls_back_to_high_priority_when_rtkit_denies_rr(
    monkeypatch,
) -> None:
    monkeypatch.setattr(thread_priority.platform, "system", lambda: "Linux")
    monkeypatch.setattr(thread_priority, "get_native_id", lambda: 321)
    monkeypatch.setattr(
        os,
        "sched_setscheduler",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(thread_priority, "_call_rtkit", lambda *_args: False)
    requested: list[int] = []
    monkeypatch.setattr(
        thread_priority,
        "request_current_thread_high_priority",
        lambda nice: requested.append(nice) is None,
    )

    assert thread_priority.request_current_thread_realtime(5)
    assert requested == [-10]


def test_priority_requests_are_noop_outside_linux(monkeypatch) -> None:
    monkeypatch.setattr(thread_priority.platform, "system", lambda: "Darwin")

    assert not thread_priority.request_current_thread_high_priority(-5)
    assert not thread_priority.request_current_thread_realtime(5)
