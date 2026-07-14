import json
from threading import Event
from unittest import mock

from reachy_mini.media.media_server import GstMediaServer
from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient


def test_media_server_notifies_open_data_channel_when_audio_pipeline_is_ready():
    channel = mock.Mock()
    channel.get_property.return_value = mock.Mock(value_nick="open")
    server = GstMediaServer.__new__(GstMediaServer)
    server.close = mock.Mock()
    server._data_channels = {"peer": channel}
    server._logger = mock.Mock()

    assert server._notify_incoming_audio_ready("peer")
    event = json.loads(channel.emit.call_args.args[1])
    assert channel.emit.call_args.args[0] == "send-string"
    assert event == {"event": "incoming_audio_ready"}


def test_media_server_defers_readiness_until_data_channel_is_open():
    channel = mock.Mock()
    channel.get_property.return_value = mock.Mock(value_nick="connecting")
    server = GstMediaServer.__new__(GstMediaServer)
    server.close = mock.Mock()
    server._data_channels = {"peer": channel}
    server._logger = mock.Mock()

    assert not server._notify_incoming_audio_ready("peer")
    channel.emit.assert_not_called()


def test_webrtc_client_records_daemon_audio_readiness_event():
    client = GstWebRTCClient.__new__(GstWebRTCClient)
    client.cleanup = mock.Mock()
    client._loop = mock.Mock()
    client._bus_record = mock.Mock()
    client._incoming_audio_ready = Event()
    client.logger = mock.Mock()

    client._on_data_channel_message(
        mock.Mock(),
        json.dumps({"event": "incoming_audio_ready"}),
    )

    assert client.wait_for_incoming_audio_ready(timeout=0.0)


def test_webrtc_client_ignores_unrelated_data_channel_events():
    client = GstWebRTCClient.__new__(GstWebRTCClient)
    client.cleanup = mock.Mock()
    client._loop = mock.Mock()
    client._bus_record = mock.Mock()
    client._incoming_audio_ready = Event()
    client.logger = mock.Mock()

    client._on_data_channel_message(mock.Mock(), json.dumps({"event": "other"}))

    assert not client.wait_for_incoming_audio_ready(timeout=0.0)
