"""RAG 检索缓存（Redis L2 + 进程内 L1）单元测试。

用内存 fake Redis 模拟，不依赖真实 Redis 服务：
- normalize_question 归一化
- key 维度：含 top_k 与语料版本（不同 top_k 不串用）
- get/set 命中与未命中、TTL 写入带抖动
- L1（进程内）优先命中、clear 连带清空 L1
- Redis 不可用时降级 + 熔断（不反复建连）
- reset 关闭客户端；purge_all 独立连接清空
- SingleFlight 并发合并
"""

import asyncio

import pytest

from app.rag import cache as cache_mod
from app.rag.cache import SearchCache, SingleFlight, normalize_question, purge_all

# ---------------- 归一化 ----------------


def test_normalize_question_strips_whitespace_and_fullwidth():
    """全角转半角、去空白、小写。"""
    assert normalize_question("  忘记密码  ") == "忘记密码"
    assert normalize_question("忘记密码！") == "忘记密码!"
    assert normalize_question("ＡＢＣ１２３") == "abc123"
    assert normalize_question("闲鱼  抽成\n多少") == "闲鱼抽成多少"


def test_normalize_question_equivalent_forms_same_key():
    """等价形式归一化后 key 相同。"""
    assert normalize_question("忘记密码") == normalize_question("忘记密码 ")
    assert normalize_question("忘记密码！") == normalize_question("忘记密码！")


# ---------------- fake Redis ----------------


class _FakeRedis:
    """内存 fake Redis，实现 get/set/delete/scan_iter/ping/aclose，并记录写入 TTL。"""

    def __init__(self):
        self._store = {}
        self.ttls: list[int | None] = []
        self.get_calls = 0

    async def ping(self):
        return True

    async def aclose(self):
        return None

    async def get(self, key):
        self.get_calls += 1
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        self.ttls.append(ex)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                n += 1
        return n

    async def scan_iter(self, match=None):
        prefix = match.replace("*", "") if match else ""
        for k in list(self._store.keys()):
            if k.startswith(prefix):
                yield k


class _FailingRedis:
    """所有命令都抛错，模拟 Redis 连接不可用。"""

    async def ping(self):
        raise ConnectionError("no redis")

    async def aclose(self):
        return None

    async def get(self, key):
        raise ConnectionError("no redis")

    async def set(self, key, value, ex=None):
        raise ConnectionError("no redis")

    async def scan_iter(self, match=None):
        raise ConnectionError("no redis")
        yield  # pragma: no cover - 仅为使其成为生成器


def _make_cache(fake=None, ttl: int = 3600) -> SearchCache:
    c = SearchCache("redis://fake", ttl=ttl)
    c._client = fake
    return c


@pytest.mark.asyncio
async def test_get_miss_returns_none():
    cache = _make_cache(_FakeRedis())
    assert await cache.get("忘记密码") is None


@pytest.mark.asyncio
async def test_set_then_get_returns_results():
    cache = _make_cache(_FakeRedis())
    results = [{"doc_id": "A-001", "text": "原文", "vector_sim": 0.7}]
    await cache.set("忘记密码", results)
    got = await cache.get("忘记密码")
    assert got == results


@pytest.mark.asyncio
async def test_get_hits_normalized_key():
    """等价问题命中同一缓存 key。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("忘记密码", [{"doc_id": "A-001"}])
    assert await cache.get("忘记密码 ") == [{"doc_id": "A-001"}]


@pytest.mark.asyncio
async def test_top_k_is_part_of_key():
    """top_k 不同的请求不得串用彼此的结果（曾经共用同一 key）。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("担保交易", [{"doc_id": f"D-{i}"} for i in range(5)], top_k=5)

    # top_k=2 的请求不应拿到 top_k=5 的结果
    assert await cache.get("担保交易", top_k=2) is None
    assert len(await cache.get("担保交易", top_k=5)) == 5


@pytest.mark.asyncio
async def test_corpus_version_is_part_of_key():
    """语料版本变化后旧缓存不再命中。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("担保交易", [{"doc_id": "D-1"}], version="aaaa1111")
    assert await cache.get("担保交易", version="aaaa1111") == [{"doc_id": "D-1"}]
    assert await cache.get("担保交易", version="bbbb2222") is None


@pytest.mark.asyncio
async def test_l1_hit_serves_without_redis():
    """L1（进程内）命中时不再访问 Redis；Redis 侧数据被清掉也照样命中。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("忘记密码", [{"doc_id": "A-001"}])

    fake._store.clear()  # 模拟 Redis 数据丢失/过期
    before = fake.get_calls
    got = await cache.get("忘记密码")
    assert got == [{"doc_id": "A-001"}]
    assert fake.get_calls == before  # 未访问 Redis


