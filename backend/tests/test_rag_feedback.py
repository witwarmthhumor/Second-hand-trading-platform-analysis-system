"""问答反馈回路 + 来源引用持久化的 API 层测试。

覆盖：
- 反馈提交（like/dislike）落库，非法 rating 拒绝，会话归属校验
- 流式命中时 sources 随消息一起落库（历史回显可还原来源）
- 非流式 ask 命中时 sources 落库
不依赖 Ollama/Chroma（命中/未命中均 mock retriever）。
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
    username = f"fb_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["user"], body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_feedback_requires_auth(client):
    resp = await client.post(
        "/api/rag/feedback",
        json={"session_id": "x", "rating": "like", "question": "q"},
    )
    assert resp.status_code == 401


async def test_feedback_invalid_rating_rejected(client):
    _, token = await _register(client)
    resp = await client.post(
        "/api/rag/feedback",
        json={"session_id": "x", "rating": "meh", "question": "q"},
        headers=_auth(token),
    )
    assert resp.status_code == 422


async def test_feedback_like_and_dislike_persist(client):
    """提交 like / dislike 均落库；关联到本人会话。"""
    _, token = await _register(client)
    # 先建一个会话（feedback 需要会话归属校验通过）
    resp = await client.post("/api/rag/sessions", headers=_auth(token))
    sid = resp.json()["session_id"]

    resp = await client.post(
        "/api/rag/feedback",
        json={
            "session_id": sid,
            "rating": "like",
            "question": "担保交易怎么运作",
            "answer": "担保交易是……",
            "mode": "hit",
            "source_doc_ids": ["A-001"],
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = await client.post(
        "/api/rag/feedback",
        json={
            "session_id": sid,
            "rating": "dislike",
            "question": "另一个问题",
            "answer": "回答",
            "mode": "fallback",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200


async def test_feedback_foreign_session_rejected(client):
    """对他人会话提交反馈被拒绝（404）。"""
    _, token_a = await _register(client)
    _, token_b = await _register(client)

    resp = await client.post("/api/rag/sessions", headers=_auth(token_a))
    sid = resp.json()["session_id"]

    resp = await client.post(
        "/api/rag/feedback",
        json={"session_id": sid, "rating": "like", "question": "q"},
        headers=_auth(token_b),
    )
    assert resp.status_code == 404


async def test_ask_hit_persists_sources(client, monkeypatch):
    """非流式 ask 命中时，assistant 消息携带 sources 落库。"""
    from app.services import rag_service

    async def _fake_retrieve(question, top_k=5, retriever=None, with_depth=False):
        results = [
            {"doc_id": "A-001", "text": "命中原文", "vector_sim": 0.8},
            {"doc_id": "A-002", "text": "次相关", "vector_sim": 0.72},
        ]
        # P1-②：ask 走双视图检索（with_depth=True），旧单视图 mock 同步升级
        return (results, results) if with_depth else results

    monkeypatch.setattr(rag_service, "retrieve", _fake_retrieve)
    # 默认档位已是 multi_doc（sources≥2 会触发合成）；本用例聚焦「命中落 sources」，
    # 显式固定 off 避免依赖真实 LLM。
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "off")

    _, token = await _register(client)
    sid = secrets.token_hex(16)
    resp = await client.post(
        "/api/rag/ask",
        json={"question": "验机流程是什么", "session_id": sid},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "hit"

    r = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token))
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    # 来源已随消息落库，供历史回显还原引用
    assert "sources" in assistant
    assert len(assistant["sources"]) == 2
    assert assistant["sources"][0]["doc_id"] == "A-001"
    assert assistant["mode"] == "hit"


async def test_ask_stream_hit_persists_sources(client, monkeypatch):
    """流式命中时，done 事件携带 sources 且落库。"""
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
    # done 事件里带 sources
    assert '"sources"' in body

    r = await client.get(f"/api/rag/sessions/{sid}", headers=_auth(token))
    msgs = r.json()["messages"]
    assistant = msgs[1]
    assert assistant["sources"][0]["doc_id"] == "A-001"
