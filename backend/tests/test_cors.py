"""CORS 跨域白名单（R6）测试。

覆盖四层：
1. 配置解析：CORS_ORIGINS 兼容逗号分隔与 JSON 数组两种环境变量写法，空值归一为 []
2. 中间件行为：白名单命中/未命中、预检放行与拒绝、空白名单不放行任何来源、
   非浏览器请求（无 Origin 头）不受影响 —— 用按 main.py 相同方式注册的最小
   独立应用观察，避免测试结果依赖本地 .env 是否已配置 CORS_ORIGINS
3. 全局应用装配：main.py 确实把 CORSMiddleware 注册进栈，且参数来自 settings
4. 启动告警：白名单为空时提示（docstring 承诺的检查此前缺失）
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.diagnostics import collect_startup_warnings
from app.main import app

ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example.com"


def _make_settings(**overrides) -> Settings:
    """构造独立 Settings 实例测解析逻辑（显式传必填字段，不碰全局单例）。"""
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5433/x",
        jwt_secret="test-secret",
        **overrides,
    )


# ---------------- 1. 配置解析 ----------------


def test_parse_cors_comma_separated():
    assert _make_settings(cors_origins="http://a:5173, http://b:3000").cors_origins == [
        "http://a:5173",
        "http://b:3000",
    ]


def test_parse_cors_json_array():
    assert _make_settings(cors_origins='["http://a:5173", "http://b:3000"]').cors_origins == [
        "http://a:5173",
        "http://b:3000",
    ]


def test_parse_cors_empty_values_normalize_to_empty_list():
    assert _make_settings(cors_origins="").cors_origins == []
    assert _make_settings(cors_origins=None).cors_origins == []


def test_parse_cors_rejects_non_array_json():
    """JSON 写法必须是数组；传对象说明使用者误解了格式，应在启动时立刻报错。"""
    with pytest.raises(ValidationError):
        _make_settings(cors_origins='{"allow": "http://a"}')


def test_parse_cors_from_env_var(monkeypatch):
    """环境变量路径（与 init kwargs 不同源）：NoDecode 跳过 JSON 预解码后，
    逗号分隔字符串必须能到达 validator —— 否则启动即炸（上线前实测踩过）。"""
    monkeypatch.setenv("CORS_ORIGINS", "http://a:5173,http://b:3000")
    assert _make_settings().cors_origins == ["http://a:5173", "http://b:3000"]


def test_parse_cors_json_from_env_var(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["http://a:5173"]')
    assert _make_settings().cors_origins == ["http://a:5173"]


# ---------------- 2. 中间件行为 ----------------


def _build_cors_app(origins: list[str]) -> FastAPI:
    """按 main.py 的注册方式构造最小应用，观察 CORS 行为。"""
    cors_app = FastAPI()
    cors_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    @cors_app.get("/api/ping")
    async def ping() -> dict:
        return {"pong": True}

    return cors_app


async def _request(cors_app, method: str, path: str, headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=cors_app), base_url="http://test"
    ) as c:
        return await c.request(method, path, headers=headers or {})


async def test_preflight_from_allowed_origin():
    resp = await _request(
        _build_cors_app([ALLOWED_ORIGIN]),
        "OPTIONS",
        "/api/ping",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "GET" in resp.headers["access-control-allow-methods"]


async def test_preflight_rejects_disallowed_method():
    """预检声明的实际方法不在白名单内 → 拒绝（浏览器会拦下后续真实请求）。"""
    resp = await _request(
        _build_cors_app([ALLOWED_ORIGIN]),
        "OPTIONS",
        "/api/ping",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "TRACE",
        },
    )
    assert resp.status_code == 400


async def test_simple_request_from_allowed_origin_gets_acao():
    resp = await _request(
        _build_cors_app([ALLOWED_ORIGIN]), "GET", "/api/ping", {"Origin": ALLOWED_ORIGIN}
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


async def test_simple_request_from_foreign_origin_gets_no_acao():
    """白名单外的来源不放行：响应不带 ACAO 头，浏览器自行拦截。"""
    resp = await _request(
        _build_cors_app([ALLOWED_ORIGIN]), "GET", "/api/ping", {"Origin": FOREIGN_ORIGIN}
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_empty_whitelist_blocks_every_origin():
    """默认（白名单为空）不放行任何跨域 —— 与未引入 CORS 前的行为一致。"""
    for origin in (ALLOWED_ORIGIN, FOREIGN_ORIGIN):
        resp = await _request(
            _build_cors_app([]), "GET", "/api/ping", {"Origin": origin}
        )
        assert "access-control-allow-origin" not in resp.headers


async def test_request_without_origin_unaffected():
    """非浏览器请求（无 Origin 头，如 curl/服务间调用）不受 CORS 影响。"""
    resp = await _request(_build_cors_app([ALLOWED_ORIGIN]), "GET", "/api/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": True}
    assert "access-control-allow-origin" not in resp.headers


# ---------------- 3. 全局应用装配 ----------------


def test_main_app_registers_cors_from_settings():
    """main.py 确实注册了 CORSMiddleware，且参数与 settings 对齐。"""
    middleware = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert middleware.kwargs["allow_origins"] == settings.cors_origins
    # 认证走 Authorization 头而非 Cookie，固定关闭凭证以收紧面
    assert middleware.kwargs["allow_credentials"] is False
    assert middleware.kwargs["allow_methods"] == ["GET", "POST", "PUT", "PATCH", "DELETE"]


def test_cors_middleware_is_outermost():
    """CORS 应在最外层：保证 429 等错误响应也能带上跨域头。

    user_middleware 内部是 insert(0)：后注册的排在列表头部（即栈的最外层）。
    """
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is CORSMiddleware


# ---------------- 4. 启动告警 ----------------


def test_startup_warning_when_cors_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", [])
    assert any("CORS" in w for w in collect_startup_warnings())

    monkeypatch.setattr(settings, "cors_origins", [ALLOWED_ORIGIN])
    assert not any("CORS" in w for w in collect_startup_warnings())
