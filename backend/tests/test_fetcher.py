"""SSRF 防护：URL 校验逻辑测试（纯逻辑，不依赖网络）。

覆盖 validate_public_url 对协议、内网/回环地址、解析到内网的域名的拦截，
以及合法公网地址的放行；另含 fetch_url 的重定向 SSRF 防护回归测试
（用 httpx.MockTransport 注入，不发真实网络请求）。
"""

import httpx
import pytest

from app.crawler.fetcher import UnsafeUrlError, fetch_url, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.0.0.1:8000/admin",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",  # 云元数据地址
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
    ],
)
def test_rejects_private_or_loopback(url):
    """内网/回环/保留地址应被拒绝。"""
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://localhost/",
    ],
)
def test_rejects_non_http_scheme(url):
    """非 http/https 协议应被拒绝。"""
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.xianyu.com/",
        "http://example.com/",
        "https://www.zhuanzhuan.com/item/123",
    ],
)
def test_accepts_public_url(url):
    """合法公网 http/https 地址应放行。"""
    validate_public_url(url)  # 不抛异常即通过


def test_rejects_empty_host():
    """无主机名的 URL 应被拒绝。"""
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://")


async def test_fetch_url_success_on_public_site():
    """公网 URL 正常抓取：回归测试——response event hook 必须是协程函数。

    历史事故：hook 曾是 sync def，httpx 对 AsyncClient 的 hook 直接 await，
    导致任何请求都抛 ``TypeError: object NoneType can't be used in 'await'``，
    前端表现为「抓取或提炼失败，请稍后重试」。本测试在旧实现下必然失败。
    """
    html = "<html><head><title>拍拍网</title></head><body>ok</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    page = await fetch_url(
        "https://www.paipai.com/", transport=httpx.MockTransport(handler)
    )
    assert page.title == "拍拍网"
    assert "拍拍网" in page.html


async def test_fetch_url_blocks_redirect_to_private():
    """重定向跳转到内网地址应被 hook 拦截（防 SSRF 绕过）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example.com":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
            )
        return httpx.Response(200, text="should not reach here")

    with pytest.raises(UnsafeUrlError):
        await fetch_url(
            "https://evil.example.com/", transport=httpx.MockTransport(handler)
        )
