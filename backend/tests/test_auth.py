"""认证 API 测试：注册 / 登录 / 当前用户 / 修改密码 / 权限。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def authed_client(client):
    """注册一个随机用户名并登录，返回带 token 的 client + user 信息。"""
    import secrets

    username = f"user_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client, resp.json()["user"]
    client.headers.pop("Authorization", None)


async def test_register_and_login_flow(client):
    """注册成功返回 token，且可用该账号登录。"""
    import secrets

    username = f"reg_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "pass12345"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == username
    assert body["user"]["role"] == "user"

    # 用同样凭据登录
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "pass12345"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == username


async def test_register_duplicate_username(client):
    """重复用户名注册返回 409。"""
    import secrets

    username = f"dup_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "pass12345"}
    )
    assert resp.status_code == 201
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "pass12345"}
    )
    assert resp.status_code == 409


async def test_login_wrong_password(client):
    """错误密码返回 401。"""
    import secrets

    username = f"wp_{secrets.token_hex(4)}"
    await client.post(
        "/api/auth/register", json={"username": username, "password": "correct123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "wrongpassword"}
    )
    assert resp.status_code == 401


async def test_login_unknown_user(client):
    """不存在的用户返回 401。"""
    resp = await client.post(
        "/api/auth/login", json={"username": "no_such_user_xyz", "password": "whatever1"}
    )
    assert resp.status_code == 401


async def test_me_requires_auth(client):
    """未带 token 访问 /me 返回 401。"""
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_returns_current_user(authed_client):
    """带 token 访问 /me 返回当前用户。"""
    client, user = authed_client
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == user["username"]
    assert resp.json()["role"] == "user"


async def test_change_password(authed_client):
    """修改密码后旧密码失效、新密码可登录。"""
    client, user = authed_client
    username = user["username"]

    resp = await client.post(
        "/api/auth/change-password",
        json={"old_password": "secret123", "new_password": "newpass456"},
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is True

    # 新密码可登录
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "newpass456"}
    )
    assert resp.status_code == 200

    # 旧密码失效
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 401


async def test_change_password_wrong_old(authed_client):
    """旧密码错误返回 400。"""
    client, _ = authed_client
    resp = await client.post(
        "/api/auth/change-password",
        json={"old_password": "wrong_old", "new_password": "newpass456"},
    )
    assert resp.status_code == 400


async def test_admin_seeded(client):
    """内置管理员 admin/123456 可登录且角色为 admin。"""
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "admin"


async def test_register_invalid_username(client):
    """用户名含非法字符返回 422。"""
    resp = await client.post(
        "/api/auth/register", json={"username": "bad name!", "password": "pass12345"}
    )
    assert resp.status_code == 422


async def test_register_short_password(client):
    """密码过短返回 422。"""
    resp = await client.post(
        "/api/auth/register", json={"username": "shortpass", "password": "123"}
    )
    assert resp.status_code == 422
