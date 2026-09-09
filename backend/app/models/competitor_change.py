"""竞品字段变化记录 ORM 模型。

用于定时监控 / 编辑竞品时的变化追踪：记录某竞品某字段从旧值变为新值，
供「动态监控」页展示与审计。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CompetitorChange(Base):
    """竞品字段变化记录（追加型，不可变）。"""

    __tablename__ = "competitor_changes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    competitor_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    competitor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    field: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变化来源：manual（人工编辑）/ monitor（定时监控抓取）
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
