"""RAG 检索结果缓存（Redis L2 + 进程内 L1）。

用 Redis 缓存检索结果（Top-K + 来源），避免热点问题重复做 embedding + 向量检索。

设计要点：
- key = 「语料版本 + top_k + 问题归一化」，避免：①「忘记密码」与「忘记密码！」算两个
  key；② 不同 top_k 的请求互相串用结果；③ 语料被替换后仍命中旧结果
- 缓存 value 存检索结果 JSON（doc_id/text/rrf_score/vector_sim 列表）
- 命中时返回缓存结果；未命中走原检索流程后写缓存（TTL 带 ±10% 随机抖动）
- L1（进程内 LRU，容量 256）作为 Redis 故障时的二级兜底，先查 L1 再查 Redis
- Redis 不可用时优雅降级：缓存读写静默跳过，不影响问答主流程；
  且失败后进入 30s 熔断窗口，窗口内不再尝试建连（否则每次操作白等 2s 连接超时）
- 只读/写会「重置缓存」的场景（知识库重建）用独立连接清空，不复用进程内单例 client
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# 缓存 key 前缀（便于重建时按前缀清空）
_CACHE_PREFIX = "rag:search:"
# key 结构版本：结构调整时递增，天然淘汰旧格式 key
_CACHE_VERSION = "v2"

# Redis 故障熔断窗口（秒）：一次失败后窗口内不再尝试建连
_CIRCUIT_COOLDOWN = 30.0

# 连接池：显式上限避免耗尽 Redis maxclients；socket_timeout 避免 Redis 卡住时挂死请求
_POOL_MAX_CONNECTIONS = 50
_SOCKET_TIMEOUT = 0.5
_HEALTH_CHECK_INTERVAL = 30

# L1（进程内）配置
_L1_MAX_ENTRIES = 256
_L1_TTL_CAP = 60.0  # L1 最长驻留秒数，避免进程内长期读到旧语料结果

T = TypeVar("T")


def normalize_question(question: str) -> str:
    """问题归一化：全角转半角、去空白、小写，作为缓存 key 的一部分。"""
    text = question.strip()
    # 全角→半角：全角字母数字（0xFF01-0xFF5E 区）统一减 0xFEE0；
    # 全角空格（U+3000）单独映射为半角空格
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    text = "".join(chars)
    text = re.sub(r"\s+", "", text)
    return text.lower()


class SingleFlight:
    """同 key 并发合并：只放一个请求回源，其余 await 同一个结果。

    用于防「热点 key 刚过期时 N 个并发请求同时打下游」的缓存击穿。
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        """执行 factory；同 key 的并发调用共享同一次执行结果。"""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        # shield：调用方被取消不影响正在回源的任务，结果仍会写入缓存供后续复用
        return await asyncio.shield(task)

    @property
    def inflight_count(self) -> int:
        """当前在途请求数（供测试/观测）。"""
        return len(self._inflight)


