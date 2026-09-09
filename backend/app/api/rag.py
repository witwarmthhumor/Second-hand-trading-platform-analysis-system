"""RAG 问答 API。

- POST /api/rag/ask        ：问答（检索命中直出 + 来源，未命中 LLM 兜底）；支持 session_id 多轮，
                             会话不存在时懒创建（前端本地生成 session_id，首问即落库）
- GET  /api/rag/status     ：知识库就绪状态
- GET  /api/rag/sessions   ：列出当前用户的会话（admin 可见全部）
- POST /api/rag/sessions   ：新建会话（绑定当前用户）
- GET  /api/rag/sessions/{session_id} ：获取指定会话的消息历史（仅本人或 admin）
- POST /api/rag/sessions/batch-delete ：批量删除会话（只能删自己的；admin 可删任意）
- DELETE /api/rag/sessions/{session_id} ：删除指定会话（仅本人或 admin）
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models.chat_session import ChatSession
from app.models.user import ROLE_ADMIN, User
from app.rag.config import SIMILARITY_THRESHOLD
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


class BatchDeleteRequest(BaseModel):
    """批量删除会话请求体。"""

    session_ids: list[str] = Field(..., min_length=1, max_length=100)


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
    existing: ChatSession | None = None
    if session_id:
        existing = await session.get(ChatSession, session_id)
        if existing is not None and user.role != ROLE_ADMIN and existing.user_id != user.id:
            # 会话存在但不属于当前用户：拒绝，避免越权读写他人会话
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")
        if existing is not None:
            history = (existing.messages or [])[-(_HISTORY_ROUNDS * 2):]
        # 会话不存在：懒创建模式，保存阶段落库归属当前用户（前端本地生成 session_id）

    # 读取已入库竞品数据作为对比类问题的上下文
    competitors_context = await _load_competitors_context(session)

    response = await rag_service.ask(
        payload.question,
        top_k=payload.top_k,
        session_id=session_id,
        history=history,
        competitors_context=competitors_context,
    )

    # 保存本轮问答到会话（仅当带 session_id 时，实现多轮记忆；不存在则懒创建）
    if session_id:
        new_messages = [
            {"role": "user", "content": payload.question},
            {"role": "assistant", "content": response.answer},
        ]
        if existing is None:
            session.add(
                ChatSession(
                    session_id=session_id, user_id=user.id, messages=new_messages
                )
            )
        else:
            existing.messages = (existing.messages or []) + new_messages
        await session.commit()

    return response


@router.post("/ask/stream")
async def ask_stream(
    payload: AskRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """问答流式版本（SSE）。

    - 检索命中：直出原文（一次性，非流式）后结束
    - 未命中：LLM 兜底逐 token 推送（`data: {delta}`），结束推 `data: [DONE]`

    会话保存与多轮记忆逻辑与 ask 一致（懒创建 + 归属校验）。
    """
    import json as _json

    session_id = payload.session_id

    history: list[dict] = []
    existing: ChatSession | None = None
    if session_id:
        existing = await session.get(ChatSession, session_id)
        if existing is not None and user.role != ROLE_ADMIN and existing.user_id != user.id:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")
        if existing is not None:
            history = (existing.messages or [])[-(_HISTORY_ROUNDS * 2):]

    competitors_context = await _load_competitors_context(session)

    retriever = get_retriever()

    async def _save_session(answer: str) -> None:
        """保存本轮问答到会话（懒创建 + 归属校验已在前面完成）。"""
        if not session_id:
            return
        new_messages = [
            {"role": "user", "content": payload.question},
            {"role": "assistant", "content": answer},
        ]
        if existing is None:
            session.add(
                ChatSession(session_id=session_id, user_id=user.id, messages=new_messages)
            )
        else:
            existing.messages = (existing.messages or []) + new_messages
        await session.commit()

    async def event_stream():
        if not retriever.is_ready:
            yield "data: " + _json.dumps(
                {"type": "done", "answer": "知识库尚未就绪，请先运行建库脚本。"},
                ensure_ascii=False,
            ) + "\n\n"
            return

        # 先做一次检索判定（命中直出，未命中走流式兜底）
        results = retriever.search(payload.question, top_k=payload.top_k)
        if results and results[0]["vector_sim"] >= SIMILARITY_THRESHOLD:
            top = results[0]
            full_answer = top["text"]
            yield "data: " + _json.dumps(
                {"type": "done", "answer": full_answer, "mode": "hit"}, ensure_ascii=False
            ) + "\n\n"
            await _save_session(full_answer)
            return

        # 未命中：流式兜底，逐 token 推送
        full_answer_parts: list[str] = []
        async for delta in rag_service._llm_fallback_stream(
            payload.question, history=history, competitors_context=competitors_context
        ):
            full_answer_parts.append(delta)
            yield "data: " + _json.dumps({"type": "delta", "content": delta}, ensure_ascii=False) + "\n\n"
        yield "data: " + _json.dumps({"type": "done", "mode": "fallback"}, ensure_ascii=False) + "\n\n"
        await _save_session("".join(full_answer_parts))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@router.get("/sessions/{session_id}")
async def get_session_messages(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """获取指定会话的完整消息历史（仅本人或 admin），供前端切换会话时回显。"""
    row = await _get_owned_session(session_id, user, session)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return {
        "session_id": row.session_id,
        "messages": row.messages or [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/sessions/batch-delete")
async def batch_delete_sessions(
    payload: BatchDeleteRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """批量删除会话。普通用户只能删自己的，admin 可删任意；不存在的 id 静默跳过。"""
    deleted = 0
    for sid in payload.session_ids:
        row = await _get_owned_session(sid, user, session)
        if row is not None:
            await session.delete(row)
            deleted += 1
    await session.commit()
    return {"deleted": deleted, "requested": len(payload.session_ids)}


@router.post("/sessions")
async def create_session(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """新建会话，返回 session_id（绑定当前用户）。

    注：前端已改为「首问懒创建」（ask 会话不存在时自动落库），此接口保留供
    显式建会话的场景与既有测试使用。
    """
    session_id = uuid.uuid4().hex
    session.add(ChatSession(session_id=session_id, user_id=user.id, messages=[]))
    await session.commit()
    return {"session_id": session_id}
