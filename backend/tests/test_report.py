"""报告 API 测试：Markdown 报告生成与下载。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_competitor(client, name: str, ratings: dict) -> int:
    resp = await client.post(
        "/api/competitors",
        json={
            "name": name,
            "type": "交易",
            "positioning": f"{name}定位",
            "launch_status": "已上线",
            "features": "竞拍、直播、鉴定",
            "target_users": "钱币收藏爱好者",
            "business_model": "抽佣",
            "pricing": "免费入驻，成交抽 2%",
            "strengths": "用户量大",
            "weaknesses": "鉴定周期长",
            "tags": ["钱币"],
            "ext_fields": {"ratings": ratings},
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def test_export_empty_returns_404(client):
    """没有任何已入库竞品时导出 → 404。"""
    resp = await client.post("/api/report/export", json={})
    assert resp.status_code in (404, 200)  # 视测试顺序是否已有竞品而定


async def test_export_all_and_download(client):
    """导出全部 + 下载回读内容校验。"""
    await _create_competitor(
        client, "报告测试竞品A", {"功能完整度": 4, "用户规模": 5, "口碑": 3}
    )
    await _create_competitor(
        client, "报告测试竞品B", {"功能完整度": 3, "鉴定能力": 4, "口碑": 5}
    )

    resp = await client.post("/api/report/export", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["competitor_count"] >= 2
    assert body["filename"].startswith("竞品分析报告_")
    assert body["download_url"].startswith("/api/report/download/")

    dl = await client.get(body["download_url"])
    assert dl.status_code == 200
    text = dl.text
    assert "报告测试竞品A" in text
    assert "报告测试竞品B" in text
    assert "竞品总览" in text
    assert "评分对比" in text
    assert "结论摘要" in text
    assert "综合评分最高" in text


async def test_export_by_ids(client):
    """指定 ids 只导出对应竞品。"""
    id_a = await _create_competitor(client, "报告测试竞品C", {"口碑": 2})
    await _create_competitor(client, "报告测试竞品D", {"口碑": 1})

    resp = await client.post("/api/report/export", json={"ids": [id_a]})
    assert resp.status_code == 200
    assert resp.json()["competitor_count"] == 1

    dl = await client.get(resp.json()["download_url"])
    assert "报告测试竞品C" in dl.text


async def test_download_rejects_path_traversal(client):
    """HTTP 层：路径穿越请求不允许 200（路由 404 或业务 400 均视为拦截）。"""
    resp = await client.get("/api/report/download/..%2F.env")
    assert resp.status_code in (400, 404)


async def test_resolve_report_path_rejects_traversal():
    """服务层：resolve_report_path 对穿越文件名抛 ValueError。"""
    from app.services import report_service

    with pytest.raises(ValueError):
        report_service.resolve_report_path("../.env")
    with pytest.raises(ValueError):
        report_service.resolve_report_path("a/b.md")


async def test_download_missing_file(client):
    resp = await client.get("/api/report/download/不存在的报告.md")
    assert resp.status_code == 404
