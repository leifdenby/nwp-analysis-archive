"""Graceful shutdown: finish the current write, then stop.

Killing the process in the middle of an append can leave an archive with an
``analysis_time`` entry whose data was never written. On SIGTERM/SIGINT the
update loops stop at the next safe point instead (between cycles and
datasets).
"""

import signal
import threading
from types import FrameType

from loguru import logger

_STOP = threading.Event()


def request_stop() -> None:
    """Ask the update loops to stop at the next safe point."""
    _STOP.set()


def check_stop_requested() -> bool:
    """Check whether a stop was requested.

    Returns
    -------
    bool
        True after :func:`request_stop` or a termination signal.
    """
    return _STOP.is_set()


def wait_for_stop(timeout: float) -> bool:
    """Sleep until the timeout or until a stop is requested.

    Parameters
    ----------
    timeout : float
        Seconds to wait at most.

    Returns
    -------
    bool
        True if a stop was requested.
    """
    return _STOP.wait(timeout)


def reset_stop() -> None:
    """Clear a requested stop (for tests)."""
    _STOP.clear()


def handle_signal(signum: int, frame: FrameType | None) -> None:
    """Request a stop when a termination signal arrives.

    Parameters
    ----------
    signum : int
        The signal number.
    frame : types.FrameType or None
        The interrupted frame (unused).
    """
    logger.warning(
        f"Received {signal.Signals(signum).name}: stopping after the current "
        "write"
    )
    request_stop()


def install_signal_handlers() -> None:
    """Install :func:`handle_signal` for SIGTERM and SIGINT."""
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
