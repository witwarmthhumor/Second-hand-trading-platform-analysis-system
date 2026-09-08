"""抓取 + 提炼 + 校对 接口测试。

关键点：mock 掉网络依赖（fetch_url 与 LLM provider），验证状态机流转
（抓取 → 待校对 → 确认 → 已入库）与异常处理，不依赖真实外部服务。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.provider import Extraction
from app.main import app


class _FakeProvider:
    """返回固定提炼结果的 mock provider。"""

    async def extract(self, url, html, title=""):
        return Extraction(
            name="闲鱼",
            type="C2C 闲置交易",
            positioning="C2C 二手闲置交易平台",
            features="闲置发布、信用交易、验机服务",
            target_users="个人闲置卖家与买家",
            business_model="C2C 撮合 + 增值服务",
            tags=["C2C", "头部"],
        )


class _FakeFetcher:
    """返回固定 HTML 的 mock fetcher。"""

    async def fetch_url(self, url):
        class _Page:
            pass

        p = _Page()
        p.url = url
        p.title = "闲鱼 - 让你的闲置游起来"
        p.html = "<html><title>闲鱼</title></html>"
        return p


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def _mock_deps(monkeypatch):
    """替换 provider 与 fetcher，避免真实网络调用。"""
    from app.services import fetch_service

    monkeypatch.setattr(fetch_service, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(fetch_service, "fetch_url", _FakeFetcher().fetch_url)


async def test_fetch_creates_pending_review(client, _mock_deps):
    """正常：抓取后落库为「待校对」，字段由提炼结果填充。"""
    resp = await client.post("/api/fetch", json={"url": "https://example.com/x"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "闲鱼"
    assert body["type"] == "C2C 闲置交易"
    assert body["data_status"] == "待校对"
    assert body["source_url"] == "https://example.com/x"

    await client.delete(f"/api/competitors/{body['id']}")


async def test_approve_transitions_to_stored(client, _mock_deps):
    """正常：确认后 待校对 → 已入库。"""
    created = (
        await client.post("/api/fetch", json={"url": "https://example.com/y"})
    ).json()
    cid = created["id"]
    assert created["data_status"] == "待校对"

    resp = await client.post(f"/api/fetch/{cid}/approve")
    assert resp.status_code == 200
    assert resp.json()["data_status"] == "已入库"

    await client.delete(f"/api/competitors/{cid}")


async def test_approve_not_found_404(client):
    """异常：确认不存在的 id 返回 404。"""
    resp = await client.post("/api/fetch/99999999/approve")
    assert resp.status_code == 404


async def test_fetch_failure_returns_502(client, monkeypatch):
    """异常：抓取失败返回 502，不落库。"""
    from app.services import fetch_service

    async def _boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(fetch_service, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(fetch_service, "fetch_url", _boom)

    resp = await client.post("/api/fetch", json={"url": "https://example.com/z"})
    assert resp.status_code == 502
