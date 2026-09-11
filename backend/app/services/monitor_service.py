"""竞品定时监控：重抓 → 提炼 → 差异比对 → 落库变化记录。

背景（路线图 F2）：此前 main.py 的 ``_monitor_job`` 是空壳——每小时只查一次 DB
确认连通，既不抓取也不对比，导致 ``competitor_changes`` 永远为 0，「追踪竞品变化」
这个产品核心价值成了纸面功能。本模块把它替换为真实的监控轮次。

设计要点：
- **只追踪可由页面客观反映的字段**（定位/功能/目标用户/商业模式/定价/优势/不足）。
  name / type / launch_status 属人工判定，不随抓取自动覆盖，避免把「解析器默认值」
  误记成「竞品变化」刷屏。
- **成本熔断即中止本轮**：LLM 提炼是主要成本项，一旦熔断继续跑只会产生无意义请求。
- **单个竞品失败不影响其余**：抓取/解析异常只记日志并计数，轮次继续。
- **只取有值的提炼结果**：抽取不到的字段不动原值，避免把已有信息抹成空。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.competitor import Competitor
from app.models.competitor_change import CompetitorChange
from app.services.change_service import diff_fields, list_stored_competitors
from app.services.fetch_service import CostLimitExceeded, fetch_and_extract

logger = logging.getLogger(__name__)

# 监控可自动覆盖的字段：全部是「页面内容能客观反映」的信息型字段。
# 刻意不含 name/type/launch_status——它们需要人工判定，自动覆盖会制造噪声变化。
_MONITOR_FIELDS = (
    "positioning",
    "features",
    "target_users",
    "business_model",
    "pricing",
    "strengths",
    "weaknesses",
)


@dataclass
class MonitorOutcome:
    """一轮监控的结果摘要（供日志与测试断言）。"""

    attempted: int = 0
    updated: int = 0
    changes: int = 0
    skipped_no_source: int = 0
    failed: int = 0
    cost_blocked: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "updated": self.updated,
            "changes": self.changes,
            "skipped_no_source": self.skipped_no_source,
            "failed": self.failed,
            "cost_blocked": self.cost_blocked,
        }


def _extraction_to_fields(extraction) -> dict:
    """把提炼结果转成「仅含有值字段」的更新字典。"""
    fields: dict = {}
    for name in _MONITOR_FIELDS:
        value = getattr(extraction, name, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        fields[name] = value
    return fields


async def run_monitor_cycle(session: AsyncSession) -> MonitorOutcome:
    """执行一轮监控：遍历已入库竞品，重抓并对比，产生变化记录。

    返回本轮结果摘要；任何单个竞品的异常都不会中断整轮。
    """
    outcome = MonitorOutcome()
    competitors = await list_stored_competitors(session)
    # 限制单轮处理量，控制外网压力与 LLM 成本
    for competitor in competitors[: settings.monitor_max_per_cycle]:
        if not competitor.source_url:
            outcome.skipped_no_source += 1
            continue

        outcome.attempted += 1
        try:
            extraction, _title, _html = await fetch_and_extract(competitor.source_url)
        except CostLimitExceeded:
            # 成本熔断：继续跑只会不断被拒，直接中止本轮
            outcome.cost_blocked = True
            logger.warning("监控中止：LLM 成本已熔断，请到「成本」页确认后继续")
            break
        except Exception as exc:  # noqa: BLE001 - 单点失败不影响整轮
            outcome.failed += 1
            outcome.errors.append(f"#{competitor.id}: {type(exc).__name__}")
            logger.warning(
                "监控抓取失败",
                extra={
                    "extra_fields": {
                        "competitor_id": competitor.id,
                        "url": competitor.source_url,
                        "error": str(exc),
                    }
                },
            )
            continue

        new_fields = _extraction_to_fields(extraction)
        changes = diff_fields(competitor, new_fields)
        if not changes:
            continue

        for obj in _build_monitor_changes(competitor, changes):
            session.add(obj)
        for key, value in new_fields.items():
            setattr(competitor, key, value)

        outcome.updated += 1
        outcome.changes += len(changes)

    if outcome.updated:
        await session.commit()
    return outcome


def _build_monitor_changes(
    competitor: Competitor, changes: list[tuple[str, str | None, str | None]]
) -> list[CompetitorChange]:
    """把 diff 结果构造成变化记录（source=monitor）。"""
    return [
        CompetitorChange(
            competitor_id=competitor.id,
            competitor_name=competitor.name,
            field=field,
            old_value=old,
            new_value=new,
            source="monitor",
        )
        for field, old, new in changes
    ]
