"""Regression tests for keeping camera processing below the capture rate."""

from __future__ import annotations

import logging
from threading import Lock
from unittest.mock import MagicMock

import gi

import reachy_mini.media.media_server as media_server_module

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
    server._incoming_audio = {}
    server._playbin_wobbler_valve = None
    server._head_wobbler = None
    server._media_profile_enabled = False
    server._media_profile_source_id = None
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
        "avdec_mjpeg",
        "videoscale",
        "videoconvert",
    ]
    source = elements[0]
    assert source.get_property("do-timestamp") is True
    queue = elements[2]
    assert queue.get_name() == "source_mjpeg_queue"
    assert int(queue.get_property("leaky")) == 2
    assert queue.get_property("max-size-buffers") == 1
    assert queue.get_property("max-size-bytes") == 0
    assert queue.get_property("max-size-time") == 0
    assert queue.get_property("flush-on-eos") is True
    limiter = elements[3]
    assert limiter.get_property("drop-only") is True
    assert limiter.get_property("max-rate") == 15
    assert limiter.get_property("qos") is True
    limited_caps = elements[4].get_property("caps").to_string()
    assert "image/jpeg" in limited_caps
    assert "framerate=(fraction)15/1" in limited_caps
    decoder = elements[5]
    assert decoder.get_property("qos") is True
    assert int(decoder.get_property("lowres")) == 1


