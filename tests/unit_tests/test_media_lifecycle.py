"""Exercise media teardown with real GStreamer and synthetic sources only."""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_mini.daemon.daemon import Daemon
from reachy_mini.io.protocol import DaemonState
from reachy_mini.media import media_server as media_module
from reachy_mini.media.camera_constants import ReachyMiniLiteCamSpecs
from reachy_mini.media.gstreamer_utils import handle_default_bus_message
from reachy_mini.media.media_server import GLib, Gst, GstMediaServer

Gst.init([])


def wait_for(predicate: Callable[[], bool]) -> None:
    """Wait for asynchronous cleanup with a bounded failure deadline."""
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for media lifecycle transition")
        time.sleep(0.005)


def terminal_message(pipeline: Gst.Pipeline, kind: str) -> Gst.Message:
    """Create a terminal notification without touching physical devices."""
    if kind == "eos":
        return Gst.Message.new_eos(pipeline)
    error = GLib.Error.new_literal(
        Gst.ResourceError.quark(), "synthetic audio disconnect", Gst.ResourceError.READ
    )
    return Gst.Message.new_error(pipeline, error, "test source")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[GstMediaServer]:
    """Use the production owner and bus loop with hardware-free source builders."""
    monkeypatch.setattr(
        media_module, "get_video_device", lambda: ("test", ReachyMiniLiteCamSpecs())
    )

    def sink(_self: GstMediaServer, pipeline: Gst.Pipeline) -> Gst.Element:
        result = Gst.ElementFactory.make("fakesink", "video_sink")
        result.set_property("sync", False)
        pipeline.add(result)
        return result

    def video(
        _self: GstMediaServer,
        _path: str,
        pipeline: Gst.Pipeline,
        sink: Gst.Element,
    ) -> None:
        source = Gst.ElementFactory.make("videotestsrc", "video_source")
        source.set_property("is-live", True)
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property(
            "caps",
            Gst.Caps.from_string("video/x-raw,width=16,height=16,framerate=30/1"),
        )
        pipeline.add(source)
        pipeline.add(caps)
        assert source.link(caps)
        assert caps.link(sink)

    def audio(
        _self: GstMediaServer, pipeline: Gst.Pipeline, _sink: Gst.Element
    ) -> None:
        source = Gst.ElementFactory.make("audiotestsrc", "audio_source")
        source.set_property("is-live", True)
        sink = Gst.ElementFactory.make("fakesink", "audio_sink")
        sink.set_property("sync", False)
        pipeline.add(source)
        pipeline.add(sink)
        assert source.link(sink)

    monkeypatch.setattr(GstMediaServer, "_configure_webrtc", sink)
    monkeypatch.setattr(GstMediaServer, "_configure_video", video)
    monkeypatch.setattr(GstMediaServer, "_configure_audio", audio)
    instance = GstMediaServer()
    try:
        yield instance
    finally:
        instance.close()
    assert not instance._thread_bus_calls.is_alive()


def start_sender(server: GstMediaServer) -> Gst.Pipeline:
    """Start and wait for both synthetic branches to reach PLAYING."""
    server.start()
    pipeline = server._pipeline_sender
    assert pipeline is not None
    assert pipeline.get_state(2 * Gst.SECOND)[1] == Gst.State.PLAYING
    return pipeline


@pytest.mark.parametrize("kind", ["error", "eos"])
def test_default_handler_keeps_draining_after_terminal_message(kind: str) -> None:
    """Logging a terminal event must not silently unsubscribe a live producer."""
    pipeline = Gst.Pipeline.new("helper-test")
    assert handle_default_bus_message(
        logging.getLogger("test"), terminal_message(pipeline, kind), pipeline
    )


@pytest.mark.parametrize("kind", ["error", "eos"])
def test_source_failure_stops_all_sender_branches_and_flushes_bus(
    server: GstMediaServer, kind: str
) -> None:
    """A failed source cannot leave the other branch generating orphan messages."""
    pipeline = start_sender(server)
    bus = pipeline.get_bus()
    watch_id = server._bus_watches[pipeline]
    assert bus.post(terminal_message(pipeline, kind))
    for _ in range(500):
        bus.post(Gst.Message.new_application(pipeline, Gst.Structure.new_empty("tick")))
    wait_for(lambda: server._pipeline_sender is None)
    assert pipeline.get_state(0)[1] == Gst.State.NULL
    assert pipeline.get_by_name("video_source").get_state(0)[1] == Gst.State.NULL
    assert pipeline.get_by_name("audio_source").get_state(0)[1] == Gst.State.NULL
    assert not bus.have_pending()
    assert not server._bus_watches
    assert not server._pending_pipeline_stops
    assert GLib.MainContext.default().find_source_by_id(watch_id) is None


