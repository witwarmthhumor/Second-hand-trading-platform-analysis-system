"""SSRF 防护：URL 校验逻辑测试（纯逻辑，不依赖网络）。

覆盖 validate_public_url 对协议、内网/回环地址、解析到内网的域名的拦截，
以及合法公网地址的放行。
"""

import pytest

from app.crawler.fetcher import UnsafeUrlError, validate_public_url


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
