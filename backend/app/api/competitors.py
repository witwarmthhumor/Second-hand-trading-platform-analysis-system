"""竞品 CRUD API。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.competitor import Competitor, DataStatus, PlatformType
from app.models.competitor_change import CompetitorChange
from app.schemas.competitor import CompetitorCreate, CompetitorOut, CompetitorUpdate
from app.services.change_service import build_change_objects, diff_fields

router = APIRouter(prefix="/api/competitors", tags=["competitors"])


@router.get("", response_model=list[CompetitorOut])
async def list_competitors(
    keyword: str | None = Query(None, description="按名称模糊搜索"),
    type: PlatformType | None = Query(None, description="按平台类型筛选"),
    data_status: DataStatus | None = Query(None, description="按数据状态筛选"),
    session: AsyncSession = Depends(get_session),
) -> list[Competitor]:
    """列表：支持关键字、类型、状态筛选。"""
    stmt = select(Competitor).order_by(Competitor.created_at.desc())
    if keyword:
        stmt = stmt.where(Competitor.name.ilike(f"%{keyword}%"))
    if type:
        stmt = stmt.where(Competitor.type == type.value)
    if data_status:
        stmt = stmt.where(Competitor.data_status == data_status.value)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{competitor_id}", response_model=CompetitorOut)
async def get_competitor(
    competitor_id: int, session: AsyncSession = Depends(get_session)
) -> Competitor:
    """详情。"""
    competitor = await session.get(Competitor, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="竞品不存在")
    return competitor


@router.post("", response_model=CompetitorOut, status_code=201)
async def create_competitor(
    payload: CompetitorCreate, session: AsyncSession = Depends(get_session)
) -> Competitor:
    """手动录入（默认直接已入库）。"""
    competitor = Competitor(**payload.model_dump())
    session.add(competitor)
    await session.commit()
    await session.refresh(competitor)
    return competitor


@router.put("/{competitor_id}", response_model=CompetitorOut)
async def update_competitor(
    competitor_id: int,
    payload: CompetitorUpdate,
    session: AsyncSession = Depends(get_session),
) -> Competitor:
    """编辑（只更新传入字段）；自动追踪字段变化并记录到变化日志。"""
    competitor = await session.get(Competitor, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="竞品不存在")

    update_data = payload.model_dump(exclude_unset=True)
    # 变化检测：对比更新前旧值与新值，记录发生变化的字段
    changes = diff_fields(competitor, update_data)
    if changes:
        for obj in build_change_objects(competitor, changes, source="manual"):
            session.add(CompetitorChange(**obj))

    for key, value in update_data.items():
        setattr(competitor, key, value)
    await session.commit()
    await session.refresh(competitor)
    return competitor


@router.delete("/{competitor_id}", status_code=204)
async def delete_competitor(
    competitor_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    """删除。"""
    competitor = await session.get(Competitor, competitor_id)
    if competitor is None:
        raise HTTPException(status_code=404, detail="竞品不存在")
    await session.delete(competitor)
    await session.commit()