class SearchCache:
    """检索结果缓存：L1(进程内 LRU) → L2(Redis)，失败降级 + 熔断。"""

    def __init__(self, redis_url: str, ttl: int, *, enabled: bool = True) -> None:
        self._redis_url = redis_url
        self._ttl = ttl
        self._enabled = enabled
        self._client: aioredis.Redis | None = None
        # 熔断截止时间（time.monotonic），早于该时刻一律直接降级
        self._down_until: float = 0.0
        self._l1: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    # ---- 连接管理 ----

    async def _get_client(self) -> aioredis.Redis | None:
        """惰性创建 Redis 客户端；失败或熔断中返回 None（降级）。"""
        if not self._enabled:
            return None
        if time.monotonic() < self._down_until:
            # 熔断窗口内直接降级：不再建连、不再等 socket_connect_timeout
            return None
        if self._client is None:
            client: aioredis.Redis | None = None
            try:
                client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=_SOCKET_TIMEOUT,
                    max_connections=_POOL_MAX_CONNECTIONS,
                    health_check_interval=_HEALTH_CHECK_INTERVAL,
                )
                await client.ping()
            except Exception as exc:  # noqa: BLE001 — 缓存不可用不影响主流程
                logger.warning(
                    "Redis 缓存不可用，进入 %.0fs 熔断降级：%s", _CIRCUIT_COOLDOWN, exc
                )
                self._down_until = time.monotonic() + _CIRCUIT_COOLDOWN
                with contextlib.suppress(Exception):
                    if client is not None:
                        await client.aclose()
                return None
            self._client = client
        return self._client

    def _trip(self, exc: Exception) -> None:
        """读写异常也进入熔断，避免坏连接反复拖慢请求。"""
        self._down_until = time.monotonic() + _CIRCUIT_COOLDOWN
        logger.warning("检索缓存访问失败，进入 %.0fs 熔断：%s", _CIRCUIT_COOLDOWN, exc)

    async def reset(self) -> None:
        """关闭并丢弃当前客户端（供跨事件循环场景收尾，避免复用到已关闭的 loop）。"""
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    # ---- key ----

    def key_for(self, question: str, top_k: int = 5, version: str = "") -> str:
        """构造缓存 key：版本 + top_k + 归一化问题。"""
        ver = f"{version}:" if version else ""
        return f"{_CACHE_PREFIX}{_CACHE_VERSION}:{ver}k{top_k}:{normalize_question(question)}"

    # 向后兼容别名（旧调用点/测试使用的私有名）
    _key = key_for

    # ---- L1（进程内） ----

    def _l1_get(self, key: str) -> list[dict] | None:
        row = self._l1.get(key)
        if row is None:
            return None
        expire_at, value = row
        if expire_at < time.monotonic():
            self._l1.pop(key, None)
            return None
        self._l1.move_to_end(key)
        return value

    def _l1_set(self, key: str, value: list[dict]) -> None:
        ttl = min(float(self._ttl), _L1_TTL_CAP)
        self._l1[key] = (time.monotonic() + ttl, value)
        self._l1.move_to_end(key)
        while len(self._l1) > _L1_MAX_ENTRIES:
            self._l1.popitem(last=False)

    # ---- 对外读写 ----

    async def get(
        self, question: str, top_k: int = 5, version: str = ""
    ) -> list[dict] | None:
        """命中返回缓存结果，未命中返回 None。先查 L1，再查 Redis。"""
        if not self._enabled:
            return None
        key = self.key_for(question, top_k, version)
        hit = self._l1_get(key)
        if hit is not None:
            # L1 命中返回的是同一份对象，必须浅拷贝再给调用方：
            # 否则任一次请求原地修改结果会污染后续所有命中（旧实现每次走 Redis
            # 反序列化，天然是全新对象，不存在该问题）
            return [dict(r) for r in hit]

        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:  # noqa: BLE001
            self._trip(exc)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("读检索缓存失败（反序列化）：%s", exc)
            return None
        self._l1_set(key, value)
        return [dict(r) for r in value]

    async def set(
        self,
        question: str,
        results: list[dict],
        top_k: int = 5,
        version: str = "",
    ) -> None:
        """写入检索结果：L1 立即可用，Redis 带 TTL（±10% 抖动，防同批 key 同时过期）。"""
        if not self._enabled:
            return
        key = self.key_for(question, top_k, version)
        self._l1_set(key, results)

        client = await self._get_client()
        if client is None:
            return
        # TTL 抖动：避免同批写入的 key 在同一秒集体过期，造成回源流量尖峰
        ttl = max(1, int(self._ttl * random.uniform(0.9, 1.1)))
        try:
            await client.set(key, json.dumps(results, ensure_ascii=False), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            self._trip(exc)

    def clear_l1(self) -> int:
        """清空进程内 L1（重建后调用；Redis L2 由同进程的 `clear()` 或 `purge_all()` 负责）。

        纯内存操作，不碰事件循环，可在后台线程里安全调用。
        """
        n = len(self._l1)
        self._l1.clear()
        return n

    async def clear(self) -> int:
        """按前缀清空所有检索缓存（含 L1），返回删除 key 数。"""
        self._l1.clear()
        client = await self._get_client()
        if client is None:
            return 0
        try:
            keys: list[Any] = []
            async for k in client.scan_iter(match=f"{_CACHE_PREFIX}*"):
                keys.append(k)
            if not keys:
                return 0
            return int(await client.delete(*keys))
        except Exception as exc:  # noqa: BLE001
            self._trip(exc)
            return 0


async def purge_all(redis_url: str, *, timeout: float = 2.0) -> int:
    """用一次性连接按前缀清空检索缓存，返回删除 key 数。

    专供「独立事件循环」场景（如后台线程里 `asyncio.run`）：不复用进程内单例
    client，避免它被绑到随后关闭的临时循环上，导致后续请求全部
    `Event loop is closed` 而缓存永久失效。
    """
    client: aioredis.Redis | None = None
    try:
        client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        keys: list[Any] = [k async for k in client.scan_iter(match=f"{_CACHE_PREFIX}*")]
        if not keys:
            return 0
        return int(await client.delete(*keys))
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空检索缓存失败：%s", exc)
        return 0
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def corpus_fingerprint(paths: tuple[Any, ...]) -> str:
    """由语料文件的 mtime 生成短指纹，作为缓存 key 的「语料版本」。

    语料被整体替换（mtime 变化）时指纹变化，旧缓存自然不再命中，
    用于兜底「没走重建接口、直接替换 vector_store 目录」的场景。
    """
    parts: list[str] = []
    for p in paths:
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{p.name}:-")
    raw = "|".join(parts).encode()
    return hashlib.sha256(raw).hexdigest()[:8]


# 全局单例
_cache: SearchCache | None = None


def get_search_cache() -> SearchCache:
    """惰性单例。"""
    global _cache
    if _cache is None:
        _cache = SearchCache(
            settings.redis_url,
            settings.rag_cache_ttl,
            enabled=settings.rag_cache_enabled,
        )
    return _cache
