"""父子文档检索实验 —— 公共设施。

本模块提供实验三方（标题分块 / 递归分块 / 父子检索）共用的：

- 语料加载：``canonical`` MD + ``faq_source`` JSON → 每个源文件一个 LangChain
  ``Document``，``metadata.doc_id`` 与生产链路一致（源自文件名，如 ``A-001``/``FAQ-001``）。
- 向量化：包装生产 ``app.rag.embedding``（bge-m3 @ 宿主机 Ollama），带落盘缓存。
- 评测：doc 级 ``recall@k`` / ``Hit@k`` / ``MRR@10`` / ``nDCG@10``。

设计要点：三方共用同一语料、同一 golden 测试集、同一稠密向量检索与同一 doc 级
去重口径，唯一变量是「分块/检索策略」，从而可做归因。
"""

from __future__ import annotations

import json
import math
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

# 实验脚本位于 Agent/scripts/experiments/parent_child_retrieval/，
# 项目根为 Agent/（parents[3]），把 backend/ 加入 sys.path 复用生产 RAG 组件。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag.chunking import Chunk, load_seed_rows  # noqa: E402
from app.rag.config import (  # noqa: E402
    CANONICAL_DIR,
    FAQ_SOURCE_DIR,
)
from app.rag.embedding import corpus_embeddings, query_embedding  # noqa: E402

GOLDEN_PATH = PROJECT_ROOT / "rag_data" / "eval" / "golden_queries.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
EXPERIMENT_CHROMA_DIR = PROJECT_ROOT / "rag_data" / "experiments" / "vector_store"

# 评测 k 值集合（doc 级）
KS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# 语料加载：每个源文件一个 Document（三方共用同一「文档」粒度）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _seed_rows() -> dict[str, Any]:
    """种子元数据 id → SeedRow（title/category/series）。"""
    return {r.id: r for r in load_seed_rows()}


def load_corpus_documents() -> list[Document]:
    """加载全量语料为「每源文件一个 Document」。

    - MD：读 ``canonical/**/*.md``，去掉 frontmatter，正文前补一行标题。
    - FAQ：读 ``faq_source/*.json``，把每条 QA 展平为文本。
    metadata 统一含 doc_id / title / category / series / source。
    """
    seeds = _seed_rows()
    docs: list[Document] = []

    for md_path in sorted(CANONICAL_DIR.rglob("*.md")):
        sid = md_path.stem.split("_")[0]
        seed = seeds.get(sid)
        if seed is None:
            continue
        body = _strip_frontmatter(md_path.read_text(encoding="utf-8"))
        text = f"# {seed.title}\n\n{body}".strip()
        docs.append(
            Document(
                page_content=text,
                metadata={
                    "doc_id": seed.id,
                    "title": seed.title,
                    "category": seed.l1_category,
                    "series": seed.series,
                    "source": f"canonical/{md_path.name}",
                },
            )
        )

    for faq_path in sorted(FAQ_SOURCE_DIR.glob("*.json")):
        sid = faq_path.stem
        seed = seeds.get(sid)
        if seed is None:
            continue
        payload = json.loads(faq_path.read_text(encoding="utf-8"))
        docs.append(
            Document(
                page_content=_flatten_faq(seed.title, payload),
                metadata={
                    "doc_id": seed.id,
                    "title": seed.title,
                    "category": seed.l1_category,
                    "series": seed.series,
                    "source": f"faq_source/{faq_path.name}",
                },
            )
        )
    return docs


def _strip_frontmatter(md_text: str) -> str:
    """去掉 YAML frontmatter，返回正文。"""
    m = md_text.lstrip()
    if not m.startswith("---"):
        return m
    end = m.find("\n---", 3)
    return m[end + 4 :].lstrip("\n") if end != -1 else m


def _flatten_faq(title: str, payload: dict) -> str:
    """FAQ JSON → 单篇文本（每条 QA 一段）。"""
    lines = [f"# {title}"]
    for item in payload.get("items", []):
        variants = "；".join(item.get("q_variants", []))
        q = item.get("q_main", "")
        if variants:
            q = f"{q}（{variants}）"
        lines.append(f"问：{q}\n答：{item.get('a', '')}")
    return "\n\n".join(lines)


