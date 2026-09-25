"""API 探针测试 —— Step 0 的验收标准就是这两个接口。

它同时是一道**回归闸**：``/healthz`` 是 Docker healthcheck 和 Render 存活检查
探的地址，这个测试挂了意味着所有容器都会开始重启。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sfly_api.main import create_app, get_deps
from sfly_bus.health import CheckResult, CheckStatus, HealthReport

# --------------------------------------------------------------------------- #
# 依赖桩
#
# 单测的分层约定是「无 Docker、无密钥、< 10 秒」。真实探测要连 Postgres 和
# Redis，所以这里替换掉 —— 真实的探测逻辑由 tests/unit/bus/test_health.py
# 用假 URL 覆盖（连不上的路径同样是真实路径）。
# --------------------------------------------------------------------------- #


class _StubDeps:
    def __init__(self, *checks: CheckResult) -> None:
        self._checks = list(checks)

    async def probe(self) -> HealthReport:
        return HealthReport(self._checks)

    async def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    """**刻意不用 ``with TestClient(app)``。**

    用 ``with`` 会触发 ``lifespan``，而 ``lifespan`` 的第一件事就是
    ``open_dependencies()`` —— 真的去连 Postgres 和 Redis。单测从此依赖
    Docker，而且在没起容器的机器上会红成一片，正好毁掉「pytest 在裸机上
    也能绿」这条分层约定。

    不跑 lifespan 的代价是 ``app.state.deps`` 不会被写入 —— 但下面的
    ``dependency_overrides`` 正好把唯一的读取点换掉了，所以路由照常工作。
    真实的 open/close 逻辑由 ``tests/unit/bus/test_health.py`` 直接测
    ``open_dependencies`` 覆盖。
    """
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: _StubDeps(
        CheckResult("postgres", CheckStatus.OK, "PostgreSQL 16.4", 1.2),
        CheckResult("redis", CheckStatus.OK, "Redis 7.4.1", 0.8),
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.unit
def test_healthz_is_minimal_and_never_touches_dependencies(client: TestClient) -> None:
    """``/healthz`` 是存活探针，**故意不探依赖**。

    依赖挂了不该让容器被重启 —— 那只会把一次数据库抖动放大成一次全站重启，
    且因为同时重启的容器都要重连，恢复反而更慢。依赖状态在 ``/api/health`` 看。
    """
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "sfly-api"
    # 只有这三个字段，加依赖探测就会变成就绪探针，语义就错了
    assert set(body) == {"ok", "service", "uptime_s"}


@pytest.mark.unit
def test_api_health_exposes_config(client: TestClient) -> None:
    """``/api/health`` 回显当前配置。

    这是排查「本地是这样、线上是那样」的第一站：一眼看出跑的是 redis 还是
    memory 队列、mock 还是 deepseek。前端首页也读它。
    """
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] in {"full", "lite"}
    assert body["config"]["queue_backend"] in {"redis", "memory"}
    assert body["config"]["llm_provider"] in {"mock", "deepseek", "openai"}
    assert body["config"]["wait_strategy"] in {"interrupt", "poll"}
    assert body["config"]["conflict_resolver"] in {"rules", "llm"}


@pytest.mark.unit
def test_cors_preflight_allows_configured_origin(client: TestClient) -> None:
    """精简模式下前端在 Vercel、后端在 Render，跨域是硬需求。

    没有这个头，浏览器的 EventSource 会被直接拦掉，而且报错信息很不直观。
    """
    origin = "http://localhost:5173"  # .env.example 里的默认值
    r = client.options(
        "/api/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.unit
def test_unknown_origin_is_not_allowed(client: TestClient) -> None:
    """反面用例：不能图省事配成 ``*``。"""
    r = client.options(
        "/api/health",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"
    assert r.headers.get("access-control-allow-origin") != "*"


@pytest.mark.unit
def test_openapi_is_served_under_api_prefix(client: TestClient) -> None:
    """文档路径也挂在 /api 下，保持前缀统一 —— 否则 nginx 的直通代理会漏掉它。"""
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"].startswith("sfly")


# --------------------------------------------------------------------------- #
# M0：依赖探测
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_health_reports_each_dependency(client: TestClient) -> None:
    """探测结果按名字展平，前端不需要自己遍历数组。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert set(checks) == {"postgres", "redis"}
    assert checks["postgres"]["status"] == "ok"
    assert checks["postgres"]["ok"] is True
    assert "PostgreSQL" in checks["postgres"]["detail"]
    assert checks["redis"]["latency_ms"] >= 0


@pytest.mark.unit
def test_dependency_down_yields_503_not_500() -> None:
    """依赖挂了必须是 **503**，而且响应体里带着诊断信息。

    这是这个接口最容易被写错的地方：让异常冒出去，调用方拿到 500 和一个
    堆栈，反而看不出是哪个依赖坏了。更糟的是前端把它显示成「无法连接后端」——
    后端明明是好的。
    """
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: _StubDeps(
        CheckResult("postgres", CheckStatus.OK, "PostgreSQL 16.4", 1.0),
        CheckResult("redis", CheckStatus.DOWN, "ConnectionRefusedError: 拒绝连接", 500.0),
    )
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["redis"]["ok"] is False
    # 单个依赖挂了不该污染另一个的结果
    assert body["checks"]["postgres"]["ok"] is True
    app.dependency_overrides.clear()


@pytest.mark.unit
def test_skipped_dependency_keeps_overall_ok() -> None:
    """精简模式下 Redis 是 ``skipped``，整体必须仍然是 ``ok``。

    三态而不是布尔的全部理由都在这条断言里：把「本就不需要」当成「不可达」，
    线上演示站会在完全健康的状态下常亮红灯。
    """
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: _StubDeps(
        CheckResult("postgres", CheckStatus.OK, "PostgreSQL 16.4", 1.0),
        CheckResult("redis", CheckStatus.SKIPPED, "精简模式无需 Redis", 0.0),
    )
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["checks"]["redis"]["status"] == "skipped"
    app.dependency_overrides.clear()


@pytest.mark.unit
def test_health_without_lifespan_returns_503_with_a_readable_detail() -> None:
    """lifespan 没跑过时（依赖从未初始化）给一条能读的错误，而不是 AttributeError。

    正常情况下 uvicorn 保证 lifespan 会跑，所以这是一个防御分支 ——
    但它防的是一个真实发生过的场景：把 app 直接挂到别的 ASGI 容器或测试
    工具上，那些宿主不一定会转发 lifespan 事件。
    """
    r = TestClient(create_app()).get("/api/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert r.json()["detail"] == "依赖未初始化"