@pytest.mark.asyncio
async def test_l1_hit_returns_isolated_copy():
    """L1 命中必须浅拷贝：调用方原地改动结果不得污染后续命中。"""
    cache = _make_cache(_FakeRedis())
    await cache.set("问题", [{"doc_id": "A-001", "text": "x"}])

    first = await cache.get("问题")
    first[0]["text"] = "被改动"
    first.append({"doc_id": "HACK"})

    assert await cache.get("问题") == [{"doc_id": "A-001", "text": "x"}]


@pytest.mark.asyncio
async def test_clear_also_clears_l1():
    """重建清缓存时必须连 L1 一起清，否则进程内旧语料结果仍会命中。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("问题一", [{"a": 1}])
    await cache.set("问题二", [{"b": 2}])
    fake._store["other:key"] = "unrelated"

    n = await cache.clear()
    assert n == 2
    assert await cache.get("问题一") is None
    assert await cache.get("问题二") is None
    assert "other:key" in fake._store  # 非缓存 key 不受影响


@pytest.mark.asyncio
async def test_ttl_written_with_jitter():
    """TTL 带 ±10% 抖动，避免同批 key 同时过期造成回源尖峰。"""
    fake = _FakeRedis()
    cache = _make_cache(fake, ttl=1000)
    for i in range(20):
        await cache.set(f"抖动{i}", [{"doc_id": "A-001"}])

    assert all(t is not None for t in fake.ttls)
    assert all(900 <= t <= 1100 for t in fake.ttls)
    assert len(set(fake.ttls)) > 1  # 确实抖动，而非固定值


@pytest.mark.asyncio
async def test_redis_unavailable_degrades_and_circuits(monkeypatch):
    """Redis 不可用：降级返回 None，且进入熔断不再反复建连（曾每次白等 2s）。"""
    calls = {"n": 0}

    def _bad_from_url(*args, **kwargs):
        calls["n"] += 1
        return _FailingRedis()

    monkeypatch.setattr(cache_mod.aioredis, "from_url", _bad_from_url)
    cache = SearchCache("redis://unreachable", ttl=3600)

    assert await cache.get("忘记密码") is None
    assert calls["n"] == 1

    # 熔断窗口内：不再尝试建连，也不再等连接超时
    assert await cache.get("忘记密码") is None
    await cache.set("忘记密码", [{"a": 1}])
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_reset_closes_client_and_allows_reconnect():
    """reset 丢掉旧客户端（跨事件循环场景收尾），之后可重新建连。"""
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.get("问题")
    assert cache._client is fake
    await cache.reset()
    assert cache._client is None


@pytest.mark.asyncio
async def test_disabled_cache_is_noop():
    """总开关关闭时：不建连、get 返回 None、set 不报错。"""
    cache = SearchCache("redis://unreachable", ttl=3600, enabled=False)
    assert await cache.get("问题") is None
    await cache.set("问题", [{"a": 1}])
    assert await cache.clear() == 0


@pytest.mark.asyncio
async def test_purge_all_uses_one_off_connection(monkeypatch):
    """purge_all 用一次性连接清空前缀（供独立事件循环调用，不碰单例 client）。"""
    fake = _FakeRedis()
    fake._store["rag:search:v2:k5:问题"] = "x"
    fake._store["rag:search:v2:k5:问题2"] = "y"
    fake._store["unrelated"] = "z"

    monkeypatch.setattr(cache_mod.aioredis, "from_url", lambda *a, **k: fake)
    assert await purge_all("redis://fake") == 2
    assert "unrelated" in fake._store


@pytest.mark.asyncio
async def test_purge_all_swallows_connection_error(monkeypatch):
    """Redis 不可达时 purge_all 返回 0 且不抛异常（重建流程不能因此失败）。"""
    monkeypatch.setattr(cache_mod.aioredis, "from_url", lambda *a, **k: _FailingRedis())
    assert await purge_all("redis://unreachable", timeout=0.2) == 0


# ---------------- singleflight ----------------


@pytest.mark.asyncio
async def test_singleflight_merges_concurrent_calls():
    """同 key 的并发调用只回源一次，其余复用同一结果（防击穿）。"""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return "结果"

    sf = SingleFlight()
    results = await asyncio.gather(*(sf.run("same-key", factory) for _ in range(5)))
    assert calls["n"] == 1
    assert results == ["结果"] * 5
    assert sf.inflight_count == 0


@pytest.mark.asyncio
async def test_singleflight_different_keys_run_separately():
    """不同 key 各自回源，不互相合并。"""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    sf = SingleFlight()
    await asyncio.gather(sf.run("k1", factory), sf.run("k2", factory))
    assert calls["n"] == 2
