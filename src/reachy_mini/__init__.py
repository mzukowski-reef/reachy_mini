"""Reachy Mini SDK."""

from importlib.metadata import version

from reachy_mini.runtime_concurrency import configure_runtime_concurrency

configure_runtime_concurrency()

from reachy_mini.apps.app import ReachyMiniApp  # noqa: E402
from reachy_mini.reachy_mini import ReachyMini  # noqa: E402

__version__ = version("reachy_mini")

__all__ = ["ReachyMini", "ReachyMiniApp", "__version__"]
