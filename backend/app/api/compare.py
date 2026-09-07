"""竞品对比 API。

雷达图评分维度：以 0~5 整数评分，存于 Competitor.ext_fields 的 "ratings" 键。
维度固定 5 项（业务/工程取舍：MVP 用固定维度，后续可扩展为可配置）。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.competitor import Competitor, DataStatus

router = APIRouter(prefix="/api/compare", tags=["compare"])

# 雷达图 5 维评分
RATING_DIMENSIONS = ["功能完整度", "用户规模", "鉴定能力", "价格竞争力", "口碑"]


class CompareRequest(BaseModel):
    ids: list[int] = Field(..., min_length=2, description="参与对比的竞品 id 列表")


class CompareItem(BaseModel):
    id: int
    name: str
    type: str
    positioning: str | None
    launch_status: str | None
    website: str | None
    features: str | None
    target_users: str | None
    business_model: str | None
    pricing: str | None
    strengths: str | None
    weaknesses: str | None
    ratings: dict[str, int]  # 维度名 → 0~5 评分


class CompareResponse(BaseModel):
    dimensions: list[str]
    items: list[CompareItem]


def _ratings_of(competitor: Competitor) -> dict[str, int]:
    """从 ext_fields 读取评分，缺失维度补 0。"""
    ext = competitor.ext_fields or {}
    raw = ext.get("ratings") or {}
    return {dim: int(raw.get(dim, 0)) for dim in RATING_DIMENSIONS}


@router.post("", response_model=CompareResponse)
async def compare(
    payload: CompareRequest, session: AsyncSession = Depends(get_session)
) -> CompareResponse:
    """按 id 列表返回对比数据（仅已入库竞品参与对比）。"""
    stmt = select(Competitor).where(
        Competitor.id.in_(payload.ids),
        Competitor.data_status == DataStatus.STORED.value,
    )
    result = await session.execute(stmt)
    competitors = list(result.scalars().all())

    if not competitors:
        raise HTTPException(status_code=404, detail="未找到可对比的竞品")

    items = [
        CompareItem(
            id=c.id,
            name=c.name,
            type=c.type,
            positioning=c.positioning,
            launch_status=c.launch_status,
            website=c.website,
            features=c.features,
            target_users=c.target_users,
            business_model=c.business_model,
            pricing=c.pricing,
            strengths=c.strengths,
            weaknesses=c.weaknesses,
            ratings=_ratings_of(c),
        )
        for c in competitors
    ]
    return CompareResponse(dimensions=RATING_DIMENSIONS, items=items)
