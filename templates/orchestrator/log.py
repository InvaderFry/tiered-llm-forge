"""Logging configuration for the orchestrator pipeline."""

import logging
import sys
from pathlib import Path

LOG_FILE = Path("orchestrator.log")

_configured = False


def setup_logging(verbose=False):
    """Configure the orchestrator logger.

    INFO level prints to stdout with a minimal format that looks like the
    previous print() output. DEBUG level is enabled with ``--verbose`` and
    includes timestamps. A file handler always writes DEBUG-level output
    to ``orchestrator.log`` for post-mortem analysis.
    """
    global _configured
    if _configured:
        return
    _configured = True

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
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(file_handler)


def get_logger(name=None):
    """Return a child logger under the ``orchestrator`` namespace."""
    if name:
        return logging.getLogger(f"orchestrator.{name}")
    return logging.getLogger("orchestrator")
