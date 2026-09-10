"""RAG 问答反馈 ORM 模型。

用于沉淀用户对 AI 回答的反馈（👍/👎），作为检索质量评测与 rerank 调参的
数据来源。反馈关联到具体会话与消息，可导出为评测集。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# 反馈值常量
FEEDBACK_LIKE = "like"
FEEDBACK_DISLIKE = "dislike"


class MessageFeedback(Base):
    """问答反馈：一条反馈对应会话中的一轮 AI 回答。"""

    __tablename__ = "message_feedbacks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 归属用户：外键关联 users.id，按用户隔离
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 所属会话：外键关联 chat_sessions.session_id
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 反馈值：like / dislike
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    # 用户原始问题（便于把反馈对齐到具体问法）
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # AI 回答原文（快照，便于离线评测；不含来源文本）
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 命中模式：hit（检索命中）/ fallback（LLM 兜底）
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="fallback")
    # 命中来源的 doc_id 列表（JSON 数组），供评测定位
    source_doc_ids: Mapped[list] = mapped_column(JSON, nullable=True)
    # 可选备注（用户补充原因）
    comment: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
