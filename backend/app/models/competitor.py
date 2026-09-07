"""竞品 ORM 模型。"""

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlatformType(str, Enum):
    """平台类型（可扩展）。"""

    APPRAISAL = "鉴定"
    TRADE = "交易"
    MARKET = "行情"
    COMMUNITY = "社区"


class LaunchStatus(str, Enum):
    """上线状态。"""

    LIVE = "已上线"
    BETA = "内测"
    OFFLINE = "已下线"


class DataStatus(str, Enum):
    """数据流转状态机。"""

    DRAFT = "draft"
    PENDING_REVIEW = "待校对"
    STORED = "已入库"


class Competitor(Base):
    """竞品实体，通用字段 + JSONB 扩展字段。"""

    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 基础信息
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    positioning: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    launch_status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 功能与业务
    features: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_users: Mapped[str | None] = mapped_column(String(500), nullable=True)
    business_model: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pricing: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 评价
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    weaknesses: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 扩展字段（不同平台类型的专属字段，无需迁移即可扩展）
    ext_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 采集来源（半自动抓取时记录源 URL 与原始抓取文本）
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 状态
    data_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DataStatus.STORED.value, index=True
    )

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
