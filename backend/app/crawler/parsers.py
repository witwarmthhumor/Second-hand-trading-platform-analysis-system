"""可插拔站点解析器：把不同平台的页面归一化成结构化线索。

背景（路线图 F1）：闲鱼/转转/爱回收等站点大量内容由前端 JS 渲染，通用 BS4 抓取
往往只拿到骨架。解析器针对各站点**服务端可得**的元信息做定向抽取——og: 系列标签、
JSON-LD 结构化数据、description/keywords、正文关键板块——产出「标题 + 线索键值对
+ 正文」，供后续 LLM/规则提炼使用，显著提升提炼字段质量。

设计原则：
- ``SiteParser`` 协议：``key`` / ``matches(url)`` / ``parse(html, title)``。
- 注册表按声明顺序匹配，命中即用；都不命中则回落通用解析器。
- 解析器**只做尽力而为的抽取**：任何子步骤失败都吞掉并降级为「该字段缺失」，
  绝不因页面结构变化而抛异常拖垮抓取主链路。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse


@dataclass
class ParsedPage:
    """解析结果：统一的结构化线索，与具体站点解耦。"""

    url: str
    title: str
    site: str
    # 线索键值对，如 {"平台名": "闲鱼", "定位": "...", "关键词": "..."}
    hints: dict[str, str] = field(default_factory=dict)
    # 归一化后的正文（供 LLM 提炼，替代对原始 HTML 的粗暴截断）
    text: str = ""


class SiteParser(Protocol):
    """站点解析器协议。"""

    key: str

    def matches(self, url: str) -> bool:
        """该解析器是否适用于此 URL。"""
        ...

    def parse(self, html: str, title: str = "", url: str = "") -> ParsedPage:
        """解析页面，返回结构化线索。"""
        ...


# ---------------- 公共抽取工具 ----------------


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _text_of(soup, *selectors: str) -> str:
    """按选择器顺序找第一个非空文本。"""
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            t = node.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _meta(soup, name: str) -> str:
    """读取 meta：兼容 name= 与 property=（og: 系列）两种写法。"""
    for attrs in ({"name": name}, {"property": name}):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            return node["content"].strip()
    return ""


def _json_ld_objects(soup) -> list[dict]:
    """抽取页面内所有 application/ld+json 对象（容错：解析失败的跳过）。"""
    out: list[dict] = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(item for item in data if isinstance(item, dict))
    return out


def _json_ld_field(objs: list[dict], *keys: str) -> str:
    """在 JSON-LD 对象里找第一个命中的字段（支持嵌套 @graph）。"""
    for obj in objs:
        for key in keys:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        graph = obj.get("@graph")
        if isinstance(graph, list):
            nested = _json_ld_field([g for g in graph if isinstance(g, dict)], *keys)
            if nested:
                return nested
    return ""


def _body_text(soup, limit: int = 3000) -> str:
    """正文文本：优先 article/main，否则全页段落拼接。"""
    for sel in ("article", "main", "#content", ".content"):
        node = soup.select_one(sel)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 80:
                return text[:limit]
    parts = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "h1", "h2", "h3"])]
    joined = "\n".join(p for p in parts if p)
    return joined[:limit]


class GenericParser:
    """通用解析器：任何站点可用的兜底，抽取 title/og/描述/关键词/正文。"""

    key = "generic"

    def matches(self, url: str) -> bool:  # 兜底：永远匹配
        return True

    def parse(self, html: str, title: str = "", url: str = "") -> ParsedPage:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
        ld = _json_ld_objects(soup)

        resolved_title = (
            title
            or _meta(soup, "og:title")
            or (soup.title.string.strip() if soup.title and soup.title.string else "")
        )
        hints: dict[str, str] = {}
        site_name = _meta(soup, "og:site_name") or _json_ld_field(ld, "name")
        if site_name:
            hints["平台名"] = site_name
        desc = (
            _meta(soup, "og:description")
            or _meta(soup, "description")
            or _json_ld_field(ld, "description")
        )
        if desc:
            hints["定位/简介"] = desc
        keywords = _meta(soup, "keywords")
        if keywords:
            hints["关键词"] = re.sub(r"\s+", " ", keywords)

        return ParsedPage(
            url=url,
            title=resolved_title.strip(),
            site=self.key,
            hints=hints,
            text=_body_text(soup),
        )


class _NamedSiteParser(GenericParser):
    """带站点标识与域名匹配的解析器基类，附加站点专属线索抽取钩子。"""

    key = "site"
    # 子类覆盖：命中任一域名后缀即适用
    domains: tuple[str, ...] = ()
    # 子类覆盖：命中任一子串即适用（用于无固定域名的场景，如 2.taobao.com/闲鱼）
    host_keywords: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        host = _host(url)
        if not host:
            return False
        if any(host == d or host.endswith("." + d) for d in self.domains):
            return True
        return any(kw in host for kw in self.host_keywords)

    def site_hints(self, soup, ld: list[dict]) -> dict[str, str]:
        """站点专属线索（默认无）。"""
        return {}

    def parse(self, html: str, title: str = "", url: str = "") -> ParsedPage:
        from bs4 import BeautifulSoup

        page = super().parse(html, title, url)
        soup = BeautifulSoup(html or "", "html.parser")
        ld = _json_ld_objects(soup)
        extra = self.site_hints(soup, ld)
        # 站点专属线索优先于通用线索
        page.hints = {**page.hints, **extra}
        page.site = self.key
        return page


class XianyuParser(_NamedSiteParser):
    """闲鱼（C2C 闲置交易）。"""

    key = "xianyu"
    domains = ("goofish.com",)
    host_keywords = ("xianyu",)

    def matches(self, url: str) -> bool:
        host = _host(url)
        return super().matches(url) or host.endswith("2.taobao.com")

    def site_hints(self, soup, ld: list[dict]) -> dict[str, str]:
        hints: dict[str, str] = {"平台类型": "C2C 闲置交易"}
        app_desc = _text_of(soup, ".app-desc", ".description", '[class*="intro"]')
        if app_desc:
            hints["定位/简介"] = app_desc
        return hints


class ZhuanzhuanParser(_NamedSiteParser):
    """转转（二手交易 + 验机质检转卖）。"""

    key = "zhuanzhuan"
    domains = ("zhuanzhuan.com",)
    host_keywords = ("zhuanzhuan",)

    def site_hints(self, soup, ld: list[dict]) -> dict[str, str]:
        hints: dict[str, str] = {"平台类型": "C2C 闲置交易"}
        # 转转主打「验机/质检」，页面上常有对应板块文案
        quality = _text_of(soup, '[class*="quality"]', '[class*="inspect"]')
        if quality:
            hints["质检服务"] = quality
        return hints


class AihuishouParser(_NamedSiteParser):
    """爱回收（回收置换）。"""

    key = "aihuishou"
    domains = ("aihuishou.com",)
    host_keywords = ("aihuishou",)

    def site_hints(self, soup, ld: list[dict]) -> dict[str, str]:
        hints: dict[str, str] = {"平台类型": "回收置换"}
        # 回收类站点常见「回收价格/品类」文案，抽取作为定价线索
        price = _text_of(soup, '[class*="price"]', '[class*="quote"]')
        if price:
            hints["定价/报价"] = price
        return hints


# 注册表：按顺序匹配，命中即用；generic 始终兜底（置于末尾）
_PARSERS: tuple[SiteParser, ...] = (
    XianyuParser(),
    ZhuanzhuanParser(),
    AihuishouParser(),
    GenericParser(),
)


def find_parser(url: str) -> SiteParser:
    """按注册顺序返回首个匹配的解析器（generic 兜底，必定有返回）。"""
    for parser in _PARSERS:
        # 站点解析器可能因 URL 结构异常而匹配失败，兜底不放异常到主链路
        try:
            if parser.matches(url):
                return parser
        except Exception:  # noqa: BLE001 - 匹配阶段任何异常都视为不匹配
            continue
    return _PARSERS[-1]


def parse_page(url: str, html: str, title: str = "") -> ParsedPage:
    """解析页面：选解析器 → 抽取线索；解析失败也返回最小可用结果。"""
    parser = find_parser(url)
    try:
        return parser.parse(html, title=title, url=url)
    except Exception:  # noqa: BLE001 - 解析失败降级为通用解析，不影响抓取
        return GenericParser().parse(html, title=title, url=url)


def available_sites() -> list[str]:
    """已注册的站点解析器标识（供设置页/文档展示）。"""
    return [p.key for p in _PARSERS if p.key != "generic"]


def format_hints(page: ParsedPage) -> str:
    """把线索格式化成提示文本，供 LLM 提炼时参考。"""
    if not page.hints:
        return ""
    lines = [f"{k}: {v}" for k, v in page.hints.items() if v]
    return "\n".join(lines)
