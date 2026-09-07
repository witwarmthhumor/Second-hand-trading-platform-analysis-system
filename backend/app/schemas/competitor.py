"""竞品 Pydantic schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.competitor import DataStatus, LaunchStatus, PlatformType


class CompetitorBase(BaseModel):
    """竞品公共字段。"""

    name: str = Field(..., min_length=1, max_length=200)
    type: PlatformType
    positioning: str | None = Field(None, max_length=500)
    website: str | None = Field(None, max_length=500)
    launch_status: LaunchStatus | None = None
    features: str | None = None
    target_users: str | None = Field(None, max_length=500)
    business_model: str | None = Field(None, max_length=500)
    pricing: str | None = Field(None, max_length=500)
    strengths: str | None = None
    weaknesses: str | None = None
    tags: list[str] | None = None
    remark: str | None = None
    ext_fields: dict | None = None
    data_status: DataStatus = DataStatus.STORED


class CompetitorCreate(CompetitorBase):
    """创建竞品。"""


class CompetitorUpdate(BaseModel):
    """更新竞品（全部可选，只更新传入字段）。"""

    name: str | None = Field(None, min_length=1, max_length=200)
    type: PlatformType | None = None
    positioning: str | None = Field(None, max_length=500)
    website: str | None = Field(None, max_length=500)
    launch_status: LaunchStatus | None = None
    features: str | None = None
    target_users: str | None = Field(None, max_length=500)
    business_model: str | None = Field(None, max_length=500)
    pricing: str | None = Field(None, max_length=500)
    strengths: str | None = None
    weaknesses: str | None = None
    tags: list[str] | None = None
    remark: str | None = None
    ext_fields: dict | None = None
    data_status: DataStatus | None = None


class CompetitorOut(CompetitorBase):
    """返回竞品（含 id 与时间戳）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
