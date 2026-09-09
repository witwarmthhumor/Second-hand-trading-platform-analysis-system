"""RAG 检索结果缓存（Redis）。

用 Redis 缓存检索结果（Top-K + 来源），避免热点问题重复做 embedding + 向量检索。

设计要点：
- key 用「问题归一化」后的字符串，避免「忘记密码」与「忘记密码！」算两个 key
- 缓存 value 存检索结果 JSON（doc_id/text/rrf_score/vector_sim 列表）
- 命中时返回缓存结果；未命中走原检索流程后写缓存（带 TTL）
- 知识库重建时按前缀批量清空缓存
- Redis 不可用时优雅降级：缓存读写静默跳过，不影响问答主流程
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# 缓存 key 前缀（便于重建时按前缀清空）
_CACHE_PREFIX = "rag:search:"


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


class SearchCache:
    """Redis 检索结果缓存（异步，惰性连接，失败降级）。"""

    def __init__(self, redis_url: str, ttl: int) -> None:
        self._redis_url = redis_url
        self._ttl = ttl
        self._client: aioredis.Redis | None = None

    async def _get_client(self) -> aioredis.Redis | None:
        """惰性创建 Redis 客户端；创建失败返回 None（降级）。"""
        if self._client is None:
            try:
                self._client = aioredis.from_url(
                    self._redis_url, decode_responses=True, socket_connect_timeout=2
                )
                await self._client.ping()
            except Exception as exc:  # noqa: BLE001 — 缓存不可用不影响主流程
                logger.warning("Redis 缓存不可用，检索缓存降级跳过：%s", exc)
                self._client = None
        return self._client

    async def get(self, question: str) -> list[dict] | None:
        """命中返回缓存结果，未命中返回 None。"""
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(self._key(question))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读检索缓存失败：%s", exc)
            return None

    async def set(self, question: str, results: list[dict]) -> None:
        """写入检索结果，带 TTL。"""
        client = await self._get_client()
        if client is None:
            return
        try:
            await client.set(self._key(question), json.dumps(results, ensure_ascii=False), ex=self._ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写检索缓存失败：%s", exc)

    async def clear(self) -> int:
        """按前缀清空所有检索缓存（知识库重建时调用），返回删除 key 数。"""
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
            logger.warning("清空检索缓存失败：%s", exc)
            return 0

    def _key(self, question: str) -> str:
        return f"{_CACHE_PREFIX}{normalize_question(question)}"


# 全局单例
_cache: SearchCache | None = None


def get_search_cache() -> SearchCache:
    """惰性单例。"""
    global _cache
    if _cache is None:
        _cache = SearchCache(settings.redis_url, settings.rag_cache_ttl)
    return _cache
