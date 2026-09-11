"""结构化日志：统一格式、请求级上下文（request_id）与访问日志。

设计要点：
- 一个进程只调用一次 ``setup_logging()``；测试/脚本可重复调用（幂等）。
- 两种输出形态：人类可读单行文本（默认）与 JSON 行（``LOG_JSON=true``）。
- ``request_id`` 存于 contextvar，由请求中间件写入，日志格式自动带上，
  异步并发下各请求互不串号。
- 额外字段（如 duration_ms / status / path）通过 ``extra={"extra_fields": {...}}``
  传入，JSON 形态下平铺到顶层，文本形态下追加为 k=v 后缀。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from app.config import settings

# 请求级上下文：request_id（由中间件写入；非请求上下文下为 "-"）
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# 额外结构化字段的日志记录属性名（logging 允许的 extra 键，避免与内置属性冲突）
_EXTRA_ATTR = "extra_fields"

# 标准 LogRecord 内置属性，序列化时需排除，避免脏字段进 JSON
_RESERVED_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        _EXTRA_ATTR,
    }
)


def get_request_id() -> str:
    """返回当前请求的 request_id（非请求上下文返回 "-"）。"""
    return _request_id.get()


def set_request_id(value: str) -> None:
    """设置当前上下文的 request_id。"""
    _request_id.set(value)


def _collect_extra(record: logging.LogRecord) -> dict:
    """取出 record 上的额外字段（显式经 extra_fields 传入 + 其它自定义属性）。"""
    extra: dict = dict(getattr(record, _EXTRA_ATTR, {}) or {})
    for key, value in record.__dict__.items():
        if key not in _RESERVED_ATTRS and not key.startswith("_"):
            extra.setdefault(key, value)
    return extra


def _timestamp(record: logging.LogRecord) -> str:
    """本地时区的 ISO 时间戳（到毫秒）。"""
    return (
        datetime.fromtimestamp(record.created, tz=UTC)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


class TextFormatter(logging.Formatter):
    """人类可读单行格式：时间 级别 [rid] logger: 消息  k=v ..."""

    def format(self, record: logging.LogRecord) -> str:
        rid = get_request_id()
        base = f"{_timestamp(record)} {record.levelname:<7} [{rid}] {record.name}: {record.getMessage()}"
        extra = _collect_extra(record)
        if extra:
            suffix = " ".join(f"{k}={v}" for k, v in extra.items())
            base = f"{base}  {suffix}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


class JsonFormatter(logging.Formatter):
    """JSON 行格式：一行一个对象，便于日志采集系统检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": get_request_id(),
        }
        payload.update(_collect_extra(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """配置根 logger 与 uvicorn logger；重复调用幂等。"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else TextFormatter())

    root = logging.getLogger()
    # 幂等：已由本模块配置过则仅更新级别，避免重复挂 handler 造成日志翻倍
    if not any(getattr(h, "_app_configured", False) for h in root.handlers):
        for existing in list(root.handlers):
            root.removeHandler(existing)
        handler._app_configured = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 自带 access 日志格式与本模块不一致，且我们已有访问日志中间件，故关掉
    # 其 access logger（仅保留 error），避免同一请求打印两行。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)
