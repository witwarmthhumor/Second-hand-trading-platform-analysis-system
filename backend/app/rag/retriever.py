"""混合检索：Chroma 向量 + BM25（jieba 分词 + glossary 词典）+ RRF 融合。

迁移自 RAGreflow-second-hand 的 ``retrieval_eval/__main__.py``，抽成可复用类，
供建库脚本与问答服务共用。

持久化策略：Chroma 存向量 + 原文；BM25 需要的分词结果与 doc_id 列表
单独以 JSON 序列化到 ``vector_store/bm25_index.json``，保证服务重启后可完整
恢复混合检索（而非退化为纯向量）。用 JSON 而非 pickle，避免不可信反序列化
带来的 RCE 隐患（数据本身只是字符串列表，无需 pickle）。
"""

from __future__ import annotations

import json
from typing import Any

import chromadb
import jieba
import yaml
from rank_bm25 import BM25Okapi

from app.rag.cache import corpus_fingerprint
from app.rag.config import (
    CHROMA_ADD_BATCH,
    CHROMA_COLLECTION,
    DOC_DEDUP_ENABLED,
    FACTS_DIR,
    QUERY_N_RESULTS,
    RERANK_BY_VSIM,
    RRF_K,
    VECTOR_STORE_DIR,
)

_BM25_JSON = VECTOR_STORE_DIR / "bm25_index.json"


def rerank_by_vsim(ranked: list[tuple[int, float]], v_scores: dict[int, float], top_k: int) -> list[tuple[int, float]]:
    """RRF 排序结果按向量相似度二次排序（rerank 精排）。

    动机：BM25 独有命中的 chunk 向量相似度≈0（纯关键词、语义无关），却会
    通过 RRF 排到靠前位置，挤掉真正相关的 chunk。按 vsim 重排能把这类
    噪声（vsim 缺失按 0 处理）沉到末尾。纯函数，便于单测。

    实验依据：scripts/experiments/ragas_eval/README.md（Hit@5 0.9825→1.0000）。
    """
    return sorted(ranked, key=lambda x: -v_scores.get(x[0], 0.0))[:top_k]


def diversify_with_refill(
    ranked_all: list[tuple[int, float]],
    v_scores: dict[int, float],
    doc_ids: list[str],
    top_k: int,
) -> list[tuple[int, float]]:
    """文档级去重 + 保底补位（多文档召回的核心算法）。

    问题：top_k 是固定预算（默认 5 个格子），而同一文档的 chunk 主题相近、
    向量相似度相近，会「抱团占位」——多文档问题的 top-5 平均只落在 2.84 个
    不同文档上（而期望平均 7 篇以上），期望文档覆盖率仅 0.3755。注意这一
    短板**不会被 Hit@5 暴露**：Hit@5 只问「至少中一篇」，不惩罚「中 1 篇漏 4 篇」。

    算法（与「硬去重」的区别在第 ② 步）：
    ① 保底：按 vsim 降序扫描，每篇文档只留第一个（= 该文档 vsim 最高）的 chunk，
       保证 top_k 尽量覆盖不同文档；
    ② 补位：不同文档数不足 top_k 时（单文档/双文档问题常见），把剩余名额回填
       给同文档的次优 chunk —— 硬去重会白扔名额、损失上下文，这里不扔。

    前提：``ranked_all`` 须按**向量相似度降序**传入。如此每篇文档的「代表」
    就是该文档 vsim 最高的 chunk，输出排序后 ``[0]`` 仍是全局最相似 chunk，
    **命中判定口径（``vector_sim >= SIMILARITY_THRESHOLD``）不变**。

    实测（285 问，生产 top_k=5，见 docs/RAG优化方案.md）：
    baseline 多文档 coverage 0.3755 / 单文档 0.9755 / Hit(任意) 0.9825
    → 本算法 0.5052 / **1.0000** / **1.0000**，三项同时变好（帕累托改进）。
    对比硬去重（diverse50）：多文档仅 -1.35pp，但单文档 +3.07pp、Hit +2.46pp。

    纯函数，便于单测。
    """

    def _did(pos: int) -> str:
        return doc_ids[pos] if pos < len(doc_ids) else f"c{pos}"

    # ① 保底：每文档取排序中最靠前（vsim 最高）的一个 chunk
    picked: list[tuple[int, float]] = []
    picked_pos: set[int] = set()
    seen_docs: set[str] = set()
    for pos, score in ranked_all:
        did = _did(pos)
        if did in seen_docs:
            continue
        seen_docs.add(did)
        picked.append((pos, score))
        picked_pos.add(pos)
        if len(picked) >= top_k:
            break

    # ② 补位：名额未满（不同文档数 < top_k），回填同文档次优 chunk（按 vsim 降序）
    if len(picked) < top_k:
        rest = [(pos, score) for pos, score in ranked_all if pos not in picked_pos]
        rest.sort(key=lambda x: -v_scores.get(x[0], 0.0))
        picked.extend(rest[: top_k - len(picked)])

    # 输出保持全局按 vsim 降序：补位 chunk 的 vsim 必不高于其同文档代表，
    # 故首位仍是全局最相似 chunk，命中判定不受影响。
    picked.sort(key=lambda x: -v_scores.get(x[0], 0.0))
    return picked[:top_k]


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut(text) if t.strip()]


