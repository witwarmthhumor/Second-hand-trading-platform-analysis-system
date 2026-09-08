"""竞品对比接口测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _payload(name, ratings=None):
    return {
        "name": name,
        "type": "C2C 闲置交易",
        "positioning": "测试定位",
        "ext_fields": {"ratings": ratings or {}},
    }


async def _create(client, name, ratings):
    r = await client.post("/api/competitors", json=_payload(name, ratings))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_compare_returns_ratings(client):
    """正常：对比返回维度 + 各竞品评分（缺省维度补 0）。"""
    id_a = await _create(client, "竞品A", {"功能完整度": 5, "口碑": 4})
    id_b = await _create(client, "竞品B", {"功能完整度": 2, "用户规模": 3})

    r = await client.post("/api/compare", json={"ids": [id_a, id_b]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dimensions"] == [
        "功能完整度",
        "用户规模",
        "质检能力",
        "价格竞争力",
        "口碑",
    ]
    assert len(body["items"]) == 2

    by_name = {x["name"]: x["ratings"] for x in body["items"]}
    assert by_name["竞品A"]["功能完整度"] == 5
    assert by_name["竞品A"]["口碑"] == 4
    assert by_name["竞品A"]["用户规模"] == 0  # 缺省补 0
    assert by_name["竞品B"]["功能完整度"] == 2
    assert by_name["竞品B"]["用户规模"] == 3

    await client.delete(f"/api/competitors/{id_a}")
    await client.delete(f"/api/competitors/{id_b}")


async def test_compare_empty_ids_422(client):
    """异常：空 id 列表被校验拦截（min_length=2）。"""
    r = await client.post("/api/compare", json={"ids": []})
    assert r.status_code == 422


async def test_compare_not_found_404(client):
    """异常：id 都不存在或未入库，返回 404。"""
    r = await client.post("/api/compare", json={"ids": [99999998, 99999999]})
    assert r.status_code == 404