def test_one_hundred_restarts_release_bus_sources_and_timers(
    server: GstMediaServer,
) -> None:
    """Repeated ownership cycles leave no old watches or pending timers."""
    assert not server._bus_watches  # Construction must not build a spare pipeline.
    for _ in range(100):
        pipeline = start_sender(server)
        source_ids = [server._bus_watches[pipeline], server._latency_source_id]
        server.stop()
        server.stop()
        assert pipeline.get_state(0)[1] == Gst.State.NULL
        assert not pipeline.get_bus().have_pending()
        assert not server._bus_watches
        for source_id in source_ids:
            assert source_id is not None
            assert GLib.MainContext.default().find_source_by_id(source_id) is None
    server.close()
    server.close()
    with pytest.raises(RuntimeError, match="closed"):
        server.start()


@pytest.mark.parametrize("kind", ["error", "eos"])
def test_incoming_playback_failure_only_releases_its_peer(
    server: GstMediaServer, kind: str
) -> None:
    """Retiring incoming audio must preserve the sender and other clients."""
    sender = start_sender(server)
    peers = {}
    for peer_id in ("one", "two"):
        pipeline = Gst.parse_launch("audiotestsrc is-live=true ! fakesink sync=false")
        server._watch_pipeline(pipeline)
        server._incoming_audio[peer_id] = {"playback_pipeline": pipeline}
        pipeline.set_state(Gst.State.PLAYING)
        peers[peer_id] = pipeline
    server._pipeline_playback = peers["one"]
    peers["one"].get_bus().post(terminal_message(peers["one"], kind))
    wait_for(lambda: peers["one"] not in server._bus_watches)
    assert peers["one"].get_state(0)[1] == Gst.State.NULL
    assert server._pipeline_playback is None
    assert set(server._incoming_audio) == {"two"}
    assert sender.get_state(0)[1] == Gst.State.PLAYING
    assert peers["two"].get_state(Gst.SECOND)[1] == Gst.State.PLAYING


def test_sound_eos_releases_player_without_stopping_sender(
    server: GstMediaServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Natural completion of a local sound frees its bus and playback graph."""
    sender = start_sender(server)
    monkeypatch.setattr(
        server, "_build_audiosink_tee_bin", lambda: Gst.ElementFactory.make("fakesink")
    )
    sound = tmp_path / "silence.wav"
    with wave.open(str(sound), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(bytes(1600))
    server.play_sound(str(sound))
    wait_for(lambda: server._playbin is None)
    assert set(server._bus_watches) == {sender}
    assert sender.get_state(0)[1] == Gst.State.PLAYING


def test_late_error_callback_cannot_stop_replacement_pipeline(
    server: GstMediaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued old failure is harmless when stop/start wins the race."""
    old = start_sender(server)
    entered, proceed, finished = Event(), Event(), Event()
    original = server._finish_pipeline

    def delayed(pipeline: Gst.Pipeline) -> bool:
        entered.set()
        if not proceed.wait(5):
            return False
        try:
            return original(pipeline)
        finally:
            finished.set()

    monkeypatch.setattr(server, "_finish_pipeline", delayed)
    old.get_bus().post(terminal_message(old, "error"))
    try:
        assert entered.wait(5)
        replacement = start_sender(server)
    finally:
        proceed.set()
    assert finished.wait(5)
    assert replacement is server._pipeline_sender
    assert replacement.get_state(0)[1] == Gst.State.PLAYING
    assert old.get_state(0)[1] == Gst.State.NULL


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[Daemon]:
    """Build a daemon with fake hardware identity, media and signalling."""
    monkeypatch.setattr("reachy_mini.utils.hardware_id.get_hardware_id", lambda: None)
    instance = Daemon(no_media=True)
    instance._media_server = MagicMock(spec=GstMediaServer)
    instance._media_server.error = None
    instance._stop_central_signaling_relay = AsyncMock()
    instance.ws_server = MagicMock()
    yield instance
    instance._media_server = None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [DaemonState.ERROR, DaemonState.STOPPED])
async def test_stop_without_backend_still_releases_media(
    daemon: Daemon, state: DaemonState
) -> None:
    """An absent backend or repeated stop cannot bypass media teardown."""
    daemon._status.state = state
    assert daemon.backend is None
    await daemon.stop(goto_sleep_on_stop=False)
    daemon._media_server.stop.assert_called_once()
    daemon._stop_central_signaling_relay.assert_awaited_once()
    assert daemon._thread_event_publish_status.is_set()


