"""Best-effort scheduling priorities for realtime media threads."""

from __future__ import annotations

import os
import platform
from threading import get_native_id

RTKIT_BUS_NAME = "org.freedesktop.RealtimeKit1"
RTKIT_OBJECT_PATH = "/org/freedesktop/RealtimeKit1"
RTKIT_INTERFACE = "org.freedesktop.RealtimeKit1"


def request_current_thread_high_priority(nice: int = -5) -> bool:
    """Raise the current thread's normal scheduler priority when permitted."""
    if platform.system() != "Linux":
        return False
    thread_id = get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, thread_id, nice)
        return True
    except (AttributeError, OSError):
        return _call_rtkit("MakeThreadHighPriority", "(ti)", thread_id, nice)


def request_current_thread_realtime(priority: int = 5) -> bool:
    """Prefer low SCHED_RR, falling back to a safe negative nice level."""
    if platform.system() != "Linux":
        return False
    thread_id = get_native_id()
    try:
        os.sched_setscheduler(
            thread_id,
            os.SCHED_RR,
            os.sched_param(priority),
        )
        return True
    except (AttributeError, OSError):
        if _call_rtkit("MakeThreadRealtime", "(tu)", thread_id, priority):
            return True
        return request_current_thread_high_priority(-10)


def _call_rtkit(method: str, signature: str, thread_id: int, value: int) -> bool:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        connection.call_sync(
            RTKIT_BUS_NAME,
            RTKIT_OBJECT_PATH,
            RTKIT_INTERFACE,
            method,
            GLib.Variant(signature, (thread_id, value)),
            None,
            Gio.DBusCallFlags.NONE,
            2_000,
            None,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
