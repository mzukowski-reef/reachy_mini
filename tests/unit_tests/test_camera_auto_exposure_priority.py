"""Tests for enabling the camera's auto-exposure priority on Windows.

The Windows camera control module talks to DirectShow through COM; these tests
replace it (or its COM libraries) so they run on any platform.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
import types
from typing import Any, cast
from unittest import mock

import gi
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import reachy_mini.media.media_server as media_server  # noqa: E402
from reachy_mini.media.media_server import GstMediaServer  # noqa: E402

Gst.init([])

CONTROLS_MODULE = "reachy_mini.media.camera_controls_windows"


def _make_server(enabled: bool = True) -> GstMediaServer:
    server = cast(GstMediaServer, object.__new__(GstMediaServer))
    server._logger = logging.getLogger("test_camera_auto_exposure_priority")
    server._camera_auto_exposure_priority = enabled
    server._video_ipc_enabled = True
    server._resolution = cast(Any, types.SimpleNamespace(value=(1920, 1080, 30)))
    server._processing_framerate = 12
    return server


def _fake_controls(set_auto_exposure_priority: Any) -> types.ModuleType:
    module = types.ModuleType(CONTROLS_MODULE)
    module.set_auto_exposure_priority = set_auto_exposure_priority  # type: ignore[attr-defined]
    return module


@pytest.mark.parametrize("enabled", [True, False])
def test_windows_source_waits_for_the_first_camera_buffer(enabled: bool) -> None:
    """The profile is applied on the first buffer only when the option is on."""
    server = _make_server(enabled)
    with mock.patch.object(media_server.Gst.ElementFactory, "make") as make:
        elements = server._build_windows_source("Reachy Mini Camera")

    pad = elements[0].get_static_pad.return_value
    if enabled:
        pad.add_probe.assert_called_once_with(
            Gst.PadProbeType.BUFFER,
            server._on_first_windows_camera_buffer,
            "Reachy Mini Camera",
        )
    else:
        pad.add_probe.assert_not_called()
    assert make.call_args_list[0] == mock.call("mfvideosrc")


def test_first_buffer_applies_the_profile_off_the_streaming_thread() -> None:
    """The probe removes itself and hands the COM calls to another thread."""
    server = _make_server()
    with mock.patch.object(media_server, "Thread") as thread:
        result = server._on_first_windows_camera_buffer(
            mock.Mock(), mock.Mock(), "Reachy Mini Camera"
        )

    assert result == Gst.PadProbeReturn.REMOVE
    thread.assert_called_once_with(
        target=server._apply_windows_camera_profile,
        args=("Reachy Mini Camera",),
        name="camera-profile",
        daemon=True,
    )
    thread.return_value.start.assert_called_once_with()


def test_profile_enables_auto_exposure_priority(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A camera that reports the enabled value logs success."""
    set_priority = mock.Mock(return_value=1)
    server = _make_server()
    with (
        mock.patch.dict(sys.modules, {CONTROLS_MODULE: _fake_controls(set_priority)}),
        caplog.at_level(logging.INFO, logger=server._logger.name),
    ):
        server._apply_windows_camera_profile("Reachy Mini Camera")

    set_priority.assert_called_once_with("Reachy Mini Camera", True)
    assert "Enabled auto-exposure priority" in caplog.text


@pytest.mark.parametrize(
    ("set_priority", "message"),
    [
        (mock.Mock(side_effect=OSError("HRESULT 0x8007007A")), "HRESULT 0x8007007A"),
        (mock.Mock(return_value=0), "reports auto-exposure priority 0"),
    ],
)
def test_profile_reports_a_camera_that_refuses_it(
    set_priority: mock.Mock, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected or ignored write is a warning, never silent success."""
    server = _make_server()
    with (
        mock.patch.dict(sys.modules, {CONTROLS_MODULE: _fake_controls(set_priority)}),
        caplog.at_level(logging.INFO, logger=server._logger.name),
    ):
        server._apply_windows_camera_profile("Reachy Mini Camera")

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert message in record.getMessage()


def test_camera_control_request_has_the_windows_size() -> None:
    """KSPROPERTY is 8-byte aligned in ks.h; drivers refuse the packed 36 bytes."""
    windll = mock.MagicMock()
    windll.ole32.CLSIDFromString.return_value = 0
    with (
        mock.patch.object(sys, "platform", "win32"),
        mock.patch.object(ctypes, "windll", windll, create=True),
        # Windows' LONG and ULONG are 32-bit.
        mock.patch.object(ctypes, "c_long", ctypes.c_int32),
        mock.patch.object(ctypes, "c_ulong", ctypes.c_uint32),
        mock.patch.dict(sys.modules),
    ):
        sys.modules.pop(CONTROLS_MODULE, None)
        controls = importlib.import_module(CONTROLS_MODULE)

    assert ctypes.sizeof(controls.KSPROPERTY) == 24
    assert ctypes.sizeof(controls.KSPROPERTY_CAMERACONTROL_S) == 40
