"""进程存活心跳与优雅停机。

Dockerfile 的 HEALTHCHECK 探的是 ``/tmp/sfly-heartbeat`` 的 mtime，**不是**
进程是否存在。这个区别很重要：一个卡在死循环里的 Worker 进程照样存在，
但心跳会停 —— 那才是我们真正想探测的故障。

三个非 HTTP 服务（orchestrator / workers ×3）靠这个文件被 Docker 判定健康；
api 和 lite 另有 HTTP 探针，但同样维护心跳，方便统一排查。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sfly_shared.logging import get_logger, setup_logging

# 心跳文件的内容只有一个 mtime，不含敏感信息，也不是可预测路径下的可执行文件。
# 路径可由 SFLY_HEARTBEAT_PATH 覆盖，写在 /tmp 只是容器的默认值。
_CONTAINER_HEARTBEAT = "/tmp/sfly-heartbeat"  # noqa: S108
DEFAULT_HEARTBEAT_PATH = Path(os.environ.get("SFLY_HEARTBEAT_PATH", _CONTAINER_HEARTBEAT))
# Windows 上没有 /tmp，本地直接跑脚本时落到临时目录
if os.name == "nt" and not DEFAULT_HEARTBEAT_PATH.parent.exists():
    import tempfile

    DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "sfly-heartbeat"

log = get_logger(__name__)


class Heartbeat:
    """后台协程，定期 touch 心跳文件。"""

    def __init__(self, path: Path | None = None, interval_s: float = 15.0) -> None:
        self.path = path or DEFAULT_HEARTBEAT_PATH
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    def beat(self) -> None:
        """同步写一次。启动时先调一次，避免探针的 start_period 期间误判。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError as exc:  # 只读文件系统之类，不该让服务起不来
            log.warning("heartbeat.write_failed", path=str(self.path), error=str(exc))

    async def _loop(self) -> None:
        while True:
            self.beat()
            await asyncio.sleep(self.interval_s)

    async def start(self) -> None:
        self.beat()
        self._task = asyncio.create_task(self._loop(), name="heartbeat")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # 删掉心跳文件：让还在跑的探针立刻失败，而不是等 60 秒超时
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def shutdown_event() -> asyncio.Event:
    """返回一个在收到 SIGTERM / SIGINT 时被 set 的 Event。

    ``loop.add_signal_handler`` 在 Windows 上不支持，所以用 ``signal.signal``。
    用 ``call_soon_threadsafe`` 是因为信号处理器不在事件循环线程里跑，
    直接 ``event.set()`` 是线程不安全的。
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handler(signum: int, _frame: Any) -> None:
        log.info("signal.received", signal=signal.Signals(signum).name)
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(sig, _handler)
    return stop


async def run_service(
    name: str,
    body: Callable[[asyncio.Event], Awaitable[None]],
    *,
    heartbeat: Heartbeat | None = None,
) -> None:
    """服务进程的标准骨架：装日志 → 起心跳 → 跑主逻辑 → 优雅停机。

    四个 app 的 ``__main__.py`` 都用它，于是每个只剩十来行。
    ``body`` 收到一个 stop Event，应该在它被 set 时干净地返回。
    """
    from sfly_shared.config import get_settings

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    # 刻意**不**在这里记录 queue_backend / lock_backend：读这两个设置只允许
    # 发生在 sfly_bus/factory.py 里（CLAUDE.md 约定 #4）。这个共用骨架一行
    # 都不该知道自己跑在哪种拓扑下，日志也不行 —— 今天是一行日志，
    # 明天就会有人顺手在它旁边加一个 if。
    # 拓扑信息由 factory.open_dependencies() 的 `deps.opened` 日志给出。
    log.info(
        "service.starting",
        service=name,
        mode=settings.mode,
        llm_provider=settings.llm_provider,
    )

    hb = heartbeat or Heartbeat()
    stop = shutdown_event()

    await hb.start()
    try:
        await body(stop)
    except asyncio.CancelledError:
        log.info("service.cancelled", service=name)
        raise
    except Exception:
        log.exception("service.crashed", service=name)
        raise
    finally:
        await hb.stop()
        log.info("service.stopped", service=name)