@pytest.mark.asyncio
async def test_backend_failure_releases_media_and_preserves_error(
    daemon: Daemon,
) -> None:
    """A motor failure shuts media down without attempting a motor movement."""
    backend = MagicMock()
    daemon.backend = backend
    await daemon._handle_backend_failure(backend, RuntimeError("motor disconnected"))
    assert daemon.backend is None
    assert daemon.status().state == DaemonState.ERROR
    assert daemon.status().error == "motor disconnected"
    assert daemon.media_released
    daemon._media_server.stop.assert_called_once()
    daemon._stop_central_signaling_relay.assert_awaited_once()
    daemon.ws_server.stop.assert_called_once()
    backend.goto_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_old_backend_failure_does_not_release_new_backend_media(
    daemon: Daemon,
) -> None:
    """Late errors from an old backend cannot tear down its replacement."""
    daemon.backend = MagicMock()
    await daemon._handle_backend_failure(MagicMock(), RuntimeError("old failure"))
    daemon._media_server.stop.assert_not_called()
    daemon._stop_central_signaling_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_recovers_from_media_failure_without_manual_release(
    daemon: Daemon,
) -> None:
    """A stopped sender must be visible and recoverable through the media API."""
    daemon._media_server.error = "audio disconnected"
    daemon._start_central_signaling_relay = AsyncMock()
    assert daemon.media_released
    assert daemon.status().media_released

    def recovered() -> None:
        daemon._media_server.error = None

    daemon._media_server.start.side_effect = recovered
    await daemon.acquire_media()
    daemon._media_server.start.assert_called_once()
    assert not daemon.media_released
    assert not daemon.status().media_released


def test_start_failure_cleans_partial_pipeline(
    server: GstMediaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed construction cannot retain its already-installed bus watch."""

    def fail(*args: object) -> None:
        raise RuntimeError("missing audio element")

    monkeypatch.setattr(server, "_configure_audio", fail)
    with pytest.raises(RuntimeError, match="missing audio element"):
        server.start()
    assert server._pipeline_sender is None
    assert not server._bus_watches
    assert not server._pending_pipeline_stops


def test_error_recovery_clears_old_failure(server: GstMediaServer) -> None:
    """Explicit start rebuilds a failed sender and clears its error marker."""
    failed = start_sender(server)
    failed.get_bus().post(terminal_message(failed, "error"))
    wait_for(lambda: server._pipeline_sender is None)
    assert "synthetic audio disconnect" in server.error
    replacement = start_sender(server)
    assert replacement != failed
    assert server.error is None


@pytest.mark.asyncio
async def test_running_backend_thread_failure_releases_media(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real start() thread wrapper and its asynchronous error path."""
    backend = MagicMock()
    backend.ready = Event()
    backend.ready.set()
    fail = Event()

    def run() -> None:
        if not fail.wait(5):
            return
        raise RuntimeError("motor disconnected during operation")

    backend.wrapped_run.side_effect = run
    monkeypatch.setattr(daemon, "_setup_backend", lambda **_kwargs: backend)
    monkeypatch.setattr(daemon, "_publish_status", lambda: None)
    monkeypatch.setattr(
        "reachy_mini.daemon.daemon.WSServer", lambda **_kwargs: MagicMock()
    )
    daemon._start_central_signaling_relay = AsyncMock()
    try:
        result = await daemon.start(mockup_sim=True, wake_up_on_start=False)
        assert result == DaemonState.RUNNING
        fail.set()
        wait_for(lambda: daemon.backend is None)
        daemon.backend_run_thread.join(timeout=5)
        assert not daemon.backend_run_thread.is_alive()
        assert daemon.status().error == "motor disconnected during operation"
        daemon._media_server.stop.assert_called_once()
        daemon._stop_central_signaling_relay.assert_awaited_once()
    finally:
        fail.set()
        daemon.backend_run_thread.join(timeout=5)


@pytest.mark.asyncio
async def test_backend_failure_before_ready_keeps_original_error(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early worker failure cannot become an AttributeError or restart media."""
    backend = MagicMock()
    backend.error = None
    backend.wrapped_run.side_effect = RuntimeError("motor failed before ready")
    cleanup_finished = Event()
    original = daemon._handle_backend_failure

    async def handle_failure(failed_backend: object, error: Exception) -> None:
        try:
            await original(failed_backend, error)
        finally:
            cleanup_finished.set()

    def ready_timeout(**_kwargs: object) -> bool:
        assert cleanup_finished.wait(5)
        return False

    backend.ready.wait.side_effect = ready_timeout
    monkeypatch.setattr(daemon, "_handle_backend_failure", handle_failure)
    monkeypatch.setattr(daemon, "_setup_backend", lambda **_kwargs: backend)
    monkeypatch.setattr(daemon, "_publish_status", lambda: None)
    monkeypatch.setattr(
        "reachy_mini.daemon.daemon.WSServer", lambda **_kwargs: MagicMock()
    )
    try:
        assert (
            await daemon.start(mockup_sim=True, wake_up_on_start=False)
            == DaemonState.ERROR
        )
        assert daemon.status().error == "motor failed before ready"
        daemon._media_server.start.assert_not_called()
    finally:
        daemon.backend_run_thread.join(timeout=5)
