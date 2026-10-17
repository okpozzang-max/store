"""Logging configuration (PRD 11).

Provides a single ``configure_logging`` helper and a ``get_logger`` accessor.
Logs go to stderr; a file handler is attached under ``logs/`` when writable.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False


def configure_logging(level: str | None = None, log_dir: Path | None = None) -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(log_level)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # Best-effort file handler.
    try:
        target_dir = log_dir or (Path.cwd() / "logs")
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target_dir / "app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # If we cannot write a log file, continue with stderr only.
        root.warning("Could not create log file; logging to stderr only.")

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
