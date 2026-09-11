"""竞品变化检测与记录测试。

覆盖：
- diff_fields：字段新旧值对比，只返回真正变化的字段
- 规整：None/空串视为无值
- build_change_objects：diff 结果转 CompetitorChange 构造参数
- API：编辑竞品自动记录变化（integration）
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.change_service import build_change_objects, diff_fields


class _FakeCompetitor:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "闲鱼")
        self.type = kw.get("type", "C2C 闲置交易")
        self.positioning = kw.get("positioning")
        self.website = kw.get("website")
        self.launch_status = kw.get("launch_status")
        self.features = kw.get("features")
        self.target_users = kw.get("target_users")
        self.business_model = kw.get("business_model")
        self.pricing = kw.get("pricing")
        self.strengths = kw.get("strengths")
        self.weaknesses = kw.get("weaknesses")


def test_diff_fields_detects_change():
    old = _FakeCompetitor(pricing="抽佣 0.6%", positioning="个人闲置")
    new = {"pricing": "抽佣 1.0%", "positioning": "个人闲置"}  # pricing 变了
    changes = diff_fields(old, new)
    assert changes == [("pricing", "抽佣 0.6%", "抽佣 1.0%")]


def test_diff_fields_no_change():
    old = _FakeCompetitor(pricing="抽佣 0.6%")
    new = {"pricing": "抽佣 0.6%"}
    assert diff_fields(old, new) == []


def test_diff_fields_normalizes_none_and_empty():
    old = _FakeCompetitor(pricing=None)
    new = {"pricing": ""}  # 空串规整为 None，与旧值 None 等价
    assert diff_fields(old, new) == []

    new2 = {"pricing": "抽佣 1%"}
    assert diff_fields(old, new2) == [("pricing", None, "抽佣 1%")]


def test_build_change_objects():
    comp = _FakeCompetitor(id=3, name="闲鱼")
    changes = [("pricing", "0.6%", "1.0%"), ("positioning", None, "新定位")]
    objs = build_change_objects(comp, changes, source="manual")
    assert len(objs) == 2
    assert objs[0]["competitor_id"] == 3
    assert objs[0]["field"] == "pricing"
    assert objs[0]["source"] == "manual"


# ---------------- 集成：编辑竞品自动记录变化 ----------------


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_update_records_change(client):
    """编辑竞品定价时，自动写入一条变化记录。"""
    # 创建一个竞品
    created = (
        await client.post(
            "/api/competitors",
            json={"name": "测试竞品", "type": "C2C 闲置交易", "pricing": "旧价"},
        )
    ).json()
    cid = created["id"]

    try:
        # 更新定价 → 应触发变化记录
        resp = await client.put(
            f"/api/competitors/{cid}", json={"pricing": "新价"}
        )
        assert resp.status_code == 200

        # 查询变化记录
        changes = await client.get(f"/api/changes?competitor_id={cid}")
        assert changes.status_code == 200
        body = changes.json()
        assert len(body) >= 1
        assert body[0]["field"] == "pricing"
        assert body[0]["old_value"] == "旧价"
        assert body[0]["new_value"] == "新价"
    finally:
        await client.delete(f"/api/competitors/{cid}")


async def test_update_no_change_no_record(client):
    """未变化的更新不产生记录。"""
    created = (
        await client.post(
            "/api/competitors",
            json={"name": "无变化竞品", "type": "回收置换", "pricing": "同价"},
        )
    ).json()
    cid = created["id"]

    try:
        # 更新为相同值 → 不应产生变化记录
        await client.put(f"/api/competitors/{cid}", json={"pricing": "同价"})
        changes = await client.get(f"/api/changes?competitor_id={cid}")
        assert changes.json() == []
    finally:
        await client.delete(f"/api/competitors/{cid}")


async def test_delete_competitor_cascades_changes(client):
    """删除竞品时变化记录一并清除（应用层级联，2026-09-11 加固）。

    CompetitorChange.competitor_id 无外键约束，若删除竞品不清理从属记录，
    会留下 competitor_id 悬空的孤儿行 —— 既让 /api/changes 统计口径失真，
    也是测试库「单跑过、全量挂」类顺序敏感 flaky 的数据泄漏源之一。
    """
    from sqlalchemy import select

    from app.db import async_session_factory
    from app.models.competitor_change import CompetitorChange

    created = (
        await client.post(
            "/api/competitors",
            json={"name": "级联删除竞品", "type": "C2C 闲置交易", "pricing": "旧价"},
        )
    ).json()
    cid = created["id"]

    # 产生一条变化记录
    resp = await client.put(f"/api/competitors/{cid}", json={"pricing": "新价"})
    assert resp.status_code == 200

    async with async_session_factory() as session:
        before = (
            await session.execute(
                select(CompetitorChange).where(CompetitorChange.competitor_id == cid)
            )
        ).scalars().all()
        assert len(before) >= 1

    # 删除竞品 → 从属变化记录必须一并消失
    resp = await client.delete(f"/api/competitors/{cid}")
    assert resp.status_code == 204

    async with async_session_factory() as session:
        after = (
            await session.execute(
                select(CompetitorChange).where(CompetitorChange.competitor_id == cid)
            )
        ).scalars().all()
        assert after == []
