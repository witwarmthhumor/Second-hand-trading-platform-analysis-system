"""问答会话服务：归属校验、列表/详情/删除、消息落库。

从 ``app/api/rag.py`` 下沉（路线图 M2）：端点只负责「解析请求 → 调服务 → 返回响应」，
会话与消息的持久化逻辑集中在此处，既可被非流式与流式两条问答路径复用，也便于
脱离 HTTP 层单独测试。

领域异常（由 API 层翻译成 HTTP 状态码，服务层不感知 HTTP）：
- SessionAccessDenied：会话存在但不属于当前用户
- SessionNotFound：会话不存在
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.user import ROLE_ADMIN, User

# 注入 LLM 兜底时保留的最近历史轮数（一轮 = 一问一答）
HISTORY_ROUNDS = 5


class SessionAccessDenied(Exception):
    """会话存在但归属他人且当前用户非管理员。"""


class SessionNotFound(Exception):
    """会话不存在。"""


async def get_owned_session(
    session_id: str, user: User, db: AsyncSession
) -> ChatSession | None:
    """按归属读取会话：普通用户只能取自己的，admin 可取任意；不存在或无权返回 None。"""
    row = await db.get(ChatSession, session_id)
    if row is None:
        return None
    if user.role != ROLE_ADMIN and row.user_id != user.id:
        return None
    return row


def recent_history(messages: list[dict] | None, rounds: int = HISTORY_ROUNDS) -> list[dict]:
    """截取最近 N 轮历史消息（一轮 = 一问一答，故取 2N 条）。"""
    return (messages or [])[-(rounds * 2) :]


async def load_for_ask(
    session_id: str | None, user: User, db: AsyncSession
) -> tuple[list[dict], ChatSession | None]:
    """问答前置：读取历史与既有会话。

    - 带 session_id 且会话归属他人 → 抛 SessionAccessDenied（防越权读写）
    - 会话不存在 → 返回空历史，由保存阶段懒创建（前端本地生成 session_id）
    """
    if not session_id:
        return [], None

    existing = await db.get(ChatSession, session_id)
    if existing is not None and user.role != ROLE_ADMIN and existing.user_id != user.id:
        raise SessionAccessDenied(session_id)
    if existing is None:
        return [], None
    return recent_history(existing.messages), existing


async def save_round(
    db: AsyncSession,
    *,
    session_id: str | None,
    user_id: int,
    question: str,
    answer: str,
    mode: str,
    sources: list | None = None,
    existing: ChatSession | None = None,
) -> None:
    """把一轮问答写入会话（会话不存在则懒创建）。

    sources 一并落库，使历史会话回显时能还原来源引用。
    """
    if not session_id:
        return
    new_messages = [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": answer,
            "mode": mode,
            "sources": [_dump_source(s) for s in (sources or [])],
        },
    ]
    if existing is None:
        # 调用方未传入既有会话时先查一次：避免「会话已存在但仍走 INSERT」
        # 造成主键冲突（对懒创建路径而言这次查询必然为空，代价可忽略）。
        existing = await db.get(ChatSession, session_id)

    if existing is None:
        db.add(
            ChatSession(
                session_id=session_id, user_id=user_id, messages=new_messages
            )
        )
    else:
        existing.messages = (existing.messages or []) + new_messages
    await db.commit()


def _dump_source(source) -> dict:
    """SourceDoc（pydantic 模型）或 dict 统一转成可 JSON 序列化的 dict。"""
    if isinstance(source, dict):
        return source
    model_dump = getattr(source, "model_dump", None)
    return model_dump() if callable(model_dump) else dict(source)


async def list_summaries(db: AsyncSession, user: User) -> list[dict]:
    """会话摘要列表（按更新时间倒序）。普通用户仅见自己的，admin 可见全部。"""
    stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(ChatSession.user_id == user.id)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "session_id": r.session_id,
            "message_count": len(r.messages or []),
            "first_question": (
                (r.messages or [])[0]["content"] if (r.messages or []) else ""
            ),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


async def get_messages(db: AsyncSession, session_id: str, user: User) -> dict:
    """获取会话完整消息历史（供前端切换会话时回显）。"""
    row = await get_owned_session(session_id, user, db)
    if row is None:
        raise SessionNotFound(session_id)
    return {
        "session_id": row.session_id,
        "messages": row.messages or [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def delete_one(db: AsyncSession, session_id: str, user: User) -> bool:
    """删除单个会话；不存在或无权返回 False。"""
    row = await get_owned_session(session_id, user, db)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def delete_many(
    db: AsyncSession, session_ids: list[str], user: User
) -> tuple[int, int]:
    """批量删除会话，返回 (已删数, 请求数)；不存在的 id 静默跳过。"""
    deleted = 0
    for sid in session_ids:
        row = await get_owned_session(sid, user, db)
        if row is not None:
            await db.delete(row)
            deleted += 1
    await db.commit()
    return deleted, len(session_ids)


async def create(db: AsyncSession, user: User) -> str:
    """新建空会话，返回 session_id（绑定当前用户）。"""
    session_id = uuid.uuid4().hex
    db.add(ChatSession(session_id=session_id, user_id=user.id, messages=[]))
    await db.commit()
    return session_id
