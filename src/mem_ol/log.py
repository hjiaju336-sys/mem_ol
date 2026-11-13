"""统一日志模块。

所有模块通过 ``get_logger(__name__)`` 获取 logger，日志统一向 root logger
传播并汇总到同一组 handler：

- 控制台：人类可读的单行文本
- 文件：JSON Lines（默认 ``logs/mem_ol.log``，10MB 轮转，保留 5 份）

标准字段：``ts``、``level``、``logger``、``message``、``thread_id``、
``user_id``、``batch_id``、``trace_id``、``event``；其余字段写入 ``extra``。
``event`` 统一使用 ``模块.动作`` 命名，例如 ``memory.batch.done``。

使用方式::

    from mem_ol.log import get_logger, log_context

    logger = get_logger(__name__)
    with log_context(thread_id=thread_id, batch_id=batch_id):
        logger.info(
            "批次处理完成",
            extra={"event": "memory.batch.done", "cost_ms": 120},
        )
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "bind_log_context",
    "clear_log_context",
    "get_logger",
    "log_context",
    "reset_logging",
    "setup_logging",
]

_STANDARD_FIELDS: tuple[str, ...] = (
    "thread_id",
    "user_id",
    "batch_id",
    "trace_id",
    "event",
)

_LOG_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "mem_ol_log_context", default={}
)

_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def bind_log_context(**fields: Any) -> None:
    """把字段绑定到当前上下文，后续日志自动携带。"""
    _LOG_CONTEXT.set({**_LOG_CONTEXT.get(), **fields})


def clear_log_context() -> None:
    """清空当前上下文。"""
    _LOG_CONTEXT.set({})


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """在 with 块内绑定日志上下文。"""
    token = _LOG_CONTEXT.set({**_LOG_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = dict(_LOG_CONTEXT.get())
    for key in _STANDARD_FIELDS:
        if hasattr(record, key):
            fields[key] = getattr(record, key)

    extra = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_ATTRS and key not in _STANDARD_FIELDS
    }
    if extra:
        fields["extra"] = extra
    return fields


class TextFormatter(logging.Formatter):
    """控制台可读格式。"""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        line = (
            f"{timestamp}.{int(record.msecs):03d} | "
            f"{record.levelname:<8} | {record.name} | {record.getMessage()}"
        )

        fields = _record_fields(record)
        if fields:
            parts = []
            for key, value in fields.items():
                rendered = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, default=str)
                )
                parts.append(f"{key}={rendered}")
            line += " | " + " ".join(parts)

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """文件聚合格式：一行一个 JSON 对象。"""

    def format(self, record: logging.LogRecord) -> str:
        fields = _record_fields(record)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in _STANDARD_FIELDS:
            payload[key] = fields.pop(key, None)

        if fields:
            extras = dict(fields.pop("extra", {}))
            extras.update(fields)
            if extras:
                payload["extra"] = extras

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _close_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def setup_logging(
    level: str | None = None,
    log_file: str | None = None,
    *,
    console: bool = True,
    force: bool = False,
) -> None:
    """初始化全局日志。应用启动时调用一次，重复调用默认无副作用。"""
    root = logging.getLogger()
    if getattr(root, "_mem_ol_configured", False) and not force:
        return

    if force:
        _close_handlers(root)

    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    path = Path(log_file or os.getenv("LOG_FILE", "logs/mem_ol.log"))
    path.parent.mkdir(parents=True, exist_ok=True)

    root.setLevel(log_level)

    if console:
        stream = sys.stdout
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        console_handler = logging.StreamHandler(stream)
        console_handler.setFormatter(TextFormatter())
        root.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    logging.captureWarnings(True)

    # Uvicorn 默认自带 handler，清掉并交给 root 统一汇总。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # 第三方库只保留 warning 及以上，避免汇总日志被加载/请求噪音淹没。
    for name in (
        "httpx",
        "httpx2",
        "urllib3",
        "neo4j",
        "openai",
        "sentence_transformers",
        "transformers",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    root._mem_ol_configured = True  # type: ignore[attr-defined]


def reset_logging() -> None:
    """关闭并移除 root handler，主要用于测试。"""
    root = logging.getLogger()
    _close_handlers(root)
    root._mem_ol_configured = False  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger；不要自行添加 handler。"""
    return logging.getLogger(name)
