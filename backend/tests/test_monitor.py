"""竞品定时监控测试（F2）。

覆盖：字段更新与变化落库、无 source_url 跳过、单点失败不中断、成本熔断中止、
空值不覆盖旧值，以及任务开关行为。
"""

import pytest
from sqlalchemy import delete, select

from app.ai.provider import Extraction
from app.db import async_session_factory
from app.models.competitor import Competitor, DataStatus
from app.models.competitor_change import CompetitorChange
from app.services import monitor_service


@pytest.fixture(autouse=True)
async def _clean_competitor_tables():
    """每个用例前清空竞品相关表：监控是「全量遍历已入库竞品」的批处理，
    若不复位，其它用例残留的竞品会混进本轮统计导致断言失真。"""
    async with async_session_factory() as session:
        await session.execute(delete(CompetitorChange))
        await session.execute(delete(Competitor))
        await session.commit()
    yield


async def _make_stored_competitor(**overrides) -> int:
    """建一个「已入库」竞品，返回 id。"""
    defaults = {
        "name": "闲鱼",
        "type": "C2C 闲置交易",
        "positioning": "旧的定位描述",
        "source_url": "https://www.goofish.com/app",
        "data_status": DataStatus.STORED.value,
    }
    defaults.update(overrides)
    async with async_session_factory() as session:
        c = Competitor(**defaults)
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


@pytest.fixture
def _fake_extract(monkeypatch):
    """把 monitor_service 里的 fetch_and_extract 换成可控假实现。"""

    def _install(result=None, exc=None):
        async def _fn(url):
            if exc is not None:
                raise exc
            return result, "标题", "<html></html>"

        monkeypatch.setattr(monitor_service, "fetch_and_extract", _fn)

    return _install


async def _changes_for(competitor_id: int) -> list[CompetitorChange]:
    async with async_session_factory() as session:
        rows = await session.execute(
            select(CompetitorChange).where(
                CompetitorChange.competitor_id == competitor_id
            )
        )
        return list(rows.scalars().all())


async def test_monitor_updates_fields_and_records_changes(_fake_extract):
    """监控抓到新定位 → 更新字段并写入 source=monitor 的变化记录。"""
    cid = await _make_stored_competitor()
    _fake_extract(
        result=Extraction(
            name="闲鱼",
            type="C2C 闲置交易",
            positioning="阿里巴巴旗下 C2C 二手闲置交易平台",
        )
    )

    async with async_session_factory() as session:
        outcome = await monitor_service.run_monitor_cycle(session)

    assert outcome.attempted == 1
    assert outcome.updated == 1
    assert outcome.changes == 1

    async with async_session_factory() as session:
        competitor = await session.get(Competitor, cid)
        assert competitor.positioning == "阿里巴巴旗下 C2C 二手闲置交易平台"

    changes = await _changes_for(cid)
    assert len(changes) == 1
    assert changes[0].source == "monitor"
    assert changes[0].field == "positioning"
    assert changes[0].old_value == "旧的定位描述"
    assert changes[0].new_value == "阿里巴巴旗下 C2C 二手闲置交易平台"


async def test_monitor_skips_competitor_without_source_url(_fake_extract):
    """无 source_url 的竞品无法重抓 → 计入 skipped，不产生请求。"""
    cid = await _make_stored_competitor(source_url=None)
    _fake_extract(result=Extraction(name="x", type="C2C 闲置交易"))

    async with async_session_factory() as session:
        outcome = await monitor_service.run_monitor_cycle(session)

    assert outcome.skipped_no_source >= 1
    assert outcome.attempted == 0
    assert await _changes_for(cid) == []


async def test_monitor_no_change_when_value_identical(_fake_extract):
    """抓取结果与现值一致 → 不更新、不记变化（避免噪声）。"""
    cid = await _make_stored_competitor(positioning="同一个定位")
    _fake_extract(
        result=Extraction(name="闲鱼", type="C2C 闲置交易", positioning="同一个定位")
    )

    async with async_session_factory() as session:
        outcome = await monitor_service.run_monitor_cycle(session)

    assert outcome.updated == 0
    assert await _changes_for(cid) == []


