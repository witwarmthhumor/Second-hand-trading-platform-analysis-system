"""可观测性测试：日志格式化、路径归一化、指标采集、request_id 透传。

覆盖 R2（结构化日志 + 请求观测）的实现要点，不依赖真实外部服务。
"""

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.logging_config import JsonFormatter, TextFormatter, set_request_id
from app.main import app
from app.metrics import Metrics
from app.middleware import normalize_path


def _record(msg: str = "hello", **extra) -> logging.LogRecord:
    """构造一个 LogRecord，并可注入额外结构化字段。"""
    rec = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        rec.extra_fields = extra
    return rec


def test_json_formatter_emits_valid_json_with_request_id():
    """JSON 形态：一行一个对象，含 request_id 与自定义字段。"""
    set_request_id("req-abc")
    try:
        out = JsonFormatter().format(_record("处理完成", path="/api/x", status=200))
    finally:
        set_request_id("-")
    data = json.loads(out)  # 必须可解析，否则日志采集会失败
    assert data["msg"] == "处理完成"
    assert data["request_id"] == "req-abc"
    assert data["path"] == "/api/x"
    assert data["status"] == 200
    assert data["level"] == "INFO"


def test_text_formatter_appends_extra_fields():
    """文本形态：消息后追加 k=v 后缀，便于肉眼排查。"""
    set_request_id("rid-1")
    try:
        out = TextFormatter().format(_record("请求完成", duration_ms=12.3))
    finally:
        set_request_id("-")
    assert "rid-1" in out
    assert "请求完成" in out
    assert "duration_ms=12.3" in out


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/api/rag/sessions/abc123def456", "/api/rag/sessions/{id}"),
        ("/api/competitors/42", "/api/competitors/{id}"),
        ("/api/competitors/42/approve", "/api/competitors/{id}/approve"),
        ("/health", "/health"),
    ],
)
def test_normalize_path_collapses_dynamic_segments(raw, expected):
    """动态段（数字/长十六进制）归一化，避免指标基数爆炸。"""
    assert normalize_path(raw) == expected


def test_metrics_observe_and_snapshot():
    """指标采集：计数、状态分组、延迟统计正确累加。"""
    m = Metrics()
    m.observe_request("GET", "/health", 200, 5.0)
    m.observe_request("GET", "/health", 200, 15.0)
    m.observe_request("POST", "/api/rag/ask", 500, 2500.0)
    snap = m.snapshot()

    assert snap["requests_total"] == 3
    assert snap["status_classes"] == {"2xx": 2, "5xx": 1}
    assert snap["latency_max_ms"] == 2500.0
    assert snap["latency_avg_ms"] == pytest.approx((5 + 15 + 2500) / 3, abs=0.01)
    # top_routes 按次数倒序
    assert snap["top_routes"][0]["path"] == "/health"


def test_metrics_observe_error_counts():
    """未捕获异常单独计数。"""
    m = Metrics()
    m.observe_error()
    assert m.snapshot()["unhandled_errors"] == 1


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_request_id_header_returned(client):
    """每个响应都带 X-Request-ID；上游传入时沿用上游值。"""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")

    resp2 = await client.get("/health", headers={"X-Request-ID": "upstream-123"})
    assert resp2.headers.get("X-Request-ID") == "upstream-123"


async def test_metrics_endpoint_exposes_snapshot(client):
    """/metrics 返回可观测快照，且访问 /health 会被计入。"""
    await client.get("/health")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests_total"] >= 1
    assert "latency_buckets" in body
    assert "uptime_seconds" in body


async def test_health_exposes_scheduler_and_security_warnings(client):
    """健康检查暴露调度器状态与启动自检告警字段。"""
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert "scheduler" in body
    assert isinstance(body["security_warnings"], list)


def test_collect_startup_warnings_flags_weak_default_and_missing_llm(monkeypatch):
    """启动自检：弱默认口令与未配置 LLM 都应被点出。"""
    from app import diagnostics

    monkeypatch.setattr(diagnostics.settings, "admin_password", "123456")
    monkeypatch.setattr(diagnostics.settings, "llm_api_key", "")
    monkeypatch.setattr(diagnostics.settings, "llm_cost_limit_cny", 1.0)
    warnings = diagnostics.collect_startup_warnings()
    assert any("弱默认口令" in w for w in warnings)
    assert any("未配置 LLM_API_KEY" in w for w in warnings)


def test_collect_startup_warnings_clean_when_configured(monkeypatch):
    """强口令 + 已配置 LLM + 正常阈值 → 无告警。"""
    from app import diagnostics

    monkeypatch.setattr(diagnostics.settings, "admin_password", "Str0ngPassw0rd")
    monkeypatch.setattr(diagnostics.settings, "llm_api_key", "sk-x")
    monkeypatch.setattr(diagnostics.settings, "llm_cost_limit_cny", 1.0)
    assert diagnostics.collect_startup_warnings() == []
