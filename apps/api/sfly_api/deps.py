"""依赖注入点 —— 路由拿 ``RunStore`` / ``TaskQueue`` 的唯一入口。

单独一个模块（而不是放在 ``main.py`` 里）是为了**断开循环导入**：路由要
``Depends(require_deps)``，而 ``main`` 要 import 路由。放在 main 里的话，
两边必须有一个人 import 一个还没执行完的模块。

顺带一个好处：单测只需要 import 这个小文件就能替换依赖，
不必把整个 app 装配起来。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from sfly_bus.factory import Dependencies


def get_deps(request: Request) -> Dependencies | None:
    """取本进程的依赖句柄。由 ``lifespan`` 写入 ``app.state.deps``。

    做成 FastAPI 依赖（而不是路由里直接 ``request.app.state.deps``）是为了
    **给单测一个干净的替换点**：``app.dependency_overrides[get_deps] = ...``
    就能让接口的测试不需要真的连 Postgres / Redis。

    没有它的话，这个测试只有两条路：要么起 TestClient 时跑 lifespan 去连
    真实依赖（单测从此需要 Docker，违背「< 10 秒、无外部依赖」的分层约定），
    要么在测试里手改 ``app.state``（可行，但那是绕过接口而不是使用接口，
    重构时会静默失效）。
    """
    deps: Dependencies | None = getattr(request.app.state, "deps", None)
    return deps


async def require_deps(deps: Annotated[Dependencies | None, Depends(get_deps)]) -> Dependencies:
    """依赖没装起来就 **503**，而不是让 ``None.store`` 炸成 500。

    503 是「现在干不了活、过会儿再来」的准确含义 —— 而 500 会让调用方以为
    是代码坏了。这条路径真实存在：lifespan 还没跑完（uvicorn 启动中、
    Render 冷启动）、或者把 app 挂到一个不转发 lifespan 的宿主上。
    """
    if deps is None:  # pragma: no cover —— 正常由 lifespan 保证
        raise HTTPException(status_code=503, detail="依赖未初始化")
    return deps
