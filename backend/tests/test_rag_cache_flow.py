"""检索缓存接入链路的测试：rag_service.retrieve / 流式端点 / 重建清缓存。

重点回归三处曾经的缺陷：
- 流式端点绕过缓存（前端默认走这条路 → 缓存收益为 0）
- key 不含 top_k（不同 top_k 结果互相串用）
- 重建时用进程内单例 client 清缓存（把连接绑到临时事件循环，缓存永久失效）
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from test_rag_cache import _FakeRedis

from app.main import app
from app.rag import cache as cache_mod
from app.rag.cache import SearchCache, corpus_fingerprint
from app.services import rag_service

# 注：pyproject 已设 asyncio_mode=auto，async 用例自动识别。
# 这里不设模块级 asyncio 标记，否则同步用例（语料指纹）会触发
# 「marked with '@pytest.mark.asyncio' but it is not an async function」告警。


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client) -> str:
    username = f"cache_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _CountingRetriever:
    """记录 search 调用次数的 mock 检索器。"""

    is_ready = True

    def __init__(self, results=None):
        self.calls: list[tuple[str, int]] = []
        self._results = results or [
            {"doc_id": "A-001", "text": "命中原文", "vector_sim": 0.78}
        ]

    def search(self, question, top_k=5):
        self.calls.append((question, top_k))
        # 结果条数与 top_k 挂钩，便于验证 top_k 不同不会串用
        return self._results[:top_k]


def _install_fake_cache(monkeypatch, ttl: int = 3600) -> SearchCache:
    """把进程内单例换成 fake Redis 支撑的缓存（测试环境默认关闭缓存）。"""
    fake = _FakeRedis()
    cache = SearchCache("redis://fake", ttl=ttl)
    cache._client = fake
    monkeypatch.setattr(cache_mod, "_cache", cache)
    return cache


# ---------------- 语料指纹 ----------------


def test_corpus_fingerprint_changes_with_mtime(tmp_path):
    """语料文件 mtime 变化 → 指纹变化（语料被替换后旧缓存自动失效）。"""
    f = tmp_path / "bm25_index.json"
    f.write_text("a", encoding="utf-8")
    first = corpus_fingerprint((f,))

    import os

    os.utime(f, (1_700_000_000, 1_700_000_000))
    assert corpus_fingerprint((f,)) != first


def test_corpus_fingerprint_missing_file_is_stable(tmp_path):
    """文件缺失不抛异常，且指纹稳定。"""
    missing = tmp_path / "nope.json"
    assert corpus_fingerprint((missing,)) == corpus_fingerprint((missing,))


# ---------------- rag_service.retrieve ----------------


async def test_retrieve_serves_second_call_from_cache(monkeypatch):
    """相同问题第二次检索命中缓存，不再调用 retriever.search。"""
    _install_fake_cache(monkeypatch)
    rtv = _CountingRetriever()

    first = await rag_service.retrieve("担保交易怎么运作", top_k=5, retriever=rtv)
    second = await rag_service.retrieve("担保交易怎么运作", top_k=5, retriever=rtv)

    assert len(rtv.calls) == 1
    assert first == second


async def test_retrieve_distinguishes_top_k(monkeypatch):
    """top_k 不同视为不同缓存条目，各自回源（修复结果串用）。"""
    _install_fake_cache(monkeypatch)
    rtv = _CountingRetriever()

    await rag_service.retrieve("担保交易怎么运作", top_k=5, retriever=rtv)
    got = await rag_service.retrieve("担保交易怎么运作", top_k=1, retriever=rtv)

    assert len(rtv.calls) == 2
    assert len(got) == 1


async def test_retrieve_normalizes_equivalent_question(monkeypatch):
    """等价问法（多余空格/全角）命中同一缓存条目。"""
    _install_fake_cache(monkeypatch)
    rtv = _CountingRetriever()

    await rag_service.retrieve("担保交易怎么运作", top_k=5, retriever=rtv)
    await rag_service.retrieve("　担保交易怎么运作 ", top_k=5, retriever=rtv)

    assert len(rtv.calls) == 1


async def test_retrieve_singleflight_merges_concurrent_misses(monkeypatch):
    """同一问题的并发 miss 只回源一次（防击穿）。"""
    import asyncio

    _install_fake_cache(monkeypatch)
    rtv = _CountingRetriever()

    results = await asyncio.gather(
        *(
            rag_service.retrieve("并发同一问题", top_k=5, retriever=rtv)
            for _ in range(6)
        )
    )
    assert len(rtv.calls) == 1
    assert all(r == results[0] for r in results)


# ---------------- 流式端点接入缓存 ----------------


async def test_stream_endpoint_uses_cache_on_repeat(client, monkeypatch):
    """流式端点重复问同一问题：第二次命中缓存，不再触发检索。"""
    from app.api import rag as rag_api

    _install_fake_cache(monkeypatch)
    rtv = _CountingRetriever()
    monkeypatch.setattr(rag_api, "get_retriever", lambda: rtv)

    token = await _register(client)
    for _ in range(2):
        async with client.stream(
            "POST",
            "/api/rag/ask/stream",
            json={"question": "流式缓存问题"},
            headers=_auth(token),
        ) as resp:
            assert resp.status_code == 200
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
        assert '"mode": "hit"' in body

    assert len(rtv.calls) == 1, f"流式端点未命中缓存：{rtv.calls}"


# ---------------- 重建清缓存 ----------------


async def test_rebuild_purges_cache_with_one_off_connection(client, monkeypatch):
    """重建后：刷新检索器单例 + 用一次性连接清缓存（且不触碰单例 client）。"""
    from app.api import kb as kb_api
    from app.services import kb_service

    _install_fake_cache(monkeypatch)
    monkeypatch.setattr(
        kb_service,
        "rebuild_vector_store",
        lambda: {"chunk_count": 7, "seconds": 0.1},
    )

    reloaded = {"n": 0}
    monkeypatch.setattr(
        "app.rag.retriever.reload_retriever",
        lambda: reloaded.__setitem__("n", reloaded["n"] + 1) or True,
    )

    purged: dict = {}

    async def _fake_purge(redis_url, **kwargs):
        purged["url"] = redis_url
        return 3

    monkeypatch.setattr("app.rag.cache.purge_all", _fake_purge)

    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    admin = resp.json()["access_token"]
    resp = await client.post("/api/kb/rebuild", headers=_auth(admin))
    assert resp.status_code == 200 and resp.json()["triggered"] is True

    last = kb_api._rebuild_state["last_result"]
    assert last["ok"] is True
    assert last["retriever_reloaded"] is True
    assert last["cache_cleared"] == 3
    assert "l1_cleared" in last
    assert reloaded["n"] == 1
    assert purged["url"]  # 用的是 settings.redis_url，而不是进程内单例
