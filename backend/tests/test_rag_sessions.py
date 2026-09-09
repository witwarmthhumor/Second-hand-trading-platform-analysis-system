"""RAG 会话按用户隔离的 API 层测试。

覆盖：会话归属隔离、admin 可见全部、越权访问拒绝。
不依赖 Ollama/Chroma（ask 未就绪时直接返回兜底，不触发检索）。
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client) -> tuple[dict, str]:
    """注册新用户，返回 (user, token)。"""
    username = f"rag_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["user"], body["access_token"]


async def _login(client, username, password) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_sessions_require_auth(client):
    """未登录访问会话列表返回 401。"""
    resp = await client.get("/api/rag/sessions")
    assert resp.status_code == 401


async def test_create_session_returns_id_and_binds_user(client):
    """新建会话返回 session_id；用户 A 的会话列表只含自己创建的。"""
    user_a, token_a = await _register(client)
    user_b, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert sid

    # A 能看到自己的会话；B 看不到
    resp = await client.get("/api/rag/sessions", headers=_auth(token_a))
    ids_a = [s["session_id"] for s in resp.json()]
    assert sid in ids_a

    resp = await client.get("/api/rag/sessions", headers=_auth(token_b))
    ids_b = [s["session_id"] for s in resp.json()]
    assert sid not in ids_b


async def test_ask_with_foreign_session_rejected(client):
    """用户 B 用 A 的 session_id 提问被拒绝（404），不能越权读写。"""
    user_a, token_a = await _register(client)
    _, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid = resp.json()["session_id"]

    # B 用 A 的会话提问 → 404
    resp = await client.post(
        "/api/rag/ask",
        json={"question": "测试", "session_id": sid},
        headers=_auth(token_b),
    )
    assert resp.status_code == 404


async def test_delete_foreign_session_rejected(client):
    """用户 B 不能删除 A 的会话。"""
    user_a, token_a = await _register(client)
    _, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid = resp.json()["session_id"]

    resp = await client.delete(f"/api/rag/sessions/{sid}", headers=_auth(token_b))
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False

    # A 自己的会话仍在
    resp = await client.get("/api/rag/sessions", headers=_auth(token_a))
    assert sid in [s["session_id"] for s in resp.json()]


async def test_admin_sees_all_sessions(client):
    """admin 能列出所有用户的会话，普通用户仅见自己的。"""
    user_a, token_a = await _register(client)
    user_b, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid_a = resp.json()["session_id"]
    resp = await client.post("/api/rag/sessions", headers=_auth(token_b))
    sid_b = resp.json()["session_id"]

    # admin 可见两者
    admin_token = await _login(client, "admin", "123456")
    resp = await client.get("/api/rag/sessions", headers=_auth(admin_token))
    ids = [s["session_id"] for s in resp.json()]
    assert sid_a in ids and sid_b in ids

    # A 只看到自己的
    resp = await client.get("/api/rag/sessions", headers=_auth(token_a))
    ids_a = [s["session_id"] for s in resp.json()]
    assert sid_a in ids_a and sid_b not in ids_a


async def test_session_survives_relogin(client):
    """同一用户重新登录后仍能找回自己的历史会话。"""
    import secrets as _s

    username = f"relogin_{_s.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    token = resp.json()["access_token"]

    resp = await client.post("/api/rag/sessions", headers=_auth(token))
    sid = resp.json()["session_id"]

    # 重新登录
    token2 = await _login(client, username, "secret123")
    resp = await client.get("/api/rag/sessions", headers=_auth(token2))
    assert sid in [s["session_id"] for s in resp.json()]
