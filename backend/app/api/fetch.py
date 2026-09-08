"""抓取与校对 API。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.fetcher import UnsafeUrlError
from app.db import get_session
from app.models.competitor import Competitor, DataStatus
from app.schemas.competitor import CompetitorOut
from app.services.fetch_service import CostLimitExceeded, create_draft_from_url

logger = logging.getLogger(__name__)

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
    except CostLimitExceeded as exc:
        # 成本熔断：HTTP 402 语义，携带需征询的信息，前端据此弹框
        raise HTTPException(
            status_code=402,
            detail={
                "code": "cost_limit",
                "reason": exc.reason,
                "approval_token": exc.detail.get("approval_token"),
                "estimate_cost_cny": exc.detail.get("estimate_cost_cny"),
                "total_cost_cny": exc.detail.get("total_cost_cny"),
                "cost_limit_cny": exc.detail.get("cost_limit_cny"),
                "projected_total_cny": exc.detail.get("projected_total_cny"),
                "message": "LLM 调用预估费用已超过限额，是否同意继续本次调用？",
            },
        ) from exc
    except UnsafeUrlError as exc:
        # URL 未通过 SSRF 校验，返回明确的 400
        raise HTTPException(status_code=400, detail=f"URL 不合法: {exc}") from exc
    except Exception as exc:
        # 内部异常不外透细节，记录日志后返回通用提示
        logger.exception("抓取或提炼失败: %s", payload.url)
        raise HTTPException(
            status_code=502, detail="抓取或提炼失败，请稍后重试"
        ) from exc


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
