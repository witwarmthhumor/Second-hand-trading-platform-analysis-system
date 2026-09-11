"""问答反馈查询服务（只读）：列表 + 聚合统计，供管理面板「反馈回流可视化」。

与提交端点（``api/rag.py::submit_feedback``）对应，把查询逻辑从端点层抽出，
使反馈数据从「只落库」变成「可见的闭环」，作为检索质量评测与调参的证据来源。
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_feedback import (
    FEEDBACK_DISLIKE,
    FEEDBACK_LIKE,
    MessageFeedback,
)


async def list_feedback(db: AsyncSession, page: int, page_size: int) -> dict:
    """分页列出反馈，按创建时间倒序（最新在前）。

    返回 ``{ total, page, page_size, items }``；每条含问题、回答快照、命中模式
    与来源 doc_id，供管理员定位「哪些问题被点 👎」。
    """
    total = (
        await db.execute(select(func.count()).select_from(MessageFeedback))
    ).scalar_one()
    rows = (
        await db.execute(
            select(MessageFeedback)
            .order_by(MessageFeedback.created_at.desc(), MessageFeedback.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(r) for r in rows],
    }


async def feedback_stats(db: AsyncSession) -> dict:
    """聚合反馈统计：总量、👍/👎 计数与占比、按命中模式（hit/fallback）交叉分布。

    一次查询完成，避免 N+1；空表时各计数归零。
    """
    row = (
        await db.execute(
            select(
                func.count().label("total"),
                func.sum(
                    case((MessageFeedback.rating == FEEDBACK_LIKE, 1), else_=0)
                ).label("like"),
                func.sum(
                    case((MessageFeedback.rating == FEEDBACK_DISLIKE, 1), else_=0)
                ).label("dislike"),
            )
        )
    ).one()

    total = row.total or 0
    like = row.like or 0
    dislike = row.dislike or 0

    # 按 mode（hit 直出 / fallback 兜底）× rating 交叉，暴露「哪条路径更常被点 👎」
    mode_rows = (
        await db.execute(
            select(
                MessageFeedback.mode,
                func.sum(
                    case((MessageFeedback.rating == FEEDBACK_LIKE, 1), else_=0)
                ).label("like"),
                func.sum(
                    case((MessageFeedback.rating == FEEDBACK_DISLIKE, 1), else_=0)
                ).label("dislike"),
            ).group_by(MessageFeedback.mode)
        )
    ).all()

    by_mode = {
        mode: {"like": like_count or 0, "dislike": dislike_count or 0}
        for mode, like_count, dislike_count in mode_rows
    }

    return {
        "total": total,
        "like": like,
        "dislike": dislike,
        "like_ratio": round(like / total, 4) if total else 0.0,
        "by_mode": by_mode,
    }


def _serialize(fb: MessageFeedback) -> dict:
    """ORM → 可 JSON 序列化的 dict（created_at 转 ISO 字符串）。"""
    return {
        "id": fb.id,
        "user_id": fb.user_id,
        "session_id": fb.session_id,
        "rating": fb.rating,
        "question": fb.question,
        "answer": fb.answer,
        "mode": fb.mode,
        "source_doc_ids": fb.source_doc_ids or [],
        "comment": fb.comment,
        "created_at": fb.created_at.isoformat() if fb.created_at else None,
    }
