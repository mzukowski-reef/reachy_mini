"""Focused tests for the WebRTC media server audio boundary."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock, call

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from reachy_mini.media.media_server import GstMediaServer  # noqa: E402

Gst.init([])


def _make_server() -> GstMediaServer:
    server = cast(GstMediaServer, object.__new__(GstMediaServer))
    server._logger = logging.getLogger("test_media_server_audio")
    server._loop = MagicMock()
    server._bus_sender = MagicMock()
    return server


def _factory_names(pipeline: Gst.Pipeline) -> set[str]:
    names: set[str] = set()
    iterator = pipeline.iterate_elements()
    while True:
        result, element = iterator.next()
        if result != Gst.IteratorResult.OK:
            break
        factory = element.get_factory()
        if factory is not None:
            names.add(factory.get_name())
    return names


def test_make_audio_capture_caps_chain_emits_f32le_16k_stereo() -> None:
    """Capture should cross WebRTC with explicit stable audio caps."""
    server = _make_server()

    chain = GstMediaServer._make_audio_capture_caps_chain(server)

    assert [element.get_factory().get_name() for element in chain] == [
        "audioconvert",
        "audioresample",
        "capsfilter",
    ]
    caps = chain[-1].get_property("caps").to_string()
    assert "audio/x-raw" in caps
    assert "format=(string)F32LE" in caps
    assert "rate=(int)16000" in caps
    assert "channels=(int)2" in caps
    assert "layout=(string)interleaved" in caps


def test_capture_pipeline_does_not_insert_software_aec() -> None:
    """Echo cancellation belongs to the XMOS input route, not GStreamer."""
    server = _make_server()

    def build_audio_source() -> Gst.Element:
        return Gst.ElementFactory.make("audiotestsrc")

    server._build_audio_source = build_audio_source
    pipeline = Gst.Pipeline.new("test_capture_without_software_aec")
    sink = Gst.ElementFactory.make("identity")
    pipeline.add(sink)

    server._configure_audio(pipeline, sink)

    factories = _factory_names(pipeline)
    assert "webrtcdsp" not in factories
    assert "webrtcechoprobe" not in factories


def test_windows_capture_source_uses_exclusive_mode() -> None:
    """Windows capture should avoid shared-mode channel folding."""
    audiosrc = MagicMock()

    GstMediaServer._configure_windows_capture_source(audiosrc, "reachy-input")

    assert audiosrc.set_property.call_args_list == [
        call("device", "reachy-input"),
        call("exclusive", True),
    ]