def test_video_streaming_threads_use_nice_without_idle_scheduler(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Keep video below inference without an idle scheduling policy."""
    server = _make_server(monkeypatch)
    element = MagicMock()
    src_pad = MagicMock()
    element.get_static_pad.return_value = src_pad
    sched_setscheduler = MagicMock()
    setpriority = MagicMock()
    monkeypatch.setattr(media_server_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(media_server_module, "get_native_id", lambda: 1234)
    monkeypatch.setattr(
        media_server_module.os,
        "sched_setscheduler",
        sched_setscheduler,
    )
    monkeypatch.setattr(media_server_module.os, "setpriority", setpriority)

    server._lower_streaming_thread_priority_once(element, label="test-video")

    src_pad.add_probe.assert_called_once()
    probe_type, callback, user_data = src_pad.add_probe.call_args.args
    assert probe_type == Gst.PadProbeType.BUFFER
    assert callback(MagicMock(), MagicMock(), user_data) == Gst.PadProbeReturn.REMOVE
    sched_setscheduler.assert_not_called()
    setpriority.assert_called_once_with(
        media_server_module.os.PRIO_PROCESS,
        1234,
        media_server_module.VIDEO_THREAD_NICE,
    )


def test_mjpeg_decoder_uses_quarter_resolution_for_480p_processing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Use scaled IDCT instead of decoding the 1080p source at full detail."""
    server = _make_server(monkeypatch)
    monkeypatch.setenv("REACHY_MINI_PROCESSING_SIZE", "480x270")
    server._configure_processing_video()

    decoder = server._build_mjpeg_decoder()

    assert decoder.get_factory().get_name() == "avdec_mjpeg"
    assert int(decoder.get_property("lowres")) == 2


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
    assert client._appsink_video.get_property("sync") is False


def test_webrtc_client_bounds_received_media_backlog() -> None:
    """Discard stale decoded media before conversion and application delivery."""
    client = object.__new__(GstWebRTCClient)
    client._pipeline_record = Gst.Pipeline.new("test-receive-backlog")
    client._appsink_video = Gst.ElementFactory.make("appsink")
    client._appsink_audio = Gst.ElementFactory.make("appsink")
    client._loop = MagicMock()
    client._bus_record = MagicMock()
    client._configure_video_sink_caps()
    client._appsink_video.set_property("drop", True)
    client._appsink_video.set_property("max-buffers", 1)
    client._pipeline_record.add(client._appsink_video)
    source = Gst.ElementFactory.make("fakesrc")
    assert source is not None
    client._pipeline_record.add(source)
    pad = MagicMock()
    pad.get_name.return_value = "video_0"
    pad.link.side_effect = source.get_static_pad("src").link

    client._webrtcsrc_pad_added_cb(MagicMock(), pad)

    queue = client._pipeline_record.get_by_name("receive_video_queue")
    assert queue is not None
    assert int(queue.get_property("leaky")) == 2
    assert queue.get_property("max-size-buffers") == 1
    assert queue.get_property("max-size-bytes") == 0
    assert queue.get_property("max-size-time") == 0
    assert queue.get_property("flush-on-eos") is True
    assert all(
        element.get_factory().get_name() != "videorate"
        for element in client._iterate_gst(client._pipeline_record.iterate_elements())
    )


def test_webrtc_client_buffers_rtp_scheduler_jitter() -> None:
    """Keep complete keyframe bursts when the local process is descheduled."""
    client = object.__new__(GstWebRTCClient)
    client._doa = MagicMock()
    client._loop = MagicMock()
    client._bus_record = MagicMock()
    source = Gst.Bin.new("test-webrtc-source")
    webrtcbin = Gst.ElementFactory.make("webrtcbin", "webrtcbin0")
    assert source is not None
    assert webrtcbin is not None
    source.add(webrtcbin)

    client._configure_webrtcbin(source)

    assert webrtcbin.get_property("latency") == 200


def test_webrtc_vp8_encoder_limits_reference_error_propagation(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Recover from a damaged VP8 reference without waiting for the default GOP."""
    server = _make_server(monkeypatch)
    encoder = Gst.ElementFactory.make("vp8enc")
    assert encoder is not None

    server._encoder_setup(MagicMock(), "peer-a", "video_0", encoder)

    assert encoder.get_property("keyframe-max-dist") == 30
    assert int(encoder.get_property("error-resilient")) == 1
    assert encoder.get_property("deadline") == 1
    assert encoder.get_property("cpu-used") == 8
    assert encoder.get_property("threads") == 1
    assert encoder.get_property("dropframe-threshold") == 30


def test_webrtc_opus_encoder_is_profiled_with_a_stable_thread_label(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Attribute the internal audio encoder cost instead of reporting audio_0."""
    server = _make_server(monkeypatch)
    encoder = Gst.ElementFactory.make("opusenc")
    assert encoder is not None
    server._label_streaming_thread_once = MagicMock()
    server._profile_media_pad = MagicMock()

    server._encoder_setup(MagicMock(), "peer-a", "audio_0", encoder)

    assert encoder.get_property("complexity") == server.TX_OPUS_COMPLEXITY
    server._label_streaming_thread_once.assert_called_once_with(
        encoder,
        label="opus:peer-a",
    )
    server._profile_media_pad.assert_called_once_with(
        encoder,
        label="opus_encoded",
    )


def test_incoming_audio_pipeline_discards_stale_playback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Bound playback debt so CPU starvation cannot trigger catch-up bursts."""
    server = _make_server(monkeypatch)
    appsrc = Gst.ElementFactory.make("appsrc")
    queue = Gst.ElementFactory.make("queue")
    sink = Gst.ElementFactory.make("fakesink")
    assert appsrc is not None
    assert queue is not None
    assert sink is not None

    server._configure_incoming_audio_appsrc(appsrc)
    server._configure_incoming_audio_queue(queue)
    server._configure_incoming_audio_sink(sink)

    assert appsrc.get_property("block") is False
    assert appsrc.get_property("max-buffers") == 20
    assert appsrc.get_property("max-bytes") == 0
    assert appsrc.get_property("max-time") == 350 * Gst.MSECOND
    assert int(appsrc.get_property("leaky-type")) == 2
    assert int(queue.get_property("leaky")) == 2
    assert queue.get_property("max-size-buffers") == 20
    assert queue.get_property("max-size-bytes") == 0
    assert queue.get_property("max-size-time") == 350 * Gst.MSECOND
    assert queue.get_property("flush-on-eos") is True
    assert sink.get_property("max-lateness") == 300 * Gst.MSECOND
    assert sink.get_property("qos") is True


def test_disabled_wobbler_drops_audio_before_conversion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Avoid convert/resample work while daemon-side wobbling is disabled."""
    server = _make_server(monkeypatch)

    valve = server._make_wobbler_valve()

    assert valve.get_property("drop") is True
    assert int(valve.get_property("drop-mode")) == 1


def test_wobbler_toggle_updates_live_audio_branches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Open and close all existing wobbler gates without pipeline rebuilds."""
    server = _make_server(monkeypatch)
    playbin_valve = server._make_wobbler_valve()
    incoming_valve = server._make_wobbler_valve()
    server._playbin_wobbler_valve = playbin_valve
    server._incoming_audio = {
        "peer": {"wobbler_valve": incoming_valve},
    }

    server._set_wobbler_valves_enabled(True)
    assert playbin_valve.get_property("drop") is False
    assert incoming_valve.get_property("drop") is False

    server._set_wobbler_valves_enabled(False)
    assert playbin_valve.get_property("drop") is True
    assert incoming_valve.get_property("drop") is True


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
    queue_webrtc = pipeline.get_by_name("queue_webrtc")
    assert queue_webrtc is not None
    assert queue_webrtc.get_property("leaky") == 2
    assert queue_webrtc.get_property("max-size-buffers") == 1
    assert queue_webrtc.get_property("max-size-bytes") == 0
    assert queue_webrtc.get_property("max-size-time") == 0
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
        "avdec_mjpeg",
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
