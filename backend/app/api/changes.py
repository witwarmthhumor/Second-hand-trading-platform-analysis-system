"""竞品变化记录查询 API。

- GET  /api/changes          ：查询变化记录（可按竞品、字段筛选，倒序）
- POST /api/changes/monitor  ：手动触发一轮监控（重抓→提炼→差异比对→落库变化）
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models.competitor_change import CompetitorChange
from app.models.user import User
from app.services.monitor_service import run_monitor_cycle

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
async def trigger_monitor(
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> dict:
    """手动触发一轮监控（管理员）。

    真实执行「重抓 source_url → 提炼 → 差异比对 → 落库变化记录」。
    与定时任务的差别：手动触发不受 MONITOR_ENABLED 开关限制（这是显式动作），
    但仍受 LLM 成本熔断约束——熔断时中止本轮并在返回里标记 cost_blocked。
    """
    outcome = await run_monitor_cycle(session)
    result = outcome.as_dict()
    if outcome.cost_blocked:
        message = "已触发 LLM 成本熔断，本轮中止；请到「成本」页确认后重试"
    else:
        message = (
            f"监控完成：检查 {outcome.attempted} 个竞品，"
            f"更新 {outcome.updated} 个，记录 {outcome.changes} 条变化"
        )
    return {
        "checked": outcome.attempted,
        "changes": outcome.changes,
        "updated": outcome.updated,
        "failed": outcome.failed,
        "skipped_no_source": outcome.skipped_no_source,
        "cost_blocked": outcome.cost_blocked,
        "message": message,
        **result,
    }
