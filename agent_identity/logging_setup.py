"""Logging setup: console + timestamped file in ``output/``."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_CONFIGURED = False
ROOT_LOGGER = "agent_identity"


def setup_logging(level: str = "INFO", output_dir: str = "output") -> Path | None:
    """Configure the package logger once. Returns the log file path (if created)."""
    global _CONFIGURED
    logger = logging.getLogger(ROOT_LOGGER)
    if _CONFIGURED:
        return None
    logger.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_path: Path | None = None
    try:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        log_path = out / f"showcase_{datetime.now():%Y%m%d_%H%M%S}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:  # read-only FS etc. - console logging still works
        logger.warning("File logging disabled: %s", exc)

    logger.propagate = False
    _CONFIGURED = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the package logger."""
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")
