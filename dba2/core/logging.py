"""Logging setup.

Kept intentionally small for Phase 0 — no file handler, no bot-specific
Windows console encoding workaround (that belongs to `bot/` once it exists
and actually prints player names with diacritics). `setup_logging()` is
idempotent-ish: callers (tools/, bot/, tests) invoke it once at process
start.
"""
from __future__ import annotations

import logging
import sys

from dba2.core.config import get_config


def setup_logging() -> None:
    level_name = get_config().log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
