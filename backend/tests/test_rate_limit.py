"""接口限流（R3）测试。

覆盖四层：
1. 算法层：滑动窗口计数、窗口滑动后恢复、主体隔离、不限流分支
2. 路由层：路径 → 策略映射（昂贵/认证/普通/豁免/OPTIONS/非 API）
3. 主体层：登录态按用户、匿名按 IP、X-Forwarded-For 仅在信任代理时生效
4. 中间件层：超限短路返回 429（业务不被触达）、豁免路径放行、全局桶、指标计数
另含一条端到端用例：真实打 /api/auth/login 触发 429。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import rate_limit as rl
from app.config import settings
from app.diagnostics import collect_startup_warnings
from app.main import app
from app.metrics import get_metrics
from app.middleware import RateLimitMiddleware
from app.rate_limit import Policy, RateLimiter, get_rate_limiter
from app.security import create_access_token


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _rate_limit_on(monkeypatch):
    """限流在 conftest 里全局关掉；本文件显式打开并收紧阈值，前后清空计数。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_llm_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_llm_global_per_minute", 0)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_default_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_trust_proxy", False)
    get_rate_limiter().reset()
    get_metrics().reset()
    yield
    get_rate_limiter().reset()
    get_metrics().reset()


# ---------------- 1. 算法层 ----------------


def test_sliding_window_allows_then_blocks():
    """达到上限后拒绝，并在响应里给出剩余额度与等待时间。"""
    limiter = RateLimiter()
    policy = Policy("default", 2)

    first = limiter.check(policy, "u:1")
    second = limiter.check(policy, "u:1")
    blocked = limiter.check(policy, "u:1")

    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert blocked.allowed is False
    assert blocked.retry_after > 0
    assert blocked.scope == "user"


def test_sliding_window_recovers_after_window(monkeypatch):
    """窗口滑过一条记录后，额度精确恢复一条（滑窗而非固定窗口）。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: clock["t"])
    limiter = RateLimiter()
    policy = Policy("default", 1, window_seconds=60.0)

    assert limiter.check(policy, "u:1").allowed is True
    assert limiter.check(policy, "u:1").allowed is False

    clock["t"] += 61  # 越过窗口
    assert limiter.check(policy, "u:1").allowed is True


def test_subjects_are_isolated():
    """不同主体各自计数，互不占用额度。"""
    limiter = RateLimiter()
    policy = Policy("default", 1)

    assert limiter.check(policy, "u:1").allowed is True
    assert limiter.check(policy, "u:2").allowed is True
    assert limiter.check(policy, "u:1").allowed is False
    assert limiter.check(policy, "ip:10.0.0.9").allowed is True


def test_non_positive_limit_means_unlimited():
    """limit <= 0 视为不限流（便于用配置关掉某类接口）。"""
    limiter = RateLimiter()
    policy = Policy("default", 0)
    for _ in range(50):
        assert limiter.check(policy, "u:1").allowed is True


def test_global_bucket_blocks_across_subjects():
    """全局桶见底时，即使单主体额度充足也拒绝（防多账号绕过）。"""
    limiter = RateLimiter()
    policy = Policy("llm", 10, global_limit=2)

    assert limiter.check(policy, "u:1").allowed is True
    assert limiter.check(policy, "u:2").allowed is True
    third = limiter.check(policy, "u:3")

    assert third.allowed is False
    assert third.scope == "global"


# ---------------- 2. 路由层 ----------------


@pytest.mark.parametrize(
    "method, path, expected",
    [
        ("POST", "/api/rag/ask", "llm"),
        ("POST", "/api/rag/ask/stream", "llm"),
        ("POST", "/api/fetch", "llm"),
        ("POST", "/api/fetch/", "llm"),  # 尾斜杠同样命中
        ("POST", "/api/changes/monitor", "llm"),
        ("POST", "/api/report/export", "llm"),
        ("POST", "/api/kb/rebuild", "llm"),
        ("POST", "/api/auth/login", "auth"),
        ("POST", "/api/auth/register", "auth"),
        ("GET", "/api/competitors", "default"),
        ("GET", "/api/rag/sessions", "default"),
        ("GET", "/health", None),
        ("GET", "/metrics", None),
        ("GET", "/openapi.json", None),
        ("OPTIONS", "/api/rag/ask", None),  # 预检放行
        ("GET", "/", None),  # 非 /api 路径不参与限流
        # 同为 /api/rag/ask 但方法不符 → 落到普通策略，避免误伤
        ("GET", "/api/rag/ask", "default"),
    ],
)
def test_select_policy(method, path, expected):
    policy = rl.select_policy(method, path)
    assert (policy.name if policy else None) == expected


def test_select_policy_disabled(monkeypatch):
    """总开关关闭后一律不限流。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    assert rl.select_policy("POST", "/api/rag/ask") is None


