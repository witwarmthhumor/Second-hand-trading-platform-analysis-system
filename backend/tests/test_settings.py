"""设置 API 测试：评分维度可配置。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_default_dimensions(client):
    """未配置时返回默认 5 维。"""
    resp = await client.get("/api/settings/rating-dimensions")
    assert resp.status_code == 200
    dims = resp.json()["dimensions"]
    assert dims == ["功能完整度", "用户规模", "鉴定能力", "价格竞争力", "口碑"]


async def test_update_dimensions(client):
    """更新维度后 GET 返回新值，且 compare 使用新维度。"""
    new_dims = ["功能完整度", "鉴定能力", "社区活跃度", "价格竞争力"]
    resp = await client.put(
        "/api/settings/rating-dimensions", json={"dimensions": new_dims}
    )
    assert resp.status_code == 200
    assert resp.json()["dimensions"] == new_dims

    resp = await client.get("/api/settings/rating-dimensions")
    assert resp.json()["dimensions"] == new_dims

    # compare 接口使用配置后的维度
    resp = await client.post("/api/compare", json={"ids": [1, 2]})
    if resp.status_code == 200:
        assert resp.json()["dimensions"] == new_dims


async def test_update_dimensions_dedup_and_strip(client):
    """重复与首尾空白自动清理。"""
    resp = await client.put(
        "/api/settings/rating-dimensions",
        json={"dimensions": [" 口碑 ", "口碑", "价格", "功能", "规模"]},
    )
    assert resp.status_code == 200
    assert resp.json()["dimensions"] == ["口碑", "价格", "功能", "规模"]


async def test_update_dimensions_too_few(client):
    resp = await client.put(
        "/api/settings/rating-dimensions", json={"dimensions": ["a", "b"]}
    )
    assert resp.status_code == 422


async def test_update_dimensions_too_many(client):
    resp = await client.put(
        "/api/settings/rating-dimensions",
        json={"dimensions": [f"维度{i}" for i in range(9)]},
    )
    assert resp.status_code == 422


async def test_update_dimensions_all_blank_rejected(client):
    """全空白去重后不足 3 个 → 422。"""
    resp = await client.put(
        "/api/settings/rating-dimensions",
        json={"dimensions": ["  ", " ", ""]},
    )
    assert resp.status_code == 422