def _load_glossary_terms() -> list[str]:
    """glossary.yaml 的同义词词条加入 jieba 词典，避免「担保交易」等被切碎。"""
    g = FACTS_DIR / "glossary.yaml"
    if not g.exists():
        return []
    data = yaml.safe_load(g.read_text(encoding="utf-8")) or {}
    terms = list((data.get("synonyms") or {}).keys())
    for v in (data.get("synonyms") or {}).values():
        terms.extend(v)
    return terms


def _add_glossary_to_jieba() -> None:
    """glossary 词条加入 jieba 自定义词典（幂等）。"""
    for term in _load_glossary_terms():
        jieba.add_word(term, freq=1000)


class BM25Index:
    """BM25Okapi + jieba 分词，glossary 词条加入自定义词典。"""

    def __init__(self, docs_tokens: list[list[str]]):
        _add_glossary_to_jieba()
        self._bm25 = BM25Okapi(docs_tokens)

    def search(self, query: str, top_n: int) -> list[tuple[int, float]]:
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda x: -x[1])[:top_n]
        return [(i, float(s)) for i, s in ranked if s > 0]


# ---------------------------------------------------------------------------
# 混合检索器
# ---------------------------------------------------------------------------


class HybridRetriever:
    """向量 + BM25 + RRF 混合检索，封装 Chroma 建库与查询。"""

    def __init__(self) -> None:
        # chromadb 未提供完整类型桩，标注 Any 保留类型检查入口；实际为 PersistentClient
        self._client: Any = chromadb.PersistentClient(path=str(VECTOR_STORE_DIR))
        self._collection: Any = None
        self._bm25: BM25Index | None = None
        self._doc_ids: list[str] = []
        self._ready = False
        # 语料指纹（作为检索缓存的 key 维度），首次访问时计算
        self._corpus_version: str | None = None

    # ---- 语料版本 ----

    @property
    def corpus_version(self) -> str:
        """语料版本指纹：bm25_index.json + chroma.sqlite3 的 mtime 哈希。

        用于缓存 key：语料被替换后指纹变化，旧缓存自然失效。
        """
        if self._corpus_version is None:
            self._corpus_version = corpus_fingerprint(
                (_BM25_JSON, VECTOR_STORE_DIR / "chroma.sqlite3")
            )
        return self._corpus_version

    # ---- 建库 ----

    def build(self, chunks: list, embeddings: list[list[float]]) -> int:
        """用分块 + embedding 重建 Chroma 库与 BM25 索引，返回入库 chunk 数。

        幂等：同名集合先删后建；BM25 分词结果 JSON 落盘供重启恢复。
        """
        import contextlib

        texts = [c.text for c in chunks]
        doc_ids = [c.doc_id for c in chunks]
        tokens = [_tokenize(t) for t in texts]

        with contextlib.suppress(Exception):
            self._client.delete_collection(CHROMA_COLLECTION)
        col = self._client.get_or_create_collection(
            CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        for i in range(0, len(chunks), CHROMA_ADD_BATCH):
            end = min(i + CHROMA_ADD_BATCH, len(chunks))
            col.add(
                ids=[f"c{k}" for k in range(i, end)],
                embeddings=embeddings[i:end],
                documents=texts[i:end],
            )

        # 持久化 BM25 所需的分词结果与 doc_id 列表（JSON，非 pickle）
        VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)
        _BM25_JSON.write_text(
            json.dumps({"doc_ids": doc_ids, "tokens": tokens}, ensure_ascii=False),
            encoding="utf-8",
        )

        self._collection = col
        self._doc_ids = doc_ids
        self._bm25 = BM25Index(tokens)
        self._ready = True
        self._corpus_version = None  # 语料已变，指纹下次访问重算
        return len(chunks)

    # ---- 加载已有库 ----

    def load(self) -> bool:
        """加载已持久化的 Chroma 库与 BM25 索引；库不存在返回 False。"""
        if not VECTOR_STORE_DIR.exists():
            return False
        try:
            col = self._client.get_collection(CHROMA_COLLECTION)
        except Exception:
            return False

        self._collection = col
        # 恢复 BM25 索引（分词结果 JSON 存在才完整恢复，否则退化为纯向量）
        if _BM25_JSON.exists():
            try:
                data = json.loads(_BM25_JSON.read_text(encoding="utf-8"))
                self._doc_ids = data["doc_ids"]
                self._bm25 = BM25Index(data["tokens"])
            except Exception:
                self._bm25 = None
                self._doc_ids = []
        else:
            self._bm25 = None
            self._doc_ids = []
        self._ready = True
        return True

    # ---- 查询 ----

    @property
    def is_ready(self) -> bool:
        return self._ready

    def search(self, query: str, top_k: int) -> list[dict]:
        """混合检索，返回 top_k 条结果，每项含 doc_id/title/text/rrf_score/vector_sim。"""
        if self._collection is None:
            raise RuntimeError("向量库未就绪，请先运行 build_vector_store.py")

        from app.rag.embedding import query_embedding

        qemb = query_embedding(query)

        # ① 向量检索
        vres = self._collection.query(
            query_embeddings=[qemb], n_results=QUERY_N_RESULTS
        )
        v_scores: dict[int, float] = {}
        v_texts: dict[int, str] = {}
        for cid, dist, doc in zip(
            vres["ids"][0], vres["distances"][0], vres["documents"][0], strict=False
        ):
            pos = int(cid[1:])  # "c123" -> 123
            v_scores[pos] = 1.0 - float(dist)  # cosine distance → similarity
            v_texts[pos] = doc

        # ② BM25 检索（索引存在时）
        b_scores: dict[int, float] = {}
        if self._bm25 is not None:
            for i, s in self._bm25.search(query, top_n=QUERY_N_RESULTS):
                b_scores[i] = s

        # BM25 命中但向量结果里没有的 chunk，一次批量取回原文。
        # 注意：不要逐个 collection.get(ids=[...])（实测每次检索约 17 次单点查，占
        # 完整检索耗时约 19%）；批量取回后按返回的 ids 建映射，不依赖返回顺序。
        missing = [i for i in b_scores if i not in v_texts]
        if missing:
            got = self._collection.get(ids=[f"c{i}" for i in missing])
            doc_by_id = dict(zip(got["ids"], got["documents"], strict=False))
            for i in missing:
                doc = doc_by_id.get(f"c{i}")
                if doc is not None:
                    v_texts[i] = doc

        # ③ RRF 融合（k=60）
        rrf: dict[int, float] = {}
        for rank, pos in enumerate(
            sorted(v_scores, key=lambda k: -v_scores[k])[:QUERY_N_RESULTS], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + 1.0 / (RRF_K + rank)
        for rank, pos in enumerate(
            sorted(b_scores, key=lambda k: -b_scores[k])[:QUERY_N_RESULTS], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + 1.0 / (RRF_K + rank)

        ranked = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]

        # 多文档召回（可选，默认关）：文档级去重 + 保底补位。
        # 注意要在**全量 RRF 候选**上做——若只看已截断的 top_k，就永远救不回
        # 被同文档 chunk 挤掉的其它文档。见 diversify_with_refill 与 docs/RAG优化方案.md。
        if DOC_DEDUP_ENABLED:
            ranked_all = sorted(rrf.items(), key=lambda x: -v_scores.get(x[0], 0.0))
            ranked = diversify_with_refill(ranked_all, v_scores, self._doc_ids, top_k)
        # rerank 精排（可选，默认开）：用向量相似度对 RRF 候选二次排序。
        # 见 rerank_by_vsim 函数文档与 scripts/experiments/ragas_eval/README.md。
        elif RERANK_BY_VSIM:
            ranked = rerank_by_vsim(ranked, v_scores, top_k)

        results: list[dict] = []
        for pos, score in ranked:
            results.append(
                {
                    "doc_id": self._doc_ids[pos]
                    if pos < len(self._doc_ids)
                    else f"c{pos}",
                    "text": v_texts.get(pos, ""),
                    "rrf_score": round(score, 6),
                    "vector_sim": round(v_scores.get(pos, 0.0), 4),
                }
            )
        return results


# 全局单例（进程内复用，避免重复建 BM25/加载 Chroma）
_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    """惰性单例；首次访问时若库已存在则自动加载。"""
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
        if not _retriever.load():
            _retriever = HybridRetriever()  # 库未建，保持未就绪状态
    return _retriever


def reload_retriever() -> bool:
    """重建向量库后刷新单例：丢弃旧的 Chroma collection 句柄并重新加载。

    必要性：`kb_service.rebuild_vector_store()` 会先删集合再重建，而单例里持有的
    是**旧 collection 句柄**，重建后继续用它检索会失败/取不到数据，必须换新实例。
    返回库是否加载成功。
    """
    global _retriever
    fresh = HybridRetriever()
    ok = fresh.load()
    _retriever = fresh
    return ok