# ---------------- 3. 主体层 ----------------


def _scope(headers=None, client=("10.0.0.1", 5555), method="POST", path="/api/rag/ask"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])
        ],
        "client": client,
    }


def test_subject_uses_user_id_when_authenticated():
    token = create_access_token(user_id=42, username="u42", role="user")
    subject = rl.subject_for(
        _scope([("authorization", f"Bearer {token}")]), Policy("llm", 1)
    )
    assert subject == "u:42"


def test_subject_falls_back_to_ip_without_token():
    assert rl.subject_for(_scope(), Policy("llm", 1)) == "ip:10.0.0.1"


def test_subject_falls_back_to_ip_on_invalid_token():
    """伪造/过期 token 不应被当成独立主体（否则改个串就能刷满额度）。"""
    subject = rl.subject_for(
        _scope([("authorization", "Bearer not-a-jwt")]), Policy("llm", 1)
    )
    assert subject == "ip:10.0.0.1"


def test_auth_policy_keys_by_ip_even_with_token():
    token = create_access_token(user_id=7, username="u7", role="user")
    policy = Policy("auth", 1, by_ip=True)
    subject = rl.subject_for(_scope([("authorization", f"Bearer {token}")]), policy)
    assert subject == "ip:10.0.0.1"


def test_forwarded_for_ignored_unless_trusted(monkeypatch):
    """直连暴露时不能信任 X-Forwarded-For，否则客户端可伪造主体绕过限流。"""
    headers = [("x-forwarded-for", "1.2.3.4")]
    assert rl.subject_for(_scope(headers), Policy("default", 1)) == "ip:10.0.0.1"

    monkeypatch.setattr(settings, "rate_limit_trust_proxy", True)
    assert rl.subject_for(_scope(headers), Policy("default", 1)) == "ip:1.2.3.4"


# ---------------- 4. 中间件层 ----------------

_NEXT_CALLS = {"n": 0}


