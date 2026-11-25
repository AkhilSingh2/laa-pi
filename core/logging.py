"""Loguru setup. Pipeline components log via `log`; durable audit entries go to the events table."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

log = logger


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan> - {message}",
    )
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(log_dir / "laa-pi.log", level="DEBUG", rotation="5 MB", retention=5, enqueue=True)