async def test_monitor_empty_extraction_does_not_wipe_existing(_fake_extract):
    """提炼字段为空 → 保留原值，不把已有信息抹成空。"""
    cid = await _make_stored_competitor(positioning="原有定位", features="原有功能")
    _fake_extract(result=Extraction(name="闲鱼", type="C2C 闲置交易"))  # 其余字段均为 None

    async with async_session_factory() as session:
        await monitor_service.run_monitor_cycle(session)

    async with async_session_factory() as session:
        competitor = await session.get(Competitor, cid)
        assert competitor.positioning == "原有定位"
        assert competitor.features == "原有功能"


async def test_monitor_single_failure_does_not_break_cycle(_fake_extract):
    """单个竞品抓取失败 → 计数并继续，不抛异常。"""
    cid = await _make_stored_competitor()
    _fake_extract(exc=RuntimeError("network down"))

    async with async_session_factory() as session:
        outcome = await monitor_service.run_monitor_cycle(session)

    assert outcome.failed >= 1
    assert outcome.updated == 0
    assert outcome.errors
    assert await _changes_for(cid) == []


async def test_monitor_stops_on_cost_limit(_fake_extract):
    """成本熔断 → 立即中止本轮（继续跑只会被不断拒绝）。"""
    from app.services.fetch_service import CostLimitExceeded

    _make = await _make_stored_competitor()
    assert _make is not None
    _fake_extract(exc=CostLimitExceeded("limit_exceeded", {"approval_token": "t"}))

    async with async_session_factory() as session:
        outcome = await monitor_service.run_monitor_cycle(session)

    assert outcome.cost_blocked is True
    assert outcome.attempted == 1


async def test_monitor_job_skips_when_disabled(monkeypatch):
    """开关关闭时任务直接返回，不做任何抓取。"""
    from app.config import settings as app_settings
    from app.main import _monitor_job

    monkeypatch.setattr(app_settings, "monitor_enabled", False)
    await _monitor_job()  # 不应抛异常，也不应有外部调用


async def test_monitor_job_runs_cycle_when_enabled(monkeypatch):
    """开关打开时任务执行一轮监控，并把结果写进日志。"""
    from app.config import settings as app_settings
    from app.main import _monitor_job

    called = {"n": 0}

    async def _fake_cycle(session):
        called["n"] += 1
        return monitor_service.MonitorOutcome(attempted=2, changes=3)

    monkeypatch.setattr(app_settings, "monitor_enabled", True)
    monkeypatch.setattr(monitor_service, "run_monitor_cycle", _fake_cycle)

    await _monitor_job()
    assert called["n"] == 1


# ---------------- 手动触发端点（admin） ----------------


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _admin_headers(client) -> dict:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_trigger_monitor_requires_auth(client):
    """未登录不得触发监控（会发起真实抓取并产生费用）。"""
    assert (await client.post("/api/changes/monitor")).status_code == 401


async def test_trigger_monitor_requires_admin(client):
    """普通用户触发监控 → 403。"""
    import secrets

    username = f"plain_{secrets.token_hex(4)}"
    reg = await client.post(
        "/api/auth/register", json={"username": username, "password": "goodpass123"}
    )
    token = reg.json()["access_token"]
    resp = await client.post(
        "/api/changes/monitor", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_trigger_monitor_runs_real_cycle(client, _fake_extract):
    """管理员手动触发：真实跑一轮并返回统计与提示。"""
    cid = await _make_stored_competitor()
    _fake_extract(
        result=Extraction(
            name="闲鱼", type="C2C 闲置交易", positioning="全新的定位描述"
        )
    )

    resp = await client.post("/api/changes/monitor", headers=await _admin_headers(client))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checked"] == 1
    assert body["changes"] == 1
    assert body["cost_blocked"] is False
    assert "监控完成" in body["message"]
    # 变化已落库
    changes = await _changes_for(cid)
    assert changes and changes[0].source == "monitor"


async def test_trigger_monitor_reports_cost_blocked(client, _fake_extract):
    """成本熔断时返回明确标记与提示。"""
    from app.services.fetch_service import CostLimitExceeded

    await _make_stored_competitor()
    _fake_extract(exc=CostLimitExceeded("limit_exceeded", {"approval_token": "t"}))

    resp = await client.post("/api/changes/monitor", headers=await _admin_headers(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_blocked"] is True
    assert "成本熔断" in body["message"]
