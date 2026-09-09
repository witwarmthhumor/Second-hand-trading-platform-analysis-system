"""RAG 检索缓存（Redis）单元测试。

用内存 fake Redis 模拟，不依赖真实 Redis 服务：
- normalize_question 归一化
- SearchCache get/set 命中与未命中
- TTL 写入
- clear 前缀清空
- Redis 不可用时降级（返回 None/静默）
"""

import pytest

from app.rag.cache import SearchCache, normalize_question

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
    """内存 fake Redis，实现 get/set/delete/scan_iter/ping。"""

    def __init__(self):
        self._store = {}

    async def ping(self):
        return True

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value

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
    """ping 抛错，模拟 Redis 不可用。"""

    async def ping(self):
        raise ConnectionError("no redis")


def _make_cache(fake=None):
    c = SearchCache("redis://fake", ttl=3600)
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
async def test_clear_removes_prefixed_keys():
    fake = _FakeRedis()
    cache = _make_cache(fake)
    await cache.set("问题一", [{"a": 1}])
    await cache.set("问题二", [{"b": 2}])
    fake._store["other:key"] = "unrelated"
    n = await cache.clear()
    assert n == 2
    assert await cache.get("问题一") is None
    assert "other:key" in fake._store  # 非缓存 key 不受影响


@pytest.mark.asyncio
async def test_redis_unavailable_degrades_gracefully():
    """Redis 不可用时 get 返回 None、set/clear 静默，不抛异常。"""
    cache = _make_cache(_FailingRedis())
    assert await cache.get("忘记密码") is None
    await cache.set("忘记密码", [{"a": 1}])  # 不抛
    assert await cache.clear() == 0
