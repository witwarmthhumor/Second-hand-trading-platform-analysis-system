"""竞品变化检测服务。

对比竞品字段新旧值，生成变化记录（纯逻辑，便于测试）。
用于人工编辑竞品时自动追踪变化，以及定时监控抓取后的差异记录。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competitor import Competitor, DataStatus

#: 可由页面客观反映、允许自动对比的字段（定位/功能/目标用户/商业模式/定价/优势/不足）。
#: 监控轮次竞品对比口径的唯一实现——name / type /
#: launch_status 需要人工判定，自动覆盖只会制造噪声变化。新增字段前先想清楚
#: 「抓取结果能否可靠反映它」。
CONTENT_FIELDS = (
    "positioning",
    "features",
    "target_users",
    "business_model",
    "pricing",
    "strengths",
    "weaknesses",
)

# 参与变化检测的字段（与竞品核心字段对齐，跳过 id/时间戳/状态）
_TRACKED_FIELDS = (
    "name",
    "type",
    "positioning",
    "website",
    "launch_status",
    "features",
    "target_users",
    "business_model",
    "pricing",
    "strengths",
    "weaknesses",
)


async def list_stored_competitors(session: AsyncSession) -> list[Competitor]:
    """查询所有「已入库」竞品（供定时监控、问答上下文、变化查询共用）。"""
    result = await session.execute(
        select(Competitor).where(Competitor.data_status == DataStatus.STORED.value)
    )
    return list(result.scalars().all())


def _norm(value) -> str | None:
    """规整字段值：None/空串视为无值，其余转字符串。"""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def diff_fields(old: Competitor, new: dict) -> list[tuple[str, str | None, str | None]]:
    """对比旧竞品与新字段值，返回变化列表 [(field, old_value, new_value), ...]。

    old：当前 Competitor 实例；new：更新后的字段字典（只含待比较字段）。
    仅比较 new 中出现且属于追踪字段的键，仅返回真正发生变化的字段（规整后比较）。
    """
    changes: list[tuple[str, str | None, str | None]] = []
    for field in _TRACKED_FIELDS:
        if field not in new:
            continue  # 未更新的字段不参与比较
        old_val = _norm(getattr(old, field, None))
        new_val = _norm(new.get(field))
        if old_val != new_val:
            changes.append((field, old_val, new_val))
    return changes


def build_change_objects(
    competitor: Competitor, changes: list[tuple[str, str | None, str | None]], source: str
) -> list[dict]:
    """把 diff 结果转成 CompetitorChange 的构造参数字典列表。

    source：manual（人工编辑）/ monitor（定时监控）。
    """

    return [
        {
            "competitor_id": competitor.id,
            "competitor_name": competitor.name,
            "field": field,
            "old_value": old,
            "new_value": new,
            "source": source,
        }
        for field, old, new in changes
    ]
