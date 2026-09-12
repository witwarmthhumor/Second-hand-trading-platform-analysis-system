"""认证加固与 LLM 降级测试（R4 / R1）。

- 密码强度策略：长度、字母+数字、弱口令黑名单
- 登录失败锁定：连续失败达阈值 → 429 + Retry-After，成功登录清零
- LLM 降级：错误分类翻译、未配置时的明确提示
"""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.security import validate_password_strength
from app.services import login_guard


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_login_state():
    """每个用例前后清空锁定状态，避免相互污染。"""
    login_guard.reset()
    yield
    login_guard.reset()


# ---------------- 密码强度 ----------------


@pytest.mark.parametrize(
    "password, ok",
    [
        ("pass12345", True),
        ("Secret123", True),
        ("abc123", False),  # 长度不足 8
        ("abcdefgh", False),  # 无数字
        ("12345678", False),  # 无字母 + 弱口令
        ("password", False),  # 黑名单
        ("ADMIN123", False),  # 黑名单（大写归一后命中 admin123）
    ],
)
def test_validate_password_strength(password, ok):
    if ok:
        assert validate_password_strength(password) == password
    else:
        with pytest.raises(ValueError):
            validate_password_strength(password)


async def test_register_rejects_pure_digit_password(client):
    """纯数字密码 → 422（强度校验）。"""
    resp = await client.post(
        "/api/auth/register", json={"username": "pure_digit", "password": "12345678"}
    )
    assert resp.status_code == 422


async def test_register_rejects_no_digit_password(client):
    """纯字母密码 → 422。"""
    resp = await client.post(
        "/api/auth/register", json={"username": "no_digit_u", "password": "abcdefgh"}
    )
    assert resp.status_code == 422


async def test_change_password_enforces_strength(client):
    """改密同样受强度约束：弱新密码 → 422（在旧密码校验之前就被拦下）。"""
    import secrets

    username = f"cp_{secrets.token_hex(4)}"
    reg = await client.post(
        "/api/auth/register", json={"username": username, "password": "goodpass123"}
    )
    token = reg.json()["access_token"]
    resp = await client.post(
        "/api/auth/change-password",
        json={"old_password": "goodpass123", "new_password": "abcdefgh"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------- 登录失败锁定 ----------------


async def test_login_lockout_after_repeated_failures(client, monkeypatch):
    """连续失败达阈值 → 后续登录 429 且带 Retry-After（即使密码正确也拒绝）。"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "login_max_failures", 3)
    monkeypatch.setattr(app_settings, "login_lockout_minutes", 10)

    import secrets

    username = f"lock_{secrets.token_hex(4)}"
    await client.post(
        "/api/auth/register", json={"username": username, "password": "goodpass123"}
    )

    for _ in range(3):
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": "wrongpass1"}
        )
        assert resp.status_code == 401

    # 第 4 次：已被锁定 → 429，且即使密码正确也拒绝
    locked = await client.post(
        "/api/auth/login", json={"username": username, "password": "goodpass123"}
    )
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


async def test_login_success_clears_failure_counter(client, monkeypatch):
    """失败若干次后成功登录，应清空计数，不会误锁。"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "login_max_failures", 3)

    import secrets

    username = f"clear_{secrets.token_hex(4)}"
    await client.post(
        "/api/auth/register", json={"username": username, "password": "goodpass123"}
    )
    for _ in range(2):
        await client.post(
            "/api/auth/login", json={"username": username, "password": "wrongpass1"}
        )
    ok = await client.post(
        "/api/auth/login", json={"username": username, "password": "goodpass123"}
    )
    assert ok.status_code == 200
    assert login_guard.check_locked(username) is None


def test_login_guard_lock_and_expiry(monkeypatch):
    """锁定计数达阈值 → 进入锁定；到期自动解锁。"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "login_max_failures", 2)
    monkeypatch.setattr(app_settings, "login_lockout_minutes", 0)  # 立即过期，验证解锁分支

    login_guard.register_failure("u1")
    assert login_guard.check_locked("u1") is None  # 未达阈值
    login_guard.register_failure("u1")
    # 锁 0 分钟 → 已过期，自动清理
    assert login_guard.check_locked("u1") is None

    monkeypatch.setattr(app_settings, "login_lockout_minutes", 5)
    login_guard.register_failure("u2")
    login_guard.register_failure("u2")
    remaining = login_guard.check_locked("u2")
    assert remaining is not None and remaining > 0
    login_guard.register_success("u2")
    assert login_guard.check_locked("u2") is None


# ---------------- LLM 降级 ----------------


@pytest.mark.parametrize(
    "status, keyword",
    [
        (401, "凭证无效"),
        (402, "余额不足"),
        (403, "余额不足"),
        (429, "限流"),
        (503, "服务异常"),
    ],
)
def test_llm_error_hint_maps_status_codes(status, keyword):
    """HTTP 状态码被翻译成可读原因（用户能据此判断该充值还是等待）。"""
    from app.services.rag_service import _llm_error_hint

    req = httpx.Request("POST", "https://api.example.com/chat/completions")
    resp = httpx.Response(status, request=req, text="err")
    exc = httpx.HTTPStatusError("boom", request=req, response=resp)
    assert keyword in _llm_error_hint(exc)


def test_llm_error_hint_timeout():
    """超时被单独识别。"""
    from app.services.rag_service import _llm_error_hint

    assert "超时" in _llm_error_hint(httpx.TimeoutException("t"))


async def test_fallback_returns_not_configured_message(monkeypatch):
    """未配置 LLM 时，兜底路径给出明确提示而非空回答。"""
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "is_llm_configured", lambda: False)
    answer = await rag_service.llm_fallback("随便问问")
    assert "未配置" in answer
