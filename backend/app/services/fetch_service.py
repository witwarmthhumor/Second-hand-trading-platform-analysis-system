"""抓取 + 提炼服务：把「抓取 → LLM 提炼 → draft/待校对」串成业务动作。

成本熔断：每次发起 LLM 提炼前先经 CostTracker 预检，估算超阈值或已熔断则抛
CostLimitExceeded，由 API 层转成"需征询"响应；真实调用后按 token 用量记账。

降级（R1）：provider 是否计费由 ``billable`` 能力位决定（而非具体类型），
远程实现失败时由 FallbackProvider 自动回退规则提取，链路不中断。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cost import get_cost_tracker
from app.ai.provider import Extraction, get_provider
from app.crawler.fetcher import fetch_url
from app.models.competitor import Competitor, DataStatus


class CostLimitExceeded(Exception):
    """LLM 成本估算/累计超阈值，需用户同意后才能继续。"""

    def __init__(self, reason: str, detail: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


async def fetch_and_extract(url: str) -> tuple[Extraction, str, str]:
    """抓取网页并用 LLM/规则提炼。返回 (提炼结果, 标题, 原始HTML)。

    提炼前先按输入规模预检成本，超阈值或已熔断则抛 CostLimitExceeded。
    仅计费 provider（远程 LLM）参与预检与记账；规则兜底不计费。
    """
    page = await fetch_url(url)
    provider = get_provider()
    billable = bool(getattr(provider, "billable", False))

    if billable:
        input_text = f"{url}\n{page.title}\n{page.html[:4000]}"
        estimated_tokens = len(input_text) // 2
        tracker = get_cost_tracker()
        await tracker.restore_from_db()
        verdict = tracker.precheck(estimated_tokens)
        if not verdict["allowed"]:
            raise CostLimitExceeded(verdict["reason"], verdict)

    extraction = await provider.extract(url=url, html=page.html, title=page.title)

    # 真实用量记账（仅计费 provider；远程失败回退规则时用量为 0，记账天然为 0）
    if billable:
        get_cost_tracker().record(
            getattr(provider, "last_input_tokens", 0),
            getattr(provider, "last_output_tokens", 0),
        )
    return extraction, page.title, page.html


async def create_draft_from_url(url: str, session: AsyncSession) -> Competitor:
    """从 URL 抓取并提炼，落库为「待校对」草稿。"""
    extraction, title, html = await fetch_and_extract(url)

    competitor = Competitor(
        name=extraction.name or title or url,
        type=extraction.type,
        positioning=extraction.positioning,
        features=extraction.features,
        target_users=extraction.target_users,
        business_model=extraction.business_model,
        pricing=extraction.pricing,
        strengths=extraction.strengths,
        weaknesses=extraction.weaknesses,
        tags=extraction.tags,
        website=url,
        source_url=url,
        source_raw=html,
        data_status=DataStatus.PENDING_REVIEW.value,
    )
    session.add(competitor)
    await session.commit()
    await session.refresh(competitor)
    return competitor
