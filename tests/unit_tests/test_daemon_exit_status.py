"""Exit status of the daemon process after its FastAPI app started or failed to.

Without the robot's USB audio, the app's lifespan raises ``Reachy ALSA output
card was not found`` and uvicorn logs ``Application startup failed. Exiting.``.
``run_app`` awaits ``uvicorn.Server.serve()``, which then just returns, so the
daemon used to exit with status 0, as after a clean shutdown. ``uvicorn.run()``
exits with ``STARTUP_FAILURE`` instead, and so must the daemon: systemd's
``Restart=on-failure`` and the launchers that start the daemon need the status
to tell a failed start from a clean stop.

Both tests run the daemon's real entry point in a subprocess, in mockup
simulation without autostart, so no hardware is touched.
"""

import subprocess
import sys

import pytest
from uvicorn.main import STARTUP_FAILURE

# The daemon's entry point, with the app's audio preflight failing as it does
# when the robot's USB audio card is missing.
_DAEMON_WITHOUT_AUDIO_CARD = """
import sys

import reachy_mini.daemon.app.main as daemon_main


def missing_audio_card(args):
    raise RuntimeError("Reachy ALSA output card was not found (simulated)")


daemon_main._prepare_audio_output = missing_audio_card
sys.argv = ["reachy-mini-daemon", *sys.argv[1:]]
daemon_main.main()
"""

_DAEMON_ARGS = [
    "--mockup-sim",
    "--no-autostart",
    "--fastapi-host",
    "127.0.0.1",
    "--fastapi-port",
    "0",
    "--dataset-update-interval",
    "0",
]


def _run_daemon(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"The daemon was still running after {exc.timeout} s")


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit status {result.returncode}, stderr tail:\n{result.stderr[-3000:]}"


def test_daemon_exits_with_startup_failure_status_when_its_app_fails_to_start() -> None:
    """A failed app startup ends the process with uvicorn's startup failure status."""
    result = _run_daemon(
        [sys.executable, "-c", _DAEMON_WITHOUT_AUDIO_CARD, *_DAEMON_ARGS]
    )

    assert "Application startup failed" in result.stderr, _describe(result)
    assert result.returncode == STARTUP_FAILURE, _describe(result)


def test_daemon_exits_with_status_zero_after_a_normal_shutdown() -> None:
    """A daemon that started and then stopped normally still exits with 0."""
    result = _run_daemon(
        [
            sys.executable,
            "-m",
            "reachy_mini.daemon.app.main",
            *_DAEMON_ARGS,
            "--no-media",
            # Nothing sends health checks, so the daemon stops itself.
            "--timeout-health-check",
            "3",
        ]
    )

    assert "Application startup complete." in result.stderr, _describe(result)
    assert "Application shutdown complete." in result.stderr, _describe(result)
    assert result.returncode == 0, _describe(result)
