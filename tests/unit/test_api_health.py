"""API 探针测试 —— Step 0 的验收标准就是这两个接口。

它同时是一道**回归闸**：``/healthz`` 是 Docker healthcheck 和 Render 存活检查
探的地址，这个测试挂了意味着所有容器都会开始重启。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sfly_api.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


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
