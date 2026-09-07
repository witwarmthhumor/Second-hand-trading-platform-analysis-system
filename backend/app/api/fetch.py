"""抓取与校对 API。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.competitor import Competitor, DataStatus
from app.schemas.competitor import CompetitorOut
from app.services.fetch_service import create_draft_from_url

router = APIRouter(prefix="/api/fetch", tags=["fetch"])


class FetchRequest(BaseModel):
    url: str = Field(..., min_length=1, description="目标竞品页面 URL")


@router.post("", response_model=CompetitorOut, status_code=201)
async def fetch_competitor(
    payload: FetchRequest, session: AsyncSession = Depends(get_session)
) -> Competitor:
    """抓取 URL 并用 LLM/规则提炼，落库为「待校对」草稿。"""
    try:
        return await create_draft_from_url(payload.url, session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"抓取或提炼失败: {exc}") from exc


@router.post("/{competitor_id}/approve", response_model=CompetitorOut)
async def approve_competitor(
    competitor_id: int, session: AsyncSession = Depends(get_session)
) -> Competitor:
    """人工校对通过：待校对 → 已入库。"""
    competitor = await session.get(Competitor, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="竞品不存在")
    competitor.data_status = DataStatus.STORED.value
    await session.commit()
    await session.refresh(competitor)
    return competitor
