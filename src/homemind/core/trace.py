"""全链路追踪与结构化日志（对齐 FR-OPS-001）。

核心能力：
1. traceId 贯穿一次请求的接入 → 意图 → Agent → 工具 → 应答全过程。
2. 日志结构化（JSON 行），可被 ELK / Loki 直接采集。
3. 计时上下文 span()，自动记录每个环节耗时——这是排查"到底哪一环慢"的关键。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# traceId 用 ContextVar 存储，保证异步并发下不同请求互不串台
_trace_id: ContextVar[str] = ContextVar("homemind_trace_id", default="-")


def new_trace_id() -> str:
    """生成新的 traceId。"""
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str) -> None:
    """设置当前上下文的 traceId。"""
    _trace_id.set(trace_id)


def get_trace_id() -> str:
    """获取当前上下文的 traceId。"""
    return _trace_id.get()


class JsonFormatter(logging.Formatter):
    """JSON 结构化日志格式器。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": record.getMessage(),
        }
        # 允许调用方通过 extra 塞入任意结构化字段
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """初始化日志：控制台输出 + JSON 格式。

    显式指定 UTF-8，避免中文 Windows 下 GBK 编码导致的豆腐块。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger("homemind")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """获取 logger，统一挂在 homemind 命名空间下。"""
    return logging.getLogger(f"homemind.{name}")


@contextmanager
def span(name: str, logger: logging.Logger | None = None, **fields: Any) -> Iterator[None]:
    """计时上下文，自动记录环节耗时。

    用法：
        with span("intent.parse", logger, user_input=text):
            ...
    输出：{"event": "span", "span": "intent.parse", "duration_ms": 12.3, ...}
    """
    log = logger or get_logger("trace")
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        log.error(
            "span failed: %s", name,
            extra={"extra_fields": {
                "event": "span", "span": name, "status": "error",
                "duration_ms": round(duration_ms, 2), "error": str(exc), **fields,
            }},
        )
        raise
    else:
        duration_ms = (time.perf_counter() - start) * 1000
        log.info(
            "span ok: %s", name,
            extra={"extra_fields": {
                "event": "span", "span": name, "status": "ok",
                "duration_ms": round(duration_ms, 2), **fields,
            }},
        )
