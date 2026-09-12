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


async def test_get_session_messages(client):
    """能取回自己会话的消息历史；他人会话 404；不存在的会话 404。"""
    user_a, token_a = await _register(client)
    _, token_b = await _register(client)

    # A 新建会话并提问一轮，产生消息
    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid = resp.json()["session_id"]
    resp = await client.post(
        "/api/rag/ask",
        json={"question": "忘记密码怎么办", "session_id": sid},
        headers=_auth(token_a),
    )
    assert resp.status_code == 200

    # A 取回消息：应有 2 条（user + assistant）
    resp = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token_a))
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and "忘记密码" in msgs[0]["content"]

    # B 取 A 的会话 → 404
    resp = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token_b))
    assert resp.status_code == 404

    # 不存在的会话 → 404
    resp = await client.get("/api/rag/sessions/nonexistent", headers=_auth(token_a))
    assert resp.status_code == 404


async def test_ask_lazy_creates_session(client):
    """ask 带不存在的 session_id 时懒创建（归属当前用户），不再 404。"""
    _, token_a = await _register(client)

    sid = secrets.token_hex(16)
    resp = await client.post(
        "/api/rag/ask",
        json={"question": "验机流程是什么", "session_id": sid},
        headers=_auth(token_a),
    )
    assert resp.status_code == 200

    # 会话已落库且归属当前用户
    resp = await client.get("/api/rag/sessions", headers=_auth(token_a))
    assert sid in [s["session_id"] for s in resp.json()]


async def test_batch_delete_sessions(client):
    """批量删除：普通用户只能删自己的；admin 可删任意；返回删除数量。"""
    user_a, token_a = await _register(client)
    user_b, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid_a1 = resp.json()["session_id"]
    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid_a2 = resp.json()["session_id"]
    resp = await client.post("/api/rag/sessions", headers=_auth(token_b))
    sid_b1 = resp.json()["session_id"]

    # A 批量删 [自己的两个 + B 的一个] → 只删掉 2 个
    resp = await client.post(
        "/api/rag/sessions/batch-delete",
        json={"session_ids": [sid_a1, sid_a2, sid_b1]},
        headers=_auth(token_a),
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2

    # A 的会话没了，B 的还在
    resp = await client.get("/api/rag/sessions", headers=_auth(token_a))
    assert sid_a1 not in [s["session_id"] for s in resp.json()]
    resp = await client.get("/api/rag/sessions", headers=_auth(token_b))
    assert sid_b1 in [s["session_id"] for s in resp.json()]

    # admin 可批量删 B 的会话
    admin_token = await _login(client, "admin", "123456")
    resp = await client.post(
        "/api/rag/sessions/batch-delete",
        json={"session_ids": [sid_b1]},
        headers=_auth(admin_token),
    )
    assert resp.json()["deleted"] == 1

    # 空 id 列表 → 422 校验失败
    resp = await client.post(
        "/api/rag/sessions/batch-delete",
        json={"session_ids": []},
        headers=_auth(token_a),
    )
    assert resp.status_code == 422


# ---------------- 流式问答（SSE） ----------------

async def test_ask_stream_fallback(client, monkeypatch):
    """未命中（灰区）时流式接口逐 token 推送 delta，并以 done 结束，会话落库。"""
    from app.api import rag as rag_api
    from app.services import rag_service

    async def _fake_stream(
        question, history=None, competitors_context=None, retrieved_context=None
    ):
        for piece in ["流式", "回答", "内容"]:
            yield piece

    monkeypatch.setattr(rag_service, "llm_fallback_stream", _fake_stream)

    # mock retriever：灰区（0.4 ≤ top1 < 阈值）→ 注入临界片段后走流式兜底
    class _Retriever:
        is_ready = True

        def search(self, question, top_k=5):
            return [{"doc_id": "A-001", "text": "x", "vector_sim": 0.5}]

    monkeypatch.setattr(rag_api, "get_retriever", lambda: _Retriever())

    _, token = await _register(client)
    sid = secrets.token_hex(16)
    async with client.stream(
        "POST",
        "/api/rag/ask/stream",
        json={"question": "测试", "session_id": sid},
        headers=_auth(token),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk

    # 应包含 3 个 delta 和一个 done
    assert "流式" in body and "回答" in body and "内容" in body
    assert '"type": "done"' in body

    # 会话已落库（2 条消息：user + assistant）
    r = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token))
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert msgs[1]["content"] == "流式回答内容"


async def test_ask_stream_rejects_without_llm_when_too_weak(client, monkeypatch):
    """流式：检索相似度连 0.4 都不到时直接拒答，不调用 LLM。"""
    from app.api import rag as rag_api
    from app.services import rag_service

    async def _should_not_call(*a, **k):
        raise AssertionError("相似度过低时不应调用 LLM")
        yield  # pragma: no cover  —— 使其成为异步生成器

    monkeypatch.setattr(rag_service, "llm_fallback_stream", _should_not_call)

    class _Retriever:
        is_ready = True

        def search(self, question, top_k=5):
            return [{"doc_id": "A-001", "text": "x", "vector_sim": 0.2}]

    monkeypatch.setattr(rag_api, "get_retriever", lambda: _Retriever())

    _, token = await _register(client)
    sid = secrets.token_hex(16)
    async with client.stream(
        "POST",
        "/api/rag/ask/stream",
        json={"question": "今天天气", "session_id": sid},
        headers=_auth(token),
    ) as resp:
        assert resp.status_code == 200
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk

    assert '"mode": "fallback"' in body
    assert "暂无" in body


async def test_ask_stream_requires_auth(client):
    """流式接口未登录返回 401。"""
    async with client.stream(
        "POST", "/api/rag/ask/stream", json={"question": "测试"}
    ) as resp:
        assert resp.status_code == 401


async def test_ask_stream_hit_saves_session(client, monkeypatch):
    """命中直出（mode=hit）时同样保存会话消息。"""
    from app.api import rag as rag_api

    class _HitRetriever:
        is_ready = True

        def search(self, question, top_k=5):
            return [{"doc_id": "A-001", "text": "命中原文内容", "vector_sim": 0.8}]

    monkeypatch.setattr(rag_api, "get_retriever", lambda: _HitRetriever())

    _, token = await _register(client)
    sid = secrets.token_hex(16)
    async with client.stream(
        "POST",
        "/api/rag/ask/stream",
        json={"question": "命中问题", "session_id": sid},
        headers=_auth(token),
    ) as resp:
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk

    assert '"mode": "hit"' in body
    assert "命中原文内容" in body

    # 会话已落库
    r = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token))
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert msgs[1]["content"] == "命中原文内容"