def chunks_to_meta(chunks: list[Chunk]) -> list[dict]:
    """Chunk 列表 → LangChain metadata 列表（doc_id 与元信息）。"""
    return [
        {
            "doc_id": c.doc_id,
            "title": c.title,
            "category": c.category,
            "series": c.series,
            "section": str(c.meta.get("section", "")),
        }
        for c in chunks
    ]


# ---------------------------------------------------------------------------
# 向量化包装（复用生产 bge-m3 + 落盘缓存）
# ---------------------------------------------------------------------------


class BgeM3Embeddings(Embeddings):
    """LangChain Embeddings 适配层：直连生产 bge-m3 (Ollama) 封装。

    ``cache_label`` 仅用于区分缓存文件名（不同切分策略互不覆盖），
    实际内容仍以全量文本 hash 为键。
    """

    def __init__(self, cache_label: int) -> None:
        self._label = cache_label

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return corpus_embeddings(self._label, texts)

    def embed_query(self, text: str) -> list[float]:
        return query_embedding(text)


def heading_baseline_embeddings(texts: list[str]) -> list[list[float]]:
    """标题分块基线：直接命中生产 embedding 缓存（内容与建库时一致）。"""
    return corpus_embeddings(900, texts)


# ---------------------------------------------------------------------------
# golden 测试集
# ---------------------------------------------------------------------------


def load_golden() -> list[dict]:
    """读取 golden_queries.yaml（285 问），返回 queries 列表。"""
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["queries"]


def doc_category(doc_id: str) -> str:
    """doc_id → 一级类目（用于分类目统计）。"""
    prefix = doc_id.split("-")[0].upper()
    mapping = {
        "A": "平台规则制度",
        "B": "买家指南",
        "C": "卖家指南",
        "D": "验货质检手册",
        "E": "售后仲裁SOP",
        "CASE": "真实场景案例库",
        "NEWS": "公告与课程",
        "FAQ": "结构化FAQ",
    }
    return mapping.get(prefix, "未知")


# ---------------------------------------------------------------------------
# 指标（doc 级）
# ---------------------------------------------------------------------------


def doc_metrics(ranked_doc_ids: list[str], expected: list[str]) -> dict:
    """doc 级指标：recall@k / Hit@k（k=1,3,5,10）+ MRR@10 + nDCG@10。

    入参 ``ranked_doc_ids`` 已按相关性排序（同一 doc 多块先去重）。
    """
    uniq = list(dict.fromkeys(ranked_doc_ids))
    exp = set(expected)
    n_exp = len(exp) or 1

    out: dict[str, float] = {}
    for k in KS:
        hit = sum(1 for d in uniq[:k] if d in exp)
        out[f"recall@{k}"] = hit / n_exp
        out[f"hit@{k}"] = 1.0 if hit > 0 else 0.0

    rr = 0.0
    for rank, did in enumerate(uniq[:10], 1):
        if did in exp:
            rr = 1.0 / rank
            break
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, did in enumerate(uniq[:10], 1)
        if did in exp
    )
    ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(len(exp), 10) + 1))
    out["mrr@10"] = rr
    out["ndcg@10"] = dcg / ideal if ideal else 0.0
    return out


def aggregate(per_query: list[dict]) -> dict:
    """对每问指标取均值，并附分类目 Hit@5。"""
    n = len(per_query)
    keys = ["recall@1", "recall@3", "recall@5", "recall@10",
            "hit@1", "hit@3", "hit@5", "hit@10", "mrr@10", "ndcg@10"]
    agg = {k: sum(q["metrics"][k] for q in per_query) / n for k in keys}

    cats: dict[str, list[float]] = {}
    for q in per_query:
        exp = q.get("expected") or []
        cat = doc_category(exp[0]) if exp else "未知"
        cats.setdefault(cat, []).append(q["metrics"]["hit@5"])
    agg["cat_hit5"] = {c: sum(v) / len(v) for c, v in sorted(cats.items())}
    agg["cat_n"] = {c: len(v) for c, v in sorted(cats.items())}
    agg["n"] = n
    return agg
