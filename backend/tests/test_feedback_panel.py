"""反馈回流可视化（管理面板）的 API 层测试。

覆盖：
- 未登录 401、普通用户 403（stats 与 list 均为 admin 专属）
- admin 统计聚合正确（total / like / dislike / like_ratio / by_mode 交叉）
- admin 分页列表（倒序、total 正确、末页截断）
不依赖 Ollama/Chroma。
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import engine
from app.main import app
from app.models.message_feedback import MessageFeedback

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _reset_feedback():
    """清空反馈表，保证每个用例的聚合数字可预测（pytest 串行，无并发干扰）。"""
    async with engine.begin() as conn:
        await conn.execute(delete(MessageFeedback))


async def _register(client) -> str:
    username = f"fbp_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


async def _admin_token(client) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _submit(client, token, sid, rating, mode, question):
    resp = await client.post(
        "/api/rag/feedback",
        json={
            "session_id": sid,
            "rating": rating,
            "question": question,
            "answer": f"answer of {question}",
            "mode": mode,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


async def test_stats_requires_admin(client):
    """未登录 401，普通用户 403。"""
    resp = await client.get("/api/rag/feedback/stats")
    assert resp.status_code == 401

    token = await _register(client)
    resp = await client.get("/api/rag/feedback/stats", headers=_auth(token))
    assert resp.status_code == 403


async def test_list_requires_admin(client):
    """反馈列表为 admin 专属。"""
    token = await _register(client)
    resp = await client.get("/api/rag/feedback", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_stats_empty(client):
    """空表时各计数归零，by_mode 为空对象。"""
    await _reset_feedback()
    admin = await _admin_token(client)
    resp = await client.get("/api/rag/feedback/stats", headers=_auth(admin))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["like"] == 0
    assert data["dislike"] == 0
    assert data["like_ratio"] == 0.0
    assert data["by_mode"] == {}


async def test_admin_stats_aggregates(client):
    """聚合正确：total/like/dislike 与 by_mode 交叉（hit×like、fallback×dislike 等）。"""
    await _reset_feedback()
    token = await _register(client)
    sid = (await client.post("/api/rag/sessions", headers=_auth(token))).json()["session_id"]

    await _submit(client, token, sid, "like", "hit", "q1")
    await _submit(client, token, sid, "dislike", "fallback", "q2")
    await _submit(client, token, sid, "like", "fallback", "q3")

    admin = await _admin_token(client)
    resp = await client.get("/api/rag/feedback/stats", headers=_auth(admin))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["like"] == 2
    assert data["dislike"] == 1
    assert data["like_ratio"] == round(2 / 3, 4)
    assert data["by_mode"]["hit"] == {"like": 1, "dislike": 0}
    assert data["by_mode"]["fallback"] == {"like": 1, "dislike": 1}


async def test_admin_list_pagination(client):
    """分页列表：total 正确、按创建时间倒序（id 兜底）、末页截断。"""
    await _reset_feedback()
    token = await _register(client)
    sid = (await client.post("/api/rag/sessions", headers=_auth(token))).json()["session_id"]
    for i in range(5):
        await _submit(client, token, sid, "like" if i % 2 else "dislike", "hit", f"q{i}")

    admin = await _admin_token(client)
    resp = await client.get(
        "/api/rag/feedback", params={"page": 1, "page_size": 2}, headers=_auth(admin)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    # 倒序：最后提交的 q4 排最前
    assert data["items"][0]["question"] == "q4"
    assert data["items"][1]["question"] == "q3"
    # 序列化字段完整
    assert data["items"][0]["rating"] in ("like", "dislike")
    assert "created_at" in data["items"][0]

    # 第 3 页只剩 1 条（5 = 2*2 + 1）
    resp2 = await client.get(
        "/api/rag/feedback", params={"page": 3, "page_size": 2}, headers=_auth(admin)
    )
    assert resp2.json()["total"] == 5
    assert len(resp2.json()["items"]) == 1
    assert resp2.json()["items"][0]["question"] == "q0"
