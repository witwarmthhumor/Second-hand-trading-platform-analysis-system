"""抓取 + 提炼服务：把「抓取 → LLM 提炼 → draft/待校对」串成业务动作。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import Extraction, get_provider
from app.crawler.fetcher import fetch_url
from app.models.competitor import Competitor, DataStatus


async def fetch_and_extract(url: str) -> tuple[Extraction, str, str]:
    """抓取网页并用 LLM/规则提炼。返回 (提炼结果, 标题, 原始HTML)。"""
    page = await fetch_url(url)
    provider = get_provider()
    extraction = await provider.extract(url=url, html=page.html, title=page.title)
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
