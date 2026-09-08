"""爬虫层：httpx 抓取 + 站点解析器（可插拔）。

当前为通用抓取（httpx + BeautifulSoup4 提取标题）；后续如需针对特定站点定制解析，
可在此注册对应的解析器。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# SSRF 防护：拒绝的私网/回环/保留地址段。
# 169.254.0.0/16 为云平台元数据地址（如 AWS 169.254.169.254），必须拦截。
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("::1"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 唯一本地
    ipaddress.ip_network("fe80::/10"),  # IPv6 链路本地
)


class UnsafeUrlError(ValueError):
    """URL 校验未通过（非 http/https 或指向内网地址）。"""


def validate_public_url(url: str) -> None:
    """校验 URL 仅 http/https 且不指向私网/回环地址，防 SSRF。

    对 host 做 DNS 解析后逐一检查解析出的 IP 是否落在私网段，可同时拦截
    形如 http://127.0.0.1、http://localhost、http://[::1] 及解析到内网的域名。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("仅支持 http/https 协议")
    if not parsed.hostname:
        raise UnsafeUrlError("URL 缺少有效主机名")

    host = parsed.hostname
    # 优先对字面量 IP 直接判断，避免不必要的 DNS 查询
    try:
        addr = ipaddress.ip_address(host)
        _assert_public(addr)
        return
    except ValueError:
        # 非 IP 字面量，走 DNS 解析
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # 无法解析的域名不指向内网（连不上任何地址），后续 httpx 请求会自然失败，
        # 此处放行而非拦截，避免把「网络/DNS 问题」误判为「SSRF 攻击」。
        return

    for info in infos:
        ip = info[4][0]
        _assert_public(ipaddress.ip_address(ip))


def _assert_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if any(addr in net for net in _PRIVATE_NETWORKS):
        raise UnsafeUrlError(f"目标地址指向内网/保留地址: {addr}")


def _redirect_is_safe(request: httpx.Request) -> None:
    """httpx 重定向钩子：跟随重定向前再次校验目标，防止重定向绕过 SSRF 防护。"""
    validate_public_url(str(request.url))


@dataclass
class FetchedPage:
    """抓取结果。"""

    url: str
    title: str
    html: str


async def fetch_url(url: str) -> FetchedPage:
    """抓取指定 URL，返回标题与原始 HTML。

    抓取前先做 SSRF 校验（仅 http/https 且非内网地址）；重定向时通过钩子再次
    校验，防止攻击者用 302 跳转到内网绕过防护。
    """
    validate_public_url(url)

    async with httpx.AsyncClient(
        timeout=30,
        headers=_HEADERS,
        follow_redirects=True,
        event_hooks={"response": [_redirect_is_safe]},
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
