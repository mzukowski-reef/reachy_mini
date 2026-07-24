"""Tests for the REST audio-config router.

Hits the real ReSpeaker USB board, so each test is gated behind
`@pytest.mark.audio` like the helpers in `test_audio_control_utils.py`.
"""

from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reachy_mini.daemon.app.routers import audio_config
from reachy_mini.media.audio_control_utils import init_respeaker_usb


@pytest.fixture
def client() -> TestClient:
    """Bare FastAPI test client with just the audio-config router mounted."""
    app = FastAPI()
    app.include_router(audio_config.router)
    return TestClient(app)


@pytest.mark.audio
def test_read_audio_parameter_round_trip(client: TestClient) -> None:
    """The REST route should return a JSON-decoded view of a board parameter."""
    response = client.get("/audio/config/parameter/AUDIO_MGR_MIC_GAIN")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "AUDIO_MGR_MIC_GAIN"
    assert isinstance(body["values"], list)
    assert len(body["values"]) >= 1


@pytest.mark.audio
def test_apply_audio_config_identity_write(client: TestClient) -> None:
    """Identity write (read → apply → verify) must succeed on the real board."""
    respeaker = init_respeaker_usb()
    assert respeaker is not None, "Reachy Mini Audio board is required."
    try:
        original = respeaker.read_values("PP_MIN_NS")
    finally:
        respeaker.close()
    assert original is not None

    response = client.post(
        "/audio/config/apply",
        json={
            "config": [{"name": "PP_MIN_NS", "values": list(original)}],
            "verify": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"applied": True}


def test_apply_audio_config_preserves_integer_register_values(
    client: TestClient,
) -> None:
    """JSON integers must reach the uint8/int32 USB writer as integers."""
    respeaker = Mock()
    respeaker.apply_audio_config.return_value = True

    with patch.object(audio_config, "init_respeaker_usb", return_value=respeaker):
        response = client.post(
            "/audio/config/apply",
            json={
                "config": [
                    {"name": "AEC_ASROUTONOFF", "values": [1]},
                    {"name": "AUDIO_MGR_OP_L", "values": [7, 3]},
                    {"name": "PP_MIN_NS", "values": [0.15]},
                ],
                "verify": True,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"applied": True}
    config = respeaker.apply_audio_config.call_args.args[0]
    assert config == [
        ("AEC_ASROUTONOFF", [1]),
        ("AUDIO_MGR_OP_L", [7, 3]),
        ("PP_MIN_NS", [0.15]),
    ]
    assert all(isinstance(value, int) for value in config[0][1])
    assert all(isinstance(value, int) for value in config[1][1])
    assert isinstance(config[2][1][0], float)
    respeaker.close.assert_called_once_with()


def test_read_unknown_parameter_returns_404(client: TestClient) -> None:
    """Unknown parameter name must not crash; 404 surfaces it cleanly.

    Runs without the @pytest.mark.audio gate because `init_respeaker_usb()`
    returning None on a board-less machine is also a valid outcome — the
    test passes in either case (404 from the name lookup or 503 from the
    missing board).
    """
    response = client.get("/audio/config/parameter/DEFINITELY_NOT_A_PARAM")
    assert response.status_code in (404, 503)
