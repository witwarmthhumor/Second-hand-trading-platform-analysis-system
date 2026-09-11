"""问答会话服务层单测（M2）。

端点逻辑下沉后，会话归属/截断/落库这些规则可在无 HTTP 层的情况下直接验证，
比通过接口间接覆盖更精准。
"""

import pytest

from app.db import async_session_factory
from app.models.user import ROLE_ADMIN, ROLE_USER, User
from app.schemas.rag import SourceDoc
from app.services import chat_session_service as svc


async def _make_user(username: str, role: str = ROLE_USER) -> User:
    async with async_session_factory() as session:
        user = User(username=username, password_hash="x", role=role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def test_recent_history_truncates_to_n_rounds():
    """最近 N 轮 = 2N 条消息，避免长会话把历史全量注入 LLM。"""
    messages = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    assert len(svc.recent_history(messages, rounds=2)) == 4
    assert svc.recent_history(messages, rounds=2)[0]["content"] == "q16"
    assert svc.recent_history(None, rounds=5) == []


def test_dump_source_handles_pydantic_and_dict():
    """来源序列化兼容 pydantic 模型与裸 dict。"""
    doc = SourceDoc(doc_id="A-001", title="t", text="x", score=0.9)
    assert svc._dump_source(doc)["doc_id"] == "A-001"
    assert svc._dump_source({"doc_id": "B-002"}) == {"doc_id": "B-002"}


async def test_create_and_get_messages_roundtrip():
    """创建空会话 → 可读回，消息为空。"""
    user = await _make_user("svc_create")
    async with async_session_factory() as session:
        sid = await svc.create(session, user)
    async with async_session_factory() as session:
        detail = await svc.get_messages(session, sid, user)
    assert detail["session_id"] == sid
    assert detail["messages"] == []


async def test_get_messages_raises_for_foreign_session():
    """他人会话 → SessionNotFound（API 层映射为 404）。"""
    owner = await _make_user("svc_owner")
    intruder = await _make_user("svc_intruder")
    async with async_session_factory() as session:
        sid = await svc.create(session, owner)
    async with async_session_factory() as session:
        with pytest.raises(svc.SessionNotFound):
            await svc.get_messages(session, sid, intruder)


async def test_load_for_ask_denies_foreign_session():
    """问答前置：访问他人会话 → SessionAccessDenied。"""
    owner = await _make_user("svc_owner2")
    intruder = await _make_user("svc_intruder2")
    async with async_session_factory() as session:
        sid = await svc.create(session, owner)
    async with async_session_factory() as session:
        with pytest.raises(svc.SessionAccessDenied):
            await svc.load_for_ask(sid, intruder, session)


async def test_load_for_ask_returns_history_and_existing():
    """正常路径：返回历史与既有会话；无 session_id 时返回空。"""
    user = await _make_user("svc_hist")
    async with async_session_factory() as session:
        sid = await svc.create(session, user)
        await svc.save_round(
            session,
            session_id=sid,
            user_id=user.id,
            question="Q1",
            answer="A1",
            mode="hit",
        )
        history, existing = await svc.load_for_ask(sid, user, session)
    assert existing is not None
    assert [m["content"] for m in history] == ["Q1", "A1"]

    async with async_session_factory() as session:
        assert await svc.load_for_ask(None, user, session) == ([], None)


async def test_save_round_lazily_creates_and_appends():
    """懒创建：首轮落库建会话，第二轮追加而非覆盖。"""
    user = await _make_user("svc_save")
    sid = "lazy_session_1"
    async with async_session_factory() as session:
        await svc.save_round(
            session,
            session_id=sid,
            user_id=user.id,
            question="Q1",
            answer="A1",
            mode="hit",
            sources=[SourceDoc(doc_id="A-001", title="t", text="x", score=0.8)],
        )
    async with async_session_factory() as session:
        detail = await svc.get_messages(session, sid, user)
        assert len(detail["messages"]) == 2
        assert detail["messages"][1]["sources"][0]["doc_id"] == "A-001"

    async with async_session_factory() as session:
        existing = await svc.get_owned_session(sid, user, session)
        await svc.save_round(
            session,
            session_id=sid,
            user_id=user.id,
            question="Q2",
            answer="A2",
            mode="fallback",
            existing=existing,
        )
    async with async_session_factory() as session:
        detail = await svc.get_messages(session, sid, user)
        assert len(detail["messages"]) == 4


async def test_save_round_without_session_id_is_noop():
    """无 session_id（单轮问答）不落库。"""
    user = await _make_user("svc_nosid")
    async with async_session_factory() as session:
        await svc.save_round(
            session, session_id=None, user_id=user.id, question="q", answer="a", mode="hit"
        )


async def test_admin_can_access_any_session():
    """admin 可读取任意用户的会话。"""
    owner = await _make_user("svc_owner3")
    admin = await _make_user("svc_admin", role=ROLE_ADMIN)
    async with async_session_factory() as session:
        sid = await svc.create(session, owner)
    async with async_session_factory() as session:
        assert await svc.get_owned_session(sid, admin, session) is not None
        detail = await svc.get_messages(session, sid, admin)
    assert detail["session_id"] == sid


async def test_delete_many_counts_only_owned():
    """批量删除只统计真正删除的；他人会话静默跳过。"""
    owner = await _make_user("svc_bulk_owner")
    other = await _make_user("svc_bulk_other")
    async with async_session_factory() as session:
        mine = await svc.create(session, owner)
        theirs = await svc.create(session, other)
        deleted, requested = await svc.delete_many(session, [mine, theirs, "nope"], owner)
    assert (deleted, requested) == (1, 3)
    async with async_session_factory() as session:
        assert await svc.delete_one(session, mine, owner) is False  # 已删
        assert await svc.delete_one(session, theirs, other) is True
