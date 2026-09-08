"""爬虫层：httpx 抓取 + 站点解析器（可插拔）。

当前为通用抓取（httpx + BeautifulSoup4 提取标题）；后续如需针对特定站点定制解析，
可在此注册对应的解析器。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


@dataclass
class FetchedPage:
    """抓取结果。"""

    url: str
    title: str
    html: str


async def fetch_url(url: str) -> FetchedPage:
    """抓取指定 URL，返回标题与原始 HTML。"""
    async with httpx.AsyncClient(
        timeout=30, headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    title = ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    except Exception:
        title = ""

    return FetchedPage(url=url, title=title, html=html)
