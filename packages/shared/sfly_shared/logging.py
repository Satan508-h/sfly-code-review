"""结构化日志。

每个进程在启动时调一次 ``setup_logging()``。日志带 ``task_id`` / ``worker_type``
上下文，这样 ``docker compose logs | grep <task_id>`` 能把跨容器的完整链路串起来 ——
分布式系统里这是最基本的可调试性要求。
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# 用 contextvar 而不是全局变量：单容器精简模式下多个 run 并发跑在同一个
# 事件循环里，全局变量会串台。
_ctx_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)
_ctx_worker: ContextVar[str | None] = ContextVar("worker_type", default=None)


def bind_task(task_id: str | None = None, worker_type: str | None = None) -> None:
    """给当前 async 任务绑定日志上下文。在消费每条消息时调用。"""
    if task_id is not None:
        _ctx_task_id.set(task_id)
    if worker_type is not None:
        _ctx_worker.set(worker_type)


def _inject_context(_logger: Any, _method: str, event_dict: dict) -> dict:
    if (tid := _ctx_task_id.get()) and "task_id" not in event_dict:
        event_dict["task_id"] = tid
    if (wt := _ctx_worker.get()) and "worker_type" not in event_dict:
        event_dict["worker_type"] = wt
    return event_dict


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # ensure_ascii=False：structlog 的 JSONRenderer 默认会把非 ASCII 转义成
    # 中文 这类序列，日志本身仍然是合法 JSON，但 grep 中文关键词
    # 就完全失效了 —— 而本项目大量日志字段是中文（status、reason 等）。
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 第三方库的 INFO 噪音太大，压掉
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "sfly") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
