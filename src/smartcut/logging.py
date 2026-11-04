"""结构化日志：每条工具调用都可回放（argv、退出码、耗时、产物）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def configure_logging(
    log_file: Path | None = None,
    level: int = logging.INFO,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> None:
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # 长任务会持续追加 JSONL；轮转避免单个日志无限增长占满磁盘。
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        )
    else:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(message)s",
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
