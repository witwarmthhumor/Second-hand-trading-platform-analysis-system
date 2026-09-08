"""bge-m3 embedding：直调 Ollama ``/api/embed``，批量 + 落盘缓存。

迁移自 RAGreflow-second-hand 的 ``_embed``/``embed_batched``/``corpus_embeddings``，
改用 httpx 同步客户端（A 项目已依赖 httpx），路径走 ``rag.config``。
"""

from __future__ import annotations

import hashlib

import httpx
import numpy as np

from app.rag.config import EMBED_DIM, EMBED_MODEL, OLLAMA_BASE_URL, PROJECT_ROOT

# embedding 缓存目录（key 含模型+全文 hash，语料不变时免重付全量 embedding）
EMB_CACHE_DIR = PROJECT_ROOT / "rag_data" / "emb_cache"
BATCH = 32


def embed(texts: list[str]) -> list[list[float]]:
    """单批 embedding（batch ≤ 32，过大触发 Ollama 超时）。"""
    resp = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()
    if "embeddings" not in data:
        raise RuntimeError(f"embedding 失败: {data}")
    return [list(e) for e in data["embeddings"]]


def embed_batched(texts: list[str], batch: int = BATCH) -> list[list[float]]:
    """分批 embedding，返回与输入等长的向量列表。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        out.extend(embed(texts[i : i + batch]))
    return out


def corpus_embeddings(chunk_size: int, texts: list[str]) -> list[list[float]]:
    """语料 embedding 落盘缓存：key = 模型+chunk_size+全文 hash。

    语料不变、仅调检索参数时命中缓存，免重付全量 embedding（约 40 分钟）。
    """
    EMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    h.update(f"{EMBED_MODEL}|{chunk_size}|{len(texts)}".encode())
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\x00")
    cache_file = EMB_CACHE_DIR / f"emb_{chunk_size}_{h.hexdigest()[:16]}.npy"
    if cache_file.exists():
        return np.load(cache_file).tolist()
    embs = embed_batched(texts)
    np.save(cache_file, np.asarray(embs, dtype=np.float32))
    return embs


def query_embedding(text: str) -> list[float]:
    """单条查询 embedding（问答在线路径）。"""
    return embed([text])[0]


def dim() -> int:
    """返回 embedding 维度（供 Chroma 校验）。"""
    return EMBED_DIM
