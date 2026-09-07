"""应用设置 API：当前管理雷达图评分维度。

配置存 app_settings 表（key="rating_dimensions"），无记录时返回默认维度。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.setting import AppSetting

router = APIRouter(prefix="/api/settings", tags=["settings"])

RATING_DIMENSIONS_KEY = "rating_dimensions"

# 默认评分维度（未配置时使用）
DEFAULT_RATING_DIMENSIONS = ["功能完整度", "用户规模", "鉴定能力", "价格竞争力", "口碑"]

# 维度数量限制（太少雷达图没意义，太多图难以阅读）
MIN_DIMENSIONS = 3
MAX_DIMENSIONS = 8


class RatingDimensionsOut(BaseModel):
    dimensions: list[str]


class RatingDimensionsIn(BaseModel):
    dimensions: list[str] = Field(
        ..., min_length=MIN_DIMENSIONS, max_length=MAX_DIMENSIONS
    )


async def get_rating_dimensions(session: AsyncSession) -> list[str]:
    """读取评分维度（供 compare / report 复用），未配置返回默认值。"""
    stmt = select(AppSetting).where(AppSetting.key == RATING_DIMENSIONS_KEY)
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    if setting and isinstance(setting.value, list) and setting.value:
        return [str(d) for d in setting.value]
    return list(DEFAULT_RATING_DIMENSIONS)


def _validate_dimensions(dims: list[str]) -> list[str]:
    """去空白、去重、去空串。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for d in dims:
        d = d.strip()
        if d and d not in seen:
            seen.add(d)
            cleaned.append(d)
    return cleaned


@router.get("/rating-dimensions", response_model=RatingDimensionsOut)
async def read_rating_dimensions(
    session: AsyncSession = Depends(get_session),
) -> RatingDimensionsOut:
    return RatingDimensionsOut(dimensions=await get_rating_dimensions(session))


@router.put("/rating-dimensions", response_model=RatingDimensionsOut)
async def update_rating_dimensions(
    payload: RatingDimensionsIn, session: AsyncSession = Depends(get_session)
) -> RatingDimensionsOut:
    """更新评分维度。注意：已录入竞品的评分按维度名匹配，改名/删维度后旧评分对应项记 0。"""
    dims = _validate_dimensions(payload.dimensions)
    if not (MIN_DIMENSIONS <= len(dims) <= MAX_DIMENSIONS):
        raise HTTPException(
            status_code=422,
            detail=f"评分维度需 {MIN_DIMENSIONS}~{MAX_DIMENSIONS} 个（去重去空后为 {len(dims)} 个）",
        )

    stmt = select(AppSetting).where(AppSetting.key == RATING_DIMENSIONS_KEY)
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = dims
    else:
        setting = AppSetting(key=RATING_DIMENSIONS_KEY, value=dims)
        session.add(setting)
    await session.commit()
    return RatingDimensionsOut(dimensions=dims)
