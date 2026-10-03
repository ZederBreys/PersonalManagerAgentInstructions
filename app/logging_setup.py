"""Minimal standard-library logging setup."""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with a simple, readable format."""

    logging.basicConfig(
        level=logging.getLevelName(level),
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
    )
