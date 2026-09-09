"""竞品变化记录查询 API。

- GET /api/changes           ：查询变化记录（可按竞品、字段筛选，倒序）
- POST /api/changes/monitor  ：手动触发一次定时监控（对已入库竞品做一次快照检查）
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.competitor_change import CompetitorChange
from app.services.change_service import list_stored_competitors

router = APIRouter(prefix="/api/changes", tags=["changes"])


class ChangeOut(BaseModel):
    id: int
    competitor_id: int
    competitor_name: str
    field: str
    old_value: str | None
    new_value: str | None
    source: str
    changed_at: datetime


@router.get("", response_model=list[ChangeOut])
async def list_changes(
    competitor_id: int | None = Query(None, description="按竞品 id 过滤"),
    field: str | None = Query(None, description="按字段过滤"),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[CompetitorChange]:
    """查询变化记录（按时间倒序）。"""
    stmt = select(CompetitorChange).order_by(CompetitorChange.changed_at.desc())
    if competitor_id:
        stmt = stmt.where(CompetitorChange.competitor_id == competitor_id)
    if field:
        stmt = stmt.where(CompetitorChange.field == field)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.post("/monitor")
async def trigger_monitor(session: AsyncSession = Depends(get_session)) -> dict:
    """手动触发一次定时监控检查。

    对已入库竞品做一次快照（当前为占位实现：仅返回竞品数与时间，
    实际网络抓取由 fetcher 完成，此处避免无人值守的抓取副作用）。
    """
    competitors = await list_stored_competitors(session)
    return {
        "checked": len(competitors),
        "changes": 0,
        "message": "定时监控骨架已就绪；自动抓取对比将在配置网络抓取策略后启用",
    }
