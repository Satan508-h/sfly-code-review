"""``python -m sfly_api`` —— uvicorn 启动器。

端口**必须**从 ``PORT`` 环境变量读：Render 会动态分配端口，写死 8000
的部署会一直返回 502，而且日志里看不出原因。
"""

from __future__ import annotations

import uvicorn

from sfly_shared.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "sfly_api.main:app",
        host="0.0.0.0",  # noqa: S104 —— 容器内必须监听全网卡
        port=settings.port,
        # 完整模式下 api 是无状态的，可以多 worker；
        # 精简模式下 lite 会另起 uvicorn 并强制 workers=1（见 apps/lite）。
        workers=1,
        log_config=None,  # 交给 structlog，避免两套日志格式打架
        access_log=False,
    )


if __name__ == "__main__":
    main()
