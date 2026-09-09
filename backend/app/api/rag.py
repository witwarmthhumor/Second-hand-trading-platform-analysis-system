"""RAG 问答 API。

- POST /api/rag/ask        ：问答（检索命中直出 + 来源，未命中 LLM 兜底）；支持 session_id 多轮
- GET  /api/rag/status     ：知识库就绪状态
- GET  /api/rag/sessions   ：列出当前用户的会话（admin 可见全部）
- POST /api/rag/sessions   ：新建会话（绑定当前用户）
- DELETE /api/rag/sessions/{session_id} ：删除指定会话（仅本人或 admin）
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models.chat_session import ChatSession
from app.models.user import ROLE_ADMIN, User
from app.rag.retriever import get_retriever
from app.schemas.rag import AskRequest, AskResponse
from app.services import rag_service
from app.services.change_service import list_stored_competitors

router = APIRouter(prefix="/api/rag", tags=["rag"])

# 注入 LLM 兜底时保留的最近历史轮数（一轮 = 一问一答）
_HISTORY_ROUNDS = 5


async def _load_competitors_context(session: AsyncSession) -> str:
    """读取已入库竞品数据，序列化为文本上下文（供 LLM 兜底回答对比类问题）。"""
    competitors = await list_stored_competitors(session)
    return rag_service.build_competitors_context(competitors)


async def _get_owned_session(
    session_id: str, user: User, db: AsyncSession
) -> ChatSession | None:
    """按会话归属读取：普通用户只能取自己的会话，admin 可取任意会话。"""
    row = await db.get(ChatSession, session_id)
    if row is None:
        return None
    if user.role != ROLE_ADMIN and row.user_id != user.id:
        return None
    return row


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> AskResponse:
    """二手交易知识库问答（支持多轮会话与竞品数据联动，会话按用户隔离）。"""
    session_id = payload.session_id

    # 读取历史：带 session_id 时从 DB 取本人会话，取最近 _HISTORY_ROUNDS 轮
    history: list[dict] = []
    if session_id:
        row = await _get_owned_session(session_id, user, session)
        if row is not None:
            messages = row.messages or []
            history = messages[-(_HISTORY_ROUNDS * 2):]
        else:
            # 会话不存在或不属于当前用户：拒绝继续，避免越权读写他人会话
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # 读取已入库竞品数据作为对比类问题的上下文
    competitors_context = await _load_competitors_context(session)

    response = await rag_service.ask(
        payload.question,
        top_k=payload.top_k,
        session_id=session_id,
        history=history,
        competitors_context=competitors_context,
    )

    # 保存本轮问答到会话（仅当带 session_id 时，实现多轮记忆）
    if session_id:
        row = await _get_owned_session(session_id, user, session)
        new_messages = [
            {"role": "user", "content": payload.question},
            {"role": "assistant", "content": response.answer},
        ]
        if row is None:
            row = ChatSession(session_id=session_id, user_id=user.id, messages=new_messages)
            session.add(row)
        else:
            row.messages = (row.messages or []) + new_messages
        await session.commit()

    return response


@router.get("/status")
async def status() -> dict:
    """返回向量库就绪状态。"""
    retriever = get_retriever()
    return {
        "ready": retriever.is_ready,
        "message": "知识库就绪" if retriever.is_ready else "知识库未就绪，请先运行建库脚本",
    }


@router.get("/sessions")
async def list_sessions(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """列出会话摘要（按更新时间倒序）。普通用户仅见自己的，admin 可见全部。"""
    stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(ChatSession.user_id == user.id)
    result = await session.execute(stmt)
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


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """删除指定会话（仅本人或 admin）。"""
    row = await _get_owned_session(session_id, user, session)
    if row is None:
        return {"deleted": False, "session_id": session_id}
    await session.delete(row)
    await session.commit()
    return {"deleted": True, "session_id": session_id}


@router.post("/sessions")
async def create_session(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """新建会话，返回 session_id（前端「新对话」用）。会话记录立即落库并归属当前用户。

    立即落库的意义：空会话也能出现在历史列表里，用户新建后未提问就退出，
    重新登录仍能找回这条会话。
    """
    session_id = uuid.uuid4().hex
    session.add(ChatSession(session_id=session_id, user_id=user.id, messages=[]))
    await session.commit()
    return {"session_id": session_id}
