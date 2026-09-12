"""P1-② 合成答案分流的 API 层 + 纯逻辑测试。

覆盖：
- SYNTH_MODE=off（默认）命中直出，现网行为不变；all/multi_doc 触发合成；
  合成失败/拒答降级直出；metrics 计数
- 剥标 _strip_citations 与收尾 finalize_stream_synthesis 的纯逻辑规则
- retrieve 对无 search_dual 的 fake 检索器退化安全
- 流式合成的 delta/done 与 failed 干净降级
不依赖 Ollama/Chroma/真实 LLM（检索与 LLM 全 mock）。
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.metrics import get_metrics
from app.services import rag_service

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client) -> str:
    username = f"syn_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk(cid, doc_id, sim):
    return {
        "chunk_id": cid,
        "doc_id": doc_id,
        "text": f"{doc_id} 的原文内容",
        "rrf_score": 0.02,
        "vector_sim": sim,
    }


def _dual(sources_n: int):
    """构造双视图：breadth 有 sources_n 篇不同文档（首篇 vsim 0.9 过阈值）；depth 为 top1 文档 4 块。"""
    breadth = [_mk(f"c{i}", f"D-{i:03d}", 0.9 - i * 0.01) for i in range(sources_n)]
    breadth += [_mk(f"c{sources_n}", "X-999", 0.30)]  # 低相似噪声，会被阈值过滤
    depth = [_mk(f"c{i}", "D-000", 0.9 - i * 0.01) for i in range(4)]
    return breadth, depth


def _patch_llm_ok(monkeypatch, text="整合后的答案【来源:D-000】"):
    """让合成路径返回固定文本（不触真实 LLM）。"""
    async def _fake_synth(question, depth):
        cleaned, cited = rag_service._finalize_text(text)
        return (cleaned, cited) if cleaned else None

    monkeypatch.setattr(rag_service, "_llm_synthesize", _fake_synth)


# ---------------------------------------------------------------------------
# 非流式 ask 的分流
# ---------------------------------------------------------------------------


async def test_off_命中直出_不调合成(client, monkeypatch):
    """SYNTH_MODE=off：命中直出原文，合成函数不应被调用（默认档位已切 multi_doc，显式声明 off）。"""
    breadth, depth = _dual(3)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "off")

    async def _boom(*a, **k):
        raise AssertionError("off 模式不应触发合成")

    monkeypatch.setattr(rag_service, "_llm_synthesize", _boom)

    token = await _register(client)
    resp = await client.post(
        "/api/rag/ask", json={"question": "担保交易怎么运作"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "hit"
    assert body["answer"] == "D-000 的原文内容"


def _ok(breadth, depth):
    async def _retrieve(*a, **k):
        if k.get("with_depth"):
            return breadth, depth
        return breadth

    return _retrieve


async def test_all_命中走合成_来源含正文引用(client, monkeypatch):
    """SYNTH_MODE=all：mode=synth，答案剥标，正文引用的 doc 并入来源。"""
    breadth, depth = _dual(2)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "all")
    _patch_llm_ok(monkeypatch)

    token = await _register(client)
    resp = await client.post(
        "/api/rag/ask", json={"question": "验机流程是什么"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "synth"
    assert "【来源" not in body["answer"]  # 剥标后正文干净
    doc_ids = [s["doc_id"] for s in body["sources"]]
    assert "D-000" in doc_ids  # 广度来源 + 正文引用合并


async def test_multi_doc_单文档直出(client, monkeypatch):
    """multi_doc：sources 只有 1 篇（单文档信号）→ 直出。"""
    breadth, depth = _dual(1)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "multi_doc")

    async def _boom(*a, **k):
        raise AssertionError("单文档问题不应触发合成")

    monkeypatch.setattr(rag_service, "_llm_synthesize", _boom)

    token = await _register(client)
    resp = await client.post(
        "/api/rag/ask", json={"question": "单文档问题"}, headers=_auth(token)
    )
    assert resp.json()["mode"] == "hit"


async def test_multi_doc_多文档合成(client, monkeypatch):
    """multi_doc：sources ≥ 2 篇 → 合成。"""
    breadth, depth = _dual(2)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "multi_doc")
    _patch_llm_ok(monkeypatch)

    token = await _register(client)
    resp = await client.post(
        "/api/rag/ask", json={"question": "对比类问题"}, headers=_auth(token)
    )
    assert resp.json()["mode"] == "synth"


async def test_合成失败降级直出(client, monkeypatch):
    """LLM 失败（返回 None）→ mode=hit 直出原文，不报错。"""
    breadth, depth = _dual(3)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "all")

    async def _fail(*a, **k):
        return None

    monkeypatch.setattr(rag_service, "_llm_synthesize", _fail)

    token = await _register(client)
    resp = await client.post(
        "/api/rag/ask", json={"question": "任意"}, headers=_auth(token)
    )
    body = resp.json()
    assert body["mode"] == "hit"
    assert body["answer"] == "D-000 的原文内容"


async def test_合成指标计数(client, monkeypatch):
    """触发合成成功后 metrics 记录 trigger 与 ok。"""
    get_metrics().reset()
    breadth, depth = _dual(2)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "all")
    _patch_llm_ok(monkeypatch)

    token = await _register(client)
    await client.post("/api/rag/ask", json={"question": "q"}, headers=_auth(token))

    snap = get_metrics().snapshot()["synth"]
    assert snap.get("trigger") == 1
    assert snap.get("ok") == 1


# ---------------------------------------------------------------------------
# 纯逻辑：剥标与收尾
# ---------------------------------------------------------------------------


def test_剥标_去重保序并清理空行():
    text = "要点一【来源:A-001】。\n要点二【来源:B-002】【来源:A-001】。\n\n\n\n尾行"
    cleaned, cited = rag_service._strip_citations(text)
    assert "【来源" not in cleaned
    assert cited == ["A-001", "B-002"]
    assert "\n\n\n" not in cleaned
    assert cleaned.endswith("尾行")


def test_剥标_兼容无来源前缀和全角冒号():
    _, cited = rag_service._strip_citations("x【D-010】y【来源：FAQ-049】z")
    assert cited == ["D-010", "FAQ-049"]


def test_收尾_拒答返回None_正常返回合并来源():
    hit_sources = [
        rag_service.SourceDoc(doc_id="D-000", title="t", text="x", score=0.9)
    ]
    depth = [_mk("c0", "D-000", 0.9), _mk("c5", "C-005", 0.7)]

    assert rag_service.finalize_stream_synthesis(
        "知识库中未找到相关信息", hit_sources, depth
    ) is None
    assert rag_service.finalize_stream_synthesis("   ", hit_sources, depth) is None

    out = rag_service.finalize_stream_synthesis(
        "答案正文【来源:C-005】", hit_sources, depth
    )
    assert out is not None
    cleaned, merged = out
    assert cleaned == "答案正文"
    assert [s.doc_id for s in merged] == ["D-000", "C-005"]  # 引用 C-005 从 depth 补齐


# ---------------------------------------------------------------------------
# retrieve 对 fake 检索器的退化
# ---------------------------------------------------------------------------


async def test_retrieve_无search_dual时退化为同一份():
    class _Fake:
        is_ready = True
        corpus_version = ""

        def search(self, question, top_k=5):
            return [_mk("c0", "D-000", 0.9)]

    rtv = _Fake()
    breadth, depth = await rag_service.retrieve(
        "q", top_k=5, retriever=rtv, with_depth=True
    )
    assert breadth == depth


# ---------------------------------------------------------------------------
# 流式合成
# ---------------------------------------------------------------------------


async def test_stream_合成成功_done带剥标全文(client, monkeypatch):
    breadth, depth = _dual(2)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "all")

    async def _stream(question, depth):
        yield ("delta", "部分【来源:D-000】")
        yield ("delta", "答案")

    monkeypatch.setattr(rag_service, "_llm_synthesize_stream", _stream)

    token = await _register(client)
    sid = secrets.token_hex(16)
    body = ""
    async with client.stream(
        "POST",
        "/api/rag/ask/stream",
        json={"question": "流式问题", "session_id": sid},
        headers=_auth(token),
    ) as resp:
        async for chunk in resp.aiter_text():
            body += chunk

    assert '"mode": "synth"' in body
    assert "部分答案" in body  # done.answer 为剥标后全文


async def test_stream_合成failed_干净降级直出(client, monkeypatch):
    """failed 发生在任何 delta 之前 → 直接直出原文，无合成内容。"""
    breadth, depth = _dual(1)
    monkeypatch.setattr(rag_service, "retrieve", _ok(breadth, depth))
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "all")

    async def _stream(question, depth):
        yield ("failed", "成本熔断")
        yield ("delta", "不应出现")

    monkeypatch.setattr(rag_service, "_llm_synthesize_stream", _stream)

    token = await _register(client)
    sid = secrets.token_hex(16)
    body = ""
    async with client.stream(
        "POST",
        "/api/rag/ask/stream",
        json={"question": "流式问题", "session_id": sid},
        headers=_auth(token),
    ) as resp:
        async for chunk in resp.aiter_text():
            body += chunk

    assert '"mode": "hit"' in body
    assert "不应出现" not in body
    assert "成本熔断" not in body


# ---------------------------------------------------------------------------
# 缓存双视图（纯 L1 往返，不连 Redis）
# ---------------------------------------------------------------------------


async def test_cache_pair_L1往返():
    from app.rag.cache import SearchCache

    cache = SearchCache("redis://localhost:6399/0", ttl=60, enabled=True)
    b = [_mk("c0", "D-000", 0.9)]
    d = [_mk("c1", "D-000", 0.85)]
    await cache.set_pair("问题", b, d, top_k=5, version="v")
    got = await cache.get_pair("问题", top_k=5, version="v")
    assert got is not None
    assert got[0] == b and got[1] == d
    # 拷贝语义：改返回值不影响缓存
    got[0][0]["doc_id"] = "HACK"
    again = await cache.get_pair("问题", top_k=5, version="v")
    assert again[0][0]["doc_id"] == "D-000"