async def _call_middleware(method="POST", path="/api/rag/ask", headers=None):
    """用「回显 200」的假 app 包一层限流中间件，直接观察短路行为。

    比走真实路由更稳：既不依赖数据库，也不会真的触发检索/LLM 调用。
    返回 (响应起始消息, 响应体, 业务被触达的次数)。
    """
    _NEXT_CALLS["n"] = 0

    async def dummy_app(scope, receive, send):
        _NEXT_CALLS["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RateLimitMiddleware(dummy_app)
    messages = []

    async def send(message):
        messages.append(message)

    await middleware(_scope(headers, method=method, path=path), None, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body = next(
        (m.get("body") for m in messages if m["type"] == "http.response.body"), b""
    )
    return start, body, _NEXT_CALLS["n"]


def _header(start, name: bytes) -> str | None:
    for key, value in start["headers"]:
        if key == name:
            return value.decode("latin-1")
    return None


async def test_middleware_passes_through_within_quota():
    start, _, _ = await _call_middleware()
    assert start["status"] == 200
    assert _NEXT_CALLS["n"] == 1


async def test_middleware_blocks_with_429_and_skips_business():
    """超限时返回 429，且**业务代码完全没被执行**（这正是省钱的关键）。"""
    for _ in range(3):  # llm 策略上限 3
        start, _, _ = await _call_middleware()
        assert start["status"] == 200

    start, body, _ = await _call_middleware()

    assert start["status"] == 429
    assert int(_header(start, b"retry-after")) > 0
    assert _header(start, b"x-ratelimit-remaining") == "0"
    assert _NEXT_CALLS["n"] == 0  # 第 4 次请求未触达业务
    assert b"rate_limited" in body


async def test_middleware_limits_per_user_not_globally():
    """两个用户各自有独立额度，互不影响。"""
    token_a = create_access_token(user_id=101, username="a", role="user")
    token_b = create_access_token(user_id=102, username="b", role="user")
    ha = [("authorization", f"Bearer {token_a}")]
    hb = [("authorization", f"Bearer {token_b}")]

    for _ in range(3):
        assert (await _call_middleware(headers=ha))[0]["status"] == 200
    assert (await _call_middleware(headers=ha))[0]["status"] == 429
    # B 用户不受 A 影响
    assert (await _call_middleware(headers=hb))[0]["status"] == 200


async def test_middleware_global_limit_returns_global_scope(monkeypatch):
    """全局桶见底时提示的是服务繁忙（而非「你调太快」），措辞与 scope 要能区分。"""
    monkeypatch.setattr(settings, "rate_limit_llm_per_minute", 10)
    monkeypatch.setattr(settings, "rate_limit_llm_global_per_minute", 1)

    token_a = create_access_token(user_id=201, username="a", role="user")
    token_b = create_access_token(user_id=202, username="b", role="user")

    assert (
        await _call_middleware(headers=[("authorization", f"Bearer {token_a}")])
    )[0]["status"] == 200
    start, body, _ = await _call_middleware(
        headers=[("authorization", f"Bearer {token_b}")]
    )

    assert start["status"] == 429
    assert b"global" in body
    assert "请求量过高".encode() in body


async def test_middleware_exempts_health_and_options(monkeypatch):
    """豁免路径与预检不受限流影响：探活失败会被误判成服务宕机。"""
    monkeypatch.setattr(settings, "rate_limit_default_per_minute", 1)
    for _ in range(5):
        start, _, _ = await _call_middleware(method="GET", path="/health")
        assert start["status"] == 200

    for _ in range(5):
        start, _, _ = await _call_middleware(method="OPTIONS", path="/api/rag/ask")
        assert start["status"] == 200


async def test_middleware_records_metrics():
    """被限流次数进入指标，便于判断是哪类接口在被刷。"""
    for _ in range(4):
        await _call_middleware()
    snapshot = get_metrics().snapshot()
    assert snapshot["rate_limited_total"] == 1
    assert snapshot["rate_limited_by_policy"]["llm"] == 1


# ---------------- 端到端：真实打登录接口 ----------------


async def test_login_returns_429_with_retry_after(client):
    """连续登录超限 → 429 + Retry-After（限流在业务之前生效）。"""
    import secrets

    username = f"rl_{secrets.token_hex(4)}"
    await client.post(
        "/api/auth/register", json={"username": username, "password": "goodpass123"}
    )
    # 注册也走 auth 策略，先清空再单独观察登录
    get_rate_limiter().reset()

    for _ in range(3):
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": "wrongpass1"}
        )
        assert resp.status_code == 401

    blocked = await client.post(
        "/api/auth/login", json={"username": username, "password": "goodpass123"}
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert blocked.json()["code"] == "rate_limited"
    assert blocked.json()["policy"] == "auth"


async def test_default_limit_applies_to_api_paths(client):
    """普通业务接口同样受限（阈值放松，只挡脚本刷接口）。"""
    for _ in range(3):
        resp = await client.get("/api/competitors")
        assert resp.status_code != 429
    resp = await client.get("/api/competitors")
    assert resp.status_code == 429


async def test_health_endpoint_stays_available_when_limited(client, monkeypatch):
    """开启限流后 /health 仍可无限探活。"""
    monkeypatch.setattr(settings, "rate_limit_default_per_minute", 1)
    for _ in range(5):
        resp = await client.get("/health")
        assert resp.status_code == 200


# ---------------- 启动告警 ----------------


def test_startup_warning_when_rate_limit_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    assert any("限流已关闭" in w for w in collect_startup_warnings())

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    assert not any("限流已关闭" in w for w in collect_startup_warnings())
