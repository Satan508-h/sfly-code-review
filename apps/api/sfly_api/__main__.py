"""``python -m sfly_api`` —— uvicorn 启动器。

端口**必须**从 ``PORT`` 环境变量读：Render 会动态分配端口，写死 8000
的部署会一直返回 502，而且日志里看不出原因。

### 为什么不用 ``uvicorn.run()``

``uvicorn.run()`` 内部是 ``asyncio_run(server.serve(), loop_factory=config.get_loop_factory())``，
而 ``uvicorn/loops/asyncio.py`` 在 Windows 上把那个工厂**写死**成了
``ProactorEventLoop``（为了让 ``--reload`` / ``--workers`` 能用上子进程）。
Proactor 不支持 ``add_reader``，psycopg v3 的异步模式就是靠它实现的 ——
在 Windows 上原生跑这个入口，进程能起来、`/healthz` 也正常，
然后在第一次碰数据库时炸掉。

自己驱动 ``Server.serve()`` 只多三行，换来的是**循环的选择权归我们**。
代价是没有 ``--reload``（本来也没用，`workers=1`）和 uvicorn 的信号处理包装
（``Server.serve()`` 内部自己会装）。
"""

from __future__ import annotations

import uvicorn

from sfly_shared.aio import run
from sfly_shared.config import get_settings


def main() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        "sfly_api.main:app",
        host="0.0.0.0",  # noqa: S104 —— 容器内必须监听全网卡
        port=settings.port,
        # 完整模式下 api 是无状态的，可以多 worker；
        # 精简模式下 lite 会另起 uvicorn 并强制 workers=1（见 apps/lite）。
        workers=1,
        log_config=None,  # 交给 structlog，避免两套日志格式打架
        access_log=False,
    )
    run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
