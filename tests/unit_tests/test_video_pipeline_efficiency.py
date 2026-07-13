"""Regression tests for keeping camera processing below the capture rate."""

from __future__ import annotations

import logging
from threading import Lock
from unittest.mock import MagicMock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from reachy_mini.media.camera_constants import ReachyMiniLiteCamSpecs  # noqa: E402
from reachy_mini.media.camera_gstreamer import GStreamerCamera  # noqa: E402
from reachy_mini.media.media_server import GstMediaServer  # noqa: E402
from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient  # noqa: E402

Gst.init([])


def _make_server(monkeypatch) -> GstMediaServer:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("REACHY_MINI_PROCESSING_SIZE", "960x540")
    monkeypatch.setenv("REACHY_MINI_PROCESSING_FRAMERATE", "15")
    server = object.__new__(GstMediaServer)
    server._logger = logging.getLogger("test_video_pipeline_efficiency")
    server.camera_specs = ReachyMiniLiteCamSpecs()
    server._resolution = server.camera_specs.default_resolution
    server._video_ipc_enabled = True
    server._video_consumers = set()
    server._video_consumers_lock = Lock()
    server._video_demand_gate = None
    server._video_demand_selector = None
    server._video_camera_pad = None
    server._video_idle_pad = None
    server._loop = MagicMock()
    server._bus_sender = MagicMock()
    server._configure_processing_video()
    return server


def _factory_names(elements: list[Gst.Element]) -> list[str]:
    return [element.get_factory().get_name() for element in elements]


def test_v4l2_drops_compressed_frames_before_jpeg_decode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Drop excess compressed frames before invoking the JPEG decoder."""
    server = _make_server(monkeypatch)

    elements = server._build_v4l2_source("/dev/video-test")

    assert _factory_names(elements) == [
        "v4l2src",
        "capsfilter",
        "queue",
        "videorate",
        "capsfilter",
        "jpegdec",
        "videoscale",
        "videoconvert",
    ]
    limiter = elements[3]
    assert limiter.get_property("drop-only") is True
    assert limiter.get_property("max-rate") == 15
    limited_caps = elements[4].get_property("caps").to_string()
    assert "image/jpeg" in limited_caps
    assert "framerate=(fraction)15/1" in limited_caps


def test_shared_raw_stream_uses_processing_geometry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Publish configured processing geometry to all downstream branches."""
    server = _make_server(monkeypatch)

    caps = server._processing_raw_caps().to_string()

    assert "width=(int)960" in caps
    assert "height=(int)540" in caps
    assert "framerate=(fraction)15/1" in caps


def test_webrtc_client_does_not_upscale_to_camera_default() -> None:
    """Keep the daemon's negotiated geometry in the WebRTC client."""
    client = object.__new__(GstWebRTCClient)
    client._appsink_video = Gst.ElementFactory.make("appsink")
    client._doa = MagicMock()
    client._loop = MagicMock()
    client._bus_record = MagicMock()

    client._configure_video_sink_caps()

    caps = client._appsink_video.get_property("caps").to_string()
    assert caps == "video/x-raw, format=(string)BGR"


def test_ipc_client_does_not_upscale_to_camera_default() -> None:
    """Keep the daemon's negotiated geometry in the local IPC client."""
    client = object.__new__(GStreamerCamera)
    client._appsink_video = Gst.ElementFactory.make("appsink")

    client._configure_video_sink_caps()

    caps = client._appsink_video.get_property("caps").to_string()
    assert caps == "video/x-raw, format=(string)BGR"


def test_video_pipeline_omits_disabled_ipc_branch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Avoid all local BGR conversion when video IPC is disabled."""
    server = _make_server(monkeypatch)
    server._video_ipc_enabled = False
    pipeline = Gst.Pipeline.new("test-no-video-ipc")
    sink = Gst.ElementFactory.make("fakesink")
    pipeline.add(sink)

    server._configure_video("/dev/video-test", pipeline, sink)

    assert pipeline.get_by_name("queue_ipc") is None
    assert pipeline.get_by_name("ipc_videoconvert") is None
    assert pipeline.get_by_name("queue_webrtc") is not None
    assert pipeline.get_by_name("video_demand_gate") is not None
    selector = pipeline.get_by_name("video_demand_selector")
    assert selector is not None
    assert selector.get_property("active-pad") == server._video_idle_pad

    server._set_video_consumer_active("peer-a", active=True)
    assert selector.get_property("active-pad") == server._video_camera_pad

    server._set_video_consumer_active("peer-a", active=False)
    assert selector.get_property("active-pad") == server._video_idle_pad


def test_webrtc_demand_gate_wraps_expensive_jpeg_processing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Decode camera frames only while at least one WebRTC peer consumes video."""
    server = _make_server(monkeypatch)
    server._video_ipc_enabled = False

    elements = server._build_v4l2_source("/dev/video-test")

    assert _factory_names(elements) == [
        "v4l2src",
        "capsfilter",
        "queue",
        "videorate",
        "capsfilter",
        "valve",
        "jpegdec",
        "videoscale",
        "videoconvert",
    ]
    gate = elements[5]
    assert gate.get_property("drop") is True

    server._set_video_consumer_active("peer-a", active=True)
    assert gate.get_property("drop") is False

    server._set_video_consumer_active("peer-b", active=True)
    server._set_video_consumer_active("peer-a", active=False)
    assert gate.get_property("drop") is False

    server._set_video_consumer_active("peer-b", active=False)
    assert gate.get_property("drop") is True
