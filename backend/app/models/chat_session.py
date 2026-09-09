"""RAG 问答会话 ORM 模型。

用于多轮对话记忆：一次对话会话（session_id）关联多轮问答历史，
历史消息以 JSON 数组存于 messages 字段，注入 LLM 兜底上下文时取最近若干轮。

会话按用户隔离：user_id 关联归属用户，普通用户仅见自己的会话，admin 可见全部。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatSession(Base):
    """问答会话：session_id 唯一，messages 存 [{role, content}, ...]。"""

    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 归属用户：外键关联 users.id，按用户隔离会话
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 历史消息 JSON：[{"role": "user"/"assistant", "content": "..."}, ...]
    messages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,  # 会话列表按 updated_at 倒序查询，加索引加速
    )
