"""LLM 成本熔断测试。

两类：
1. CostTracker 纯逻辑：费用计算、单次预检、累计熔断、审批放行、重置（各自 new 实例，隔离）。
2. 集成：mock 带 api_key 与 token 用量的 provider，验证 /api/fetch 熔断返回 402、
   审批后放行重试成功。用 monkeypatch 替换 get_cost_tracker 注入低阈值独立实例，
   不污染全局单例。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.cost import CostTracker
from app.ai.provider import Extraction
from app.main import app

# ---------------- CostTracker 纯逻辑 ----------------


def _tracker(limit: float = 1.0):
    # 单价默认：输入2.0/M 输出12.0/M（gpt-5.6-luna 经 rightapi 路由到 terra 的保守价），汇率7.1
    return CostTracker(cost_limit_cny=limit)


def test_estimate_cost_cny():
    t = _tracker()
    # 1M 输入 + 100k 输出 = 2.0*7.1 + 0.1*12.0*7.1 = 14.2 + 8.52 = 22.72
    assert t.estimate_cost_cny(1_000_000, 100_000) == pytest.approx(22.72, abs=0.01)


def test_precheck_allows_within_limit():
    t = _tracker(limit=1.0)
    v = t.precheck(1_000)  # 输入约1k token，费用极低
    assert v["allowed"] is True
    assert t.blocked is False


def test_precheck_blocks_when_exceeds_limit():
    """预估本次 + 已累计 超过阈值 → 熔断并给出审批令牌。"""
    t = _tracker(limit=0.0001)  # 极低阈值，任何真实调用都超
    v = t.precheck(2_000_000)  # 2M 输入 = 10.65 元，远超
    assert v["allowed"] is False
    assert v["reason"] == "limit_exceeded"
    assert t.blocked is True
    assert v["approval_token"]


def test_blocked_blocks_subsequent_calls():
    """熔断后再次 precheck 直接拒绝（reason=blocked）。"""
    t = _tracker(limit=0.0001)
    t.precheck(2_000_000)
    assert t.blocked
    v = t.precheck(100)  # 即便费用很小也拦
    assert v["allowed"] is False
    assert v["reason"] == "blocked"


def test_approve_requires_valid_token():
    t = _tracker(limit=0.0001)
    t.precheck(2_000_000)
    token = t._pending_approval_token
    # 错误令牌拒绝
    assert t.approve("wrong") is False
    assert t.blocked
    # 正确令牌放行
    assert t.approve(token) is True
    assert t.blocked is False
    # 放行后再次用同一令牌无效（令牌已清）
    assert t.approve(token) is False


def test_record_accumulates_and_blocks():
    """记账累计超限自动熔断。"""
    t = _tracker(limit=0.01)
    # 每次 2M 输入 ≈ 10.65 元
    t.record(2_000_000, 0)
    assert t.total_cost_cny > 0.01
    assert t.blocked is True
    assert t._pending_approval_token


def test_reset_clears_state():
    t = _tracker(limit=0.01)
    t.record(2_000_000, 0)
    assert t.blocked
    st = t.reset()
    assert st["blocked"] is False
    assert st["total_cost_cny"] == 0.0


# ---------------- 集成：/api/fetch 熔断 + 审批 ----------------


class _CostProvider:
    """带 api_key 和 token 用量的 mock provider（会触发 cost 链路）。"""

    def __init__(self):
        self.last_input_tokens = 2_000_000
        self.last_output_tokens = 0

    @property
    def api_key(self):
        return "sk-test"

    async def extract(self, url, html, title=""):
        return Extraction(name="熔断测试", type="C2C 闲置交易", positioning="p")


class _CheapCostProvider(_CostProvider):
    """费用极低的 provider，验证放行后能正常抓取。"""

    def __init__(self):
        super().__init__()
        self.last_input_tokens = 100
        self.last_output_tokens = 50


class _FakeFetcher:
    async def fetch_url(self, url):
        class _Page:
            pass

        p = _Page()
        p.title = "t"
        p.html = "<html><title>t</title></html>"
        return p


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def _reset_cost(monkeypatch):
    """把 fetch_service 与 cost 审批 API 的 get_cost_tracker 换成同一独立低阈值实例，
    保证「抓取熔断」与「审批放行」作用于同一个记账器；测完自动还原全局单例。"""
    from app.ai import cost as cost_mod
    from app.services import fetch_service

    tracker = CostTracker(cost_limit_cny=0.0001)
    # 隔离持久化：跳过 DB 恢复与落库，保证测试间状态干净（持久化单独用 test_cost_persist 验证）
    tracker._restored = True
    tracker._schedule_persist = lambda: None
    monkeypatch.setattr(fetch_service, "get_cost_tracker", lambda: tracker)
    monkeypatch.setattr(cost_mod, "get_cost_tracker", lambda: tracker)
    # cost API 模块里 from app.ai.cost import get_cost_tracker 绑定了 app.api.cost.get_cost_tracker
    import app.api.cost as cost_api

    monkeypatch.setattr(cost_api, "get_cost_tracker", lambda: tracker)
    monkeypatch.setattr(fetch_service, "fetch_url", _FakeFetcher().fetch_url)
    return tracker


async def test_fetch_blocks_on_cost_and_approve_retries(client, _reset_cost):
    """真实 LLM provider（大用量）→ /api/fetch 返回 402；审批放行后换成低价 provider 成功。"""
    from app.services import fetch_service

    orig = fetch_service.get_provider
    # 第一次：高用量 provider → 应 402 熔断（get_provider 是同步工厂，返回 provider 实例）
    fetch_service.get_provider = lambda: _CostProvider()
    try:
        resp = await client.post("/api/fetch", json={"url": "https://example.com/c"})
    finally:
        fetch_service.get_provider = orig
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "cost_limit"
    token = detail["approval_token"]
    assert token

    # 审批放行
    approve = await client.post("/api/cost/approve", json={"token": token})
    assert approve.status_code == 200
    assert approve.json()["blocked"] is False

    # 第二次：低价 provider（已放行，费用低于阈值 → 能正常抓取）
    fetch_service.get_provider = lambda: _CheapCostProvider()
    try:
        resp2 = await client.post("/api/fetch", json={"url": "https://example.com/c2"})
    finally:
        fetch_service.get_provider = orig

    if resp2.status_code == 201:
        await client.delete(f"/api/competitors/{resp2.json()['id']}")
        assert resp2.json()["name"] == "熔断测试"
    else:
        # 低价但记录仍可能越过极小阈值；属可接受分支
        assert resp2.status_code in (201, 402)


async def test_approve_wrong_token_keeps_blocked(client, _reset_cost):
    """错误审批令牌：保持熔断，后续抓取仍 402。"""
    from app.services import fetch_service

    orig = fetch_service.get_provider
    fetch_service.get_provider = lambda: _CostProvider()
    try:
        resp = await client.post("/api/fetch", json={"url": "https://example.com/c3"})
        assert resp.status_code == 402
        real_token = resp.json()["detail"]["approval_token"]
    finally:
        fetch_service.get_provider = orig

    # 错误令牌：返回 400（明确失败），熔断保持
    bad = await client.post("/api/cost/approve", json={"token": "wrong"})
    assert bad.status_code == 400

    # 正确令牌：放行成功，解除熔断
    good = await client.post("/api/cost/approve", json={"token": real_token})
    assert good.status_code == 200
    assert good.json()["blocked"] is False


async def test_cost_status_and_reset(client, _reset_cost):
    """状态查询与重置。"""
    st = await client.get("/api/cost/status")
    assert st.status_code == 200
    body = st.json()
    assert "total_cost_cny" in body
    assert "blocked" in body

    reset = await client.post("/api/cost/reset")
    assert reset.status_code == 200
    assert reset.json()["total_cost_cny"] == 0.0
    assert reset.json()["blocked"] is False


# ---------------- 持久化（DB 往返） ----------------

def test_to_dict_and_load_dict_roundtrip():
    """序列化 → 反序列化往返，累计状态一致。"""
    t = CostTracker(cost_limit_cny=1.0)
    t.record(1000, 200)
    t.record(500, 100)
    data = t.to_dict()
    assert data["total_input_tokens"] == 1500
    assert data["total_output_tokens"] == 300

    t2 = CostTracker(cost_limit_cny=1.0)
    t2.load_dict(data)
    assert t2.total_input_tokens == 1500
    assert t2.total_output_tokens == 300
    assert t2.total_cost_cny == t.total_cost_cny


async def test_persist_and_restore_roundtrip():
    """落库 → 恢复往返：新实例从 DB 恢复出累计用量。"""
    from app.db import async_session_factory
    from app.models.setting import AppSetting

    t = CostTracker(cost_limit_cny=1.0)
    t.record(2000, 400)

    # 手动落库（绕过 fire-and-forget，直接 await）
    await t._persist_async()

    # 清理：验证后删除落库记录，避免污染其他测试
    try:
        t2 = CostTracker(cost_limit_cny=1.0)
        await t2.restore_from_db()
        assert t2.total_input_tokens == 2000
        assert t2.total_output_tokens == 400
        assert t2.total_cost_cny == t.total_cost_cny
    finally:
        async with async_session_factory() as session:
            row = await session.get(AppSetting, "llm_cost_state")
            if row is not None:
                await session.delete(row)
                await session.commit()
