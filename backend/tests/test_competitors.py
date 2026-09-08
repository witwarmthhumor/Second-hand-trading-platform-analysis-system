"""竞品 CRUD 接口测试。

依赖真实 PostgreSQL（Docker 容器，端口 5433）。
用 httpx.AsyncClient + ASGITransport 全程 async，规避 TestClient 与 async engine 的
event loop 冲突。测试自建自清，保证可重复执行。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    """异步测试客户端；lifespan 会自动建表。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _create_payload(**overrides) -> dict:
    base = {
        "name": "闲鱼",
        "type": "C2C 闲置交易",
        "positioning": "阿里旗下 C2C 二手闲置交易平台",
        "website": "https://www.goofish.com",
        "launch_status": "已上线",
        "tags": ["C2C", "头部"],
    }
    base.update(overrides)
    return base


async def test_create_and_get(client):
    """正常：创建后能查回，字段完整。"""
    resp = await client.post("/api/competitors", json=_create_payload(name="测试竞品A"))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "测试竞品A"
    assert created["type"] == "C2C 闲置交易"
    assert created["data_status"] == "已入库"
    assert created["id"] > 0

    got = await client.get(f"/api/competitors/{created['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]

    await client.delete(f"/api/competitors/{created['id']}")


async def test_list_and_filter(client):
    """正常：列表 + 关键字/类型筛选。"""
    a = _create_payload(name="唯一关键字AAA", type="回收置换")
    b = _create_payload(name="普通竞品B", type="C2C 闲置交易")
    id_a = (await client.post("/api/competitors", json=a)).json()["id"]
    id_b = (await client.post("/api/competitors", json=b)).json()["id"]

    resp = await client.get("/api/competitors", params={"keyword": "唯一关键字"})
    names = [x["name"] for x in resp.json()]
    assert "唯一关键字AAA" in names

    resp = await client.get("/api/competitors", params={"type": "回收置换"})
    types = {x["type"] for x in resp.json()}
    assert types == {"回收置换"}

    await client.delete(f"/api/competitors/{id_a}")
    await client.delete(f"/api/competitors/{id_b}")


async def test_update_partial(client):
    """正常：PUT 只更新传入字段。"""
    payload = _create_payload(name="待更新竞品", positioning="旧定位")
    cid = (await client.post("/api/competitors", json=payload)).json()["id"]

    resp = await client.put(f"/api/competitors/{cid}", json={"positioning": "新定位"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["positioning"] == "新定位"
    assert body["name"] == "待更新竞品"  # 未传入字段保持原值

    await client.delete(f"/api/competitors/{cid}")


async def test_delete(client):
    """正常：删除后详情返回 404。"""
    resp = await client.post("/api/competitors", json=_create_payload(name="待删除"))
    cid = resp.json()["id"]
    resp = await client.delete(f"/api/competitors/{cid}")
    assert resp.status_code == 204
    assert (await client.get(f"/api/competitors/{cid}")).status_code == 404


async def test_create_missing_name_422(client):
    """异常：缺 name 被校验拦截（422）。"""
    payload = _create_payload()
    payload.pop("name")
    resp = await client.post("/api/competitors", json=payload)
    assert resp.status_code == 422


async def test_create_invalid_type_422(client):
    """异常：非法 type 被校验拦截（422）。"""
    payload = _create_payload(type="不存在的类型")
    resp = await client.post("/api/competitors", json=payload)
    assert resp.status_code == 422


async def test_get_not_found_404(client):
    """异常：查询不存在的 id 返回 404。"""
    resp = await client.get("/api/competitors/99999999")
    assert resp.status_code == 404
