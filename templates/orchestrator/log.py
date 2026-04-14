"""Logging configuration and timestamped log helpers for the orchestrator."""

import atexit
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import FORGE_LOGS_DIR

_configured = False
_footer_written = False
_run_file_handler = None


def _now():
    """Return the current local time as a timezone-aware datetime."""
    return datetime.now().astimezone()


def make_log_stamp(dt=None):
    """Return a collision-resistant timestamp for log filenames."""
    return (dt or _now()).strftime("%Y%m%dT%H%M%S%f")


def format_log_time(dt):
    """Format a timestamp consistently across all on-disk logs."""
    return dt.astimezone().isoformat(timespec="seconds")


def reserve_log_path(prefix, directory=None):
    """Reserve and return a unique log path using exclusive file creation."""
    directory = directory or FORGE_LOGS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    while True:
        path = Path(directory) / f"{prefix}-{make_log_stamp()}.log"
        try:
            with path.open("x", encoding="utf-8"):
                pass
            return path
        except FileExistsError:
            continue


def write_timestamped_log(path, body, started_at=None, ended_at=None):
    """Write a text log with explicit start/end markers."""
    started_at = started_at or _now()
    ended_at = ended_at or _now()
    body = body.rstrip()
    text = (
        f"Start time: {format_log_time(started_at)}\n\n"
        f"{body}\n\n"
        f"End time: {format_log_time(ended_at)}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_run_footer():
    """Append an end-time footer to the orchestrator run log exactly once."""
    global _footer_written
    if _footer_written or _run_file_handler is None:
        return

    stream = getattr(_run_file_handler, "stream", None)
    if stream is None or getattr(stream, "closed", False):
        return

    stream.write(f"\nEnd time: {format_log_time(_now())}\n")
    stream.flush()
    _footer_written = True


def setup_logging(verbose=False):
    """Configure the orchestrator logger.

    INFO level prints to stdout with a minimal format that looks like the
    previous print() output. DEBUG level is enabled with ``--verbose`` and
    includes timestamps. A file handler always writes DEBUG-level output
    to ``forgeLogs/orchestrator-<timestamp>.log`` for post-mortem analysis.
    Each run gets its own log file so reruns do not overwrite prior output.
    """
    global _configured, _run_file_handler
    if _configured:
        return
    _configured = True

    log_file = reserve_log_path("orchestrator")
    log_file.write_text(f"Start time: {format_log_time(_now())}\n\n", encoding="utf-8")

    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.DEBUG)

    # Console handler — terse by default, timestamps with --verbose
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    if verbose:
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    else:
        console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    # File handler — always DEBUG for post-mortem
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(file_handler)
    _run_file_handler = file_handler
    atexit.register(_write_run_footer)


def get_logger(name=None):
    """Return a child logger under the ``orchestrator`` namespace."""
    if name:
        return logging.getLogger(f"orchestrator.{name}")
    return logging.getLogger("orchestrator")
