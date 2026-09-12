"""RAG 问答 API。

端点只做「解析请求 → 调服务 → 返回响应」；会话与消息的持久化逻辑下沉到
``app.services.chat_session_service``（路线图 M2），避免单文件承载过多职责。

- POST   /api/rag/ask                     ：问答（命中直出 + 来源 / 未命中 LLM 兜底），支持多轮
- POST   /api/rag/ask/stream              ：流式问答（SSE）
- GET    /api/rag/status                  ：知识库就绪状态
- GET    /api/rag/sessions                ：会话摘要列表（admin 可见全部）
- POST   /api/rag/sessions                ：新建空会话
- GET    /api/rag/sessions/{session_id}   ：会话消息历史（仅本人或 admin）
- DELETE /api/rag/sessions/{session_id}   ：删除会话（仅本人或 admin）
- POST   /api/rag/sessions/batch-delete   ：批量删除会话
- POST   /api/rag/feedback                ：提交 👍/👎 反馈
- GET    /api/rag/feedback                ：反馈列表（仅管理员，分页）
- GET    /api/rag/feedback/stats          ：反馈统计聚合（仅管理员）
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user, require_admin
from app.models.chat_session import ChatSession
from app.models.message_feedback import FEEDBACK_DISLIKE, FEEDBACK_LIKE, MessageFeedback
from app.models.user import User
from app.rag.retriever import get_retriever
from app.schemas.rag import (
    AskRequest,
    AskResponse,
    BatchDeleteRequest,
    FeedbackRequest,
    SourceDoc,
)
from app.services import chat_session_service as sessions
from app.services import feedback_service, rag_service
from app.services.change_service import list_stored_competitors

router = APIRouter(prefix="/api/rag", tags=["rag"])


async def _load_competitors_context(session: AsyncSession) -> str:
    """读取已入库竞品数据，序列化为文本上下文（供 LLM 兜底回答对比类问题）。"""
    competitors = await list_stored_competitors(session)
    return rag_service.build_competitors_context(competitors)


async def _load_for_ask(
    session_id: str | None, user: User, db: AsyncSession
) -> tuple[list[dict], ChatSession | None]:
    """读取历史与既有会话；越权访问统一映射为 404（不泄露会话是否存在）。"""
    try:
        return await sessions.load_for_ask(session_id, user, db)
    except sessions.SessionAccessDenied:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问") from None


@router.post("/ask", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> AskResponse:
    """二手交易知识库问答（支持多轮会话与竞品数据联动，会话按用户隔离）。"""
    history, existing = await _load_for_ask(payload.session_id, user, session)

    # 读取已入库竞品数据作为对比类问题的上下文
    competitors_context = await _load_competitors_context(session)

    response = await rag_service.ask(
        payload.question,
        top_k=payload.top_k,
        session_id=payload.session_id,
        history=history,
        competitors_context=competitors_context,
    )

    # 保存本轮问答（懒创建 + 归属已在上面校验）
    await sessions.save_round(
        session,
        session_id=payload.session_id,
        user_id=user.id,
        question=payload.question,
        answer=response.answer,
        mode=response.mode,
        sources=response.sources,
        existing=existing,
    )
    return response


@router.post("/ask/stream")
async def ask_stream(
    payload: AskRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """问答流式版本（SSE）。

    - 检索命中：直出原文（一次性）后结束
    - 未命中：LLM 兜底逐 token 推送（`data: {delta}`），结束推 `data: [DONE]`

    检索走 `rag_service.retrieve`（缓存 + singleflight），与非流式端点共用同一套
    缓存 key —— 否则前端默认走的这条路会完全绕过缓存。
    """
    import json as _json

    history, existing = await _load_for_ask(payload.session_id, user, session)
    competitors_context = await _load_competitors_context(session)
    retriever = get_retriever()

    async def event_stream():
        if not retriever.is_ready:
            yield (
                "data: "
                + _json.dumps(
                    {"type": "done", "answer": "知识库尚未就绪，请先运行建库脚本。"},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            return

        # 先做一次检索判定（命中直出/合成，未命中走流式兜底）。
        # 双视图：来源/命中判定用广度，合成上下文用深度（P1-②）。
        breadth, depth = await rag_service.retrieve(
            payload.question, top_k=payload.top_k, retriever=retriever, with_depth=True
        )
        hit = rag_service.build_hit_gate(breadth)
        if hit is not None:
            sources: list[SourceDoc] = hit["sources"]

            # P1-② 合成分流：触发失败/拒答 → 降级直出（failed 保证发生在任何 delta 之前）
            if rag_service._should_synthesize(sources):
                synth_parts: list[str] = []
                synth_failed = False
                async for kind, text in rag_service._llm_synthesize_stream(
                    payload.question, depth
                ):
                    if kind == "failed":
                        synth_failed = True
                        break
                    synth_parts.append(text)
                    yield (
                        "data: "
                        + _json.dumps(
                            {"type": "delta", "content": text}, ensure_ascii=False
                        )
                        + "\n\n"
                    )

                if not synth_failed:
                    finalized = rag_service.finalize_stream_synthesis(
                        "".join(synth_parts), sources, depth
                    )
                    if finalized is not None:
                        cleaned, merged = finalized
                        rag_service.observe_synth("ok")
                        yield (
                            "data: "
                            + _json.dumps(
                                {
                                    "type": "done",
                                    "answer": cleaned,
                                    "mode": "synth",
                                    "sources": [s.model_dump() for s in merged],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        await sessions.save_round(
                            session,
                            session_id=payload.session_id,
                            user_id=user.id,
                            question=payload.question,
                            answer=cleaned,
                            mode="synth",
                            sources=merged,
                            existing=existing,
                        )
                        return
                    # 拒答/剥标为空：拒答 delta 已推送，done 降级为直出原文
                    rag_service.observe_synth("refused")

            full_answer = hit["answer"]
            yield (
                "data: "
                + _json.dumps(
                    {
                        "type": "done",
                        "answer": full_answer,
                        "mode": "hit",
                        "sources": [s.model_dump() for s in sources],
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            await sessions.save_round(
                session,
                session_id=payload.session_id,
                user_id=user.id,
                question=payload.question,
                answer=full_answer,
                mode="hit",
                sources=sources,
                existing=existing,
            )
            return

        # 未命中：先取「临界片段」。连临界标准都达不到 → 直接拒答，不调 LLM
        #（没有知识库依据时调用 LLM 只会得到凭参数知识编造的答案）。
        fallback_docs = rag_service.build_fallback_context(breadth)
        if fallback_docs is None:
            yield (
                "data: "
                + _json.dumps(
                    {
                        "type": "done",
                        "answer": rag_service.NO_KB_CONTENT_MSG,
                        "mode": "fallback",
                        "sources": [],
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            await sessions.save_round(
                session,
                session_id=payload.session_id,
                user_id=user.id,
                question=payload.question,
                answer=rag_service.NO_KB_CONTENT_MSG,
                mode="fallback",
                sources=[],
                existing=existing,
            )
            return

        # 未命中但存在临界片段：流式兜底（注入片段作参考），逐 token 推送
        full_answer_parts: list[str] = []
        async for delta in rag_service._llm_fallback_stream(
            payload.question,
            history=history,
            competitors_context=competitors_context,
            retrieved_context=rag_service.format_fallback_context(fallback_docs),
        ):
            full_answer_parts.append(delta)
            yield (
                "data: "
                + _json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
                + "\n\n"
            )
        yield (
            "data: "
            + _json.dumps(
                {
                    "type": "done",
                    "mode": "fallback",
                    "sources": [s.model_dump() for s in fallback_docs],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )
        await sessions.save_round(
            session,
            session_id=payload.session_id,
            user_id=user.id,
            question=payload.question,
            answer="".join(full_answer_parts),
            mode="fallback",
            sources=fallback_docs,
            existing=existing,
        )

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
        "message": "知识库就绪"
        if retriever.is_ready
        else "知识库未就绪，请先运行建库脚本",
    }


@router.get("/sessions")
async def list_sessions(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """列出会话摘要（按更新时间倒序）。普通用户仅见自己的，admin 可见全部。"""
    return await sessions.list_summaries(session, user)


@router.post("/sessions")
async def create_session(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """新建会话，返回 session_id（绑定当前用户）。

    注：前端已改为「首问懒创建」（ask 会话不存在时自动落库），此接口保留供
    显式建会话的场景与既有测试使用。
    """
    return {"session_id": await sessions.create(session, user)}


@router.post("/sessions/batch-delete")
async def batch_delete_sessions(
    payload: BatchDeleteRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """批量删除会话。普通用户只能删自己的，admin 可删任意；不存在的 id 静默跳过。"""
    deleted, requested = await sessions.delete_many(session, payload.session_ids, user)
    return {"deleted": deleted, "requested": requested}


@router.get("/sessions/{session_id}")
async def get_session_messages(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """获取指定会话的完整消息历史（仅本人或 admin），供前端切换会话时回显。"""
    try:
        return await sessions.get_messages(session, session_id, user)
    except sessions.SessionNotFound:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问") from None


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """删除指定会话（仅本人或 admin）。"""
    deleted = await sessions.delete_one(session, session_id, user)
    return {"deleted": deleted, "session_id": session_id}


@router.post("/feedback")
async def submit_feedback(
    payload: FeedbackRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict:
    """提交问答反馈（👍/👎），用于检索质量评测与 rerank 调参。

    校验：rating 必须为 like/dislike；会话需属于当前用户（admin 可对任意会话反馈）。
    """
    if payload.rating not in (FEEDBACK_LIKE, FEEDBACK_DISLIKE):
        raise HTTPException(status_code=422, detail="rating 仅支持 like / dislike")

    # 校验会话归属（与其它会话接口一致）
    if payload.session_id:
        row = await sessions.get_owned_session(payload.session_id, user, session)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    fb = MessageFeedback(
        user_id=user.id,
        session_id=payload.session_id,
        rating=payload.rating,
        question=payload.question,
        answer=payload.answer,
        mode=payload.mode,
        source_doc_ids=payload.source_doc_ids,
        comment=payload.comment,
    )
    session.add(fb)
    await session.commit()
    return {"ok": True, "id": fb.id}


@router.get("/feedback/stats")
async def feedback_stats(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> dict:
    """反馈统计聚合（仅管理员），供「反馈回流可视化」面板展示。"""
    return await feedback_service.feedback_stats(session)


@router.get("/feedback")
async def list_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> dict:
    """分页列出问答反馈（仅管理员，最新在前）。"""
    return await feedback_service.list_feedback(session, page, page_size)
