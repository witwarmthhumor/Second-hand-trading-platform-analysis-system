"""实验版可配置检索器：复用生产已建好的 Chroma + BM25 索引，仅调检索参数做 A/B。

与生产 ``HybridRetriever.search`` 的差异点（可调参数）：
- ``query_n_results``：向量/BM25 各自候选池大小（生产默认 20，瓶颈所在）
- ``bm25_weight``：RRF 融合时 BM25 分支的权重放大系数（>1 表示更信关键词）
- ``rrf_k``：RRF 平滑常数（越小排名差越大）

设计目标：不改生产 ``retriever.py``（避免影响在途改动），用同一份索引
复现/超越生产检索，定位「候选池太小导致多文档问题漏检」这一根因。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.embedding import query_embedding
from app.rag.retriever import HybridRetriever, diversify_with_refill


@dataclass
class SearchParams:
    """检索超参（与生产默认值对齐，便于逐个消融）。"""

    query_n_results: int = 20  # 候选池
    bm25_weight: float = 1.0  # BM25 在 RRF 中的权重放大
    rrf_k: float = 60.0  # RRF 平滑常数
    # 文档多样性重排：RRF 排序后，按 doc_id 去重（每文档只保留 RRF 最高分 chunk），
    # 再按该 chunk 的 RRF 分数重排。让 top_k 覆盖更多不同文档，避免单文档多 chunk
    # 重复占位挤掉其它相关文档。>0 时启用。
    max_chunks_per_doc: int = 0  # 0=关闭（生产默认行为）
    # 保底 + 补位（配合 max_chunks_per_doc>0 使用）：去重后若文档数不足 top_k，
    # 用同文档次优 chunk 补满名额，避免单文档问题因去重白白损失上下文。
    # 见 diversify_with_refill 的 docstring（这是生产接入的正确形态）。
    refill: bool = False
    # rerank 精排：RRF 融合后，用向量相似度二次排序（而非纯 RRF 分数）。
    # 动机：BM25 独有命中的 chunk 向量相似度为 0（纯关键词、语义无关），
    # 却通过 RRF 排到靠前位置，挤掉真正相关的 chunk。vsim 能压掉这类噪声。
    rerank_by_vsim: bool = False


class ConfigurableRetriever:
    """在既有索引上按 SearchParams 检索，返回与生产同结构的结果。"""

    def __init__(self, base: HybridRetriever, params: SearchParams):
        self._base = base
        self.params = params

    @property
    def is_ready(self) -> bool:
        return self._base.is_ready

    def search(self, query: str, top_k: int) -> list[dict]:
        col = self._base._collection
        bm25 = self._base._bm25
        doc_ids = self._base._doc_ids
        p = self.params

        qemb = query_embedding(query)

        # ① 向量检索（候选池 = p.query_n_results）
        vres = col.query(query_embeddings=[qemb], n_results=p.query_n_results)
        v_scores: dict[int, float] = {}
        v_texts: dict[int, str] = {}
        for cid, dist, doc in zip(
            vres["ids"][0], vres["distances"][0], vres["documents"][0], strict=False
        ):
            pos = int(cid[1:])
            v_scores[pos] = 1.0 - float(dist)
            v_texts[pos] = doc

        # ② BM25 检索（候选池 = p.query_n_results）
        b_scores: dict[int, float] = {}
        if bm25 is not None:
            for i, s in bm25.search(query, top_n=p.query_n_results):
                b_scores[i] = s

        # BM25 独有命中批量取原文
        missing = [i for i in b_scores if i not in v_texts]
        if missing:
            got = col.get(ids=[f"c{i}" for i in missing])
            doc_by_id = dict(zip(got["ids"], got["documents"], strict=False))
            for i in missing:
                doc = doc_by_id.get(f"c{i}")
                if doc is not None:
                    v_texts[i] = doc

        # ③ RRF 融合（k=p.rrf_k，BM25 加权 p.bm25_weight）
        rrf: dict[int, float] = {}
        for rank, pos in enumerate(
            sorted(v_scores, key=lambda k: -v_scores[k])[: p.query_n_results], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + 1.0 / (p.rrf_k + rank)
        for rank, pos in enumerate(
            sorted(b_scores, key=lambda k: -b_scores[k])[: p.query_n_results], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + p.bm25_weight / (p.rrf_k + rank)

        ranked = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]

        # rerank 精排：用向量相似度重排（BM25 独有命中 vsim=0 会被压到末尾）。
        # 仅对向量有分数的 chunk 生效；纯 BM25 命中（vsim 无值）按 0 处理。
        if p.rerank_by_vsim:
            ranked = sorted(
                ranked, key=lambda x: -v_scores.get(x[0], 0.0)
            )[:top_k]

        # 文档多样性重排（可选）：每文档最多 max_chunks_per_doc 个 chunk，
        # 按 doc 内最高 RRF 分数排序文档，再展开。目标是让 top_k 覆盖更多文档。
        if p.max_chunks_per_doc > 0:
            if p.refill:
                # 保底 + 补位：在**全量候选**上按 vsim 降序做文档级去重，
                # 名额不足时回填同文档次优 chunk（生产接入形态）。
                ranked_all = sorted(
                    rrf.items(), key=lambda x: -v_scores.get(x[0], 0.0)
                )
                ranked = diversify_with_refill(ranked_all, v_scores, doc_ids, top_k)
            else:
                # 硬去重：每文档只留 RRF 最高分 chunk，直接截断 top_k。
                best_per_doc: dict[str, float] = {}
                ordered: list[tuple[int, float]] = []
                for pos, score in sorted(rrf.items(), key=lambda x: -x[1]):
                    did = doc_ids[pos] if pos < len(doc_ids) else f"c{pos}"
                    if best_per_doc.get(did, -1.0) >= score:
                        continue
                    best_per_doc[did] = score
                    ordered.append((pos, score))
                ranked = ordered[:top_k]

        results: list[dict] = []
        for pos, score in ranked:
            results.append(
                {
                    # chunk_id 与生产 retriever.search 保持一致（评测 chunk 级覆盖率用）
                    "chunk_id": f"c{pos}",
                    "doc_id": doc_ids[pos] if pos < len(doc_ids) else f"c{pos}",
                    "text": v_texts.get(pos, ""),
                    "rrf_score": round(score, 6),
                    "vector_sim": round(v_scores.get(pos, 0.0), 4),
                }
            )
        return results


# 预置参数组：基线（=生产默认）+ 优化消融
PARAM_SETS: dict[str, SearchParams] = {
    "baseline": SearchParams(query_n_results=20, bm25_weight=1.0, rrf_k=60.0),
    "pool50": SearchParams(query_n_results=50, bm25_weight=1.0, rrf_k=60.0),
    "pool50_bm2": SearchParams(query_n_results=50, bm25_weight=2.0, rrf_k=60.0),
    "pool80_bm2_k30": SearchParams(query_n_results=80, bm25_weight=2.0, rrf_k=30.0),
    # 文档多样性重排：候选池 50，每文档最多 1 个 chunk（最大化文档覆盖）
    "diverse50": SearchParams(
        query_n_results=50, bm25_weight=1.0, rrf_k=60.0, max_chunks_per_doc=1
    ),
    # 文档多样性 + 更大候选池
    "diverse80": SearchParams(
        query_n_results=80, bm25_weight=1.0, rrf_k=60.0, max_chunks_per_doc=1
    ),
    # 保底 + 补位（生产接入形态）：候选池 50，每文档保底 1 篇，
    # 名额不足时用同文档次优 chunk 补满 —— 多文档最大化覆盖、单文档不损失。
    "diverse_refill": SearchParams(
        query_n_results=50,
        bm25_weight=1.0,
        rrf_k=60.0,
        max_chunks_per_doc=1,
        refill=True,
    ),
    "diverse_refill20": SearchParams(
        query_n_results=20,
        bm25_weight=1.0,
        rrf_k=60.0,
        max_chunks_per_doc=1,
        refill=True,
    ),
    # rerank 精排：RRF 候选 + vsim 二次排序（压掉 BM25 噪声）
    "rerank": SearchParams(
        query_n_results=20, bm25_weight=1.0, rrf_k=60.0, rerank_by_vsim=True
    ),
    # rerank + 更大候选池（更充分的候选再精排）
    "rerank_pool50": SearchParams(
        query_n_results=50, bm25_weight=1.0, rrf_k=60.0, rerank_by_vsim=True
    ),
}
