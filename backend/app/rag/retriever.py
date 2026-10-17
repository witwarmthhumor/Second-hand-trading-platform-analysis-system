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
import logging
from typing import Any

import chromadb
import jieba
import yaml
from rank_bm25 import BM25Okapi

from app.rag.cache import corpus_fingerprint
from app.rag.config import (
    ANCHOR_QUOTA,
    BREADTH_QUOTA,
    CHROMA_ADD_BATCH,
    CHROMA_COLLECTION,
    DEPTH_QUOTA,
    DOC_DEDUP_ENABLED,
    DOC_MIN_SIM,
    FACTS_DIR,
    QUERY_N_RESULTS,
    RERANK_BY_VSIM,
    RERANKER_TOP_N,
    RRF_K,
    VECTOR_STORE_DIR,
)
from app.rag.reranker import get_active_reranker

logger = logging.getLogger(__name__)

_BM25_JSON = VECTOR_STORE_DIR / "bm25_index.json"


def sort_by_vsim(ranked: list[tuple[int, float]], v_scores: dict[int, float], top_k: int) -> list[tuple[int, float]]:
    """RRF 排序结果按 bge-m3 余弦二次排序（**不是** cross-encoder rerank）。

    命名说明（2026-09-19 由 ``rerank_by_vsim`` 改名）：本项目里 "rerank" 专指
    cross-encoder 精排（``_rerank_scores`` / 结果的 ``rerank_score`` 字段）；本函数
    只是 bi-encoder 余弦的二次排序，改名以消除同名不同物。历史文档
    （docs/RAG优化方案.md）里的旧名不改。

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
    min_sim: float = 0.0,
    anchor_quota: int = 1,
) -> list[tuple[int, float]]:
    """文档级去重 + 保底补位（多文档召回的核心算法）。

    问题：top_k 是固定预算（默认 5 个格子），而同一文档的 chunk 主题相近、
    向量相似度相近，会「抱团占位」——多文档问题的 top-5 平均只落在 2.84 个
    不同文档上（而期望平均 7 篇以上），期望文档覆盖率仅 0.3755。注意这一
    短板**不会被 Hit@5 暴露**：Hit@5 只问「至少中一篇」，不惩罚「中 1 篇漏 4 篇」。

    算法（与「硬去重」的区别在第 ② 步）：
    ① 保底：按 vsim 降序扫描，每篇文档只留第一个（= 该文档 vsim 最高）的 chunk，
       保证 top_k 尽量覆盖不同文档；其中锚定文档（见 ``anchor_quota``）可多占；
    ② 补位：不同文档数不足 top_k 时（单文档/双文档问题常见），把剩余名额回填
       给同文档的次优 chunk —— 硬去重会白扔名额、损失上下文，这里不扔。

    前提：``ranked_all`` 须按**向量相似度降序**传入。如此每篇文档的「代表」
    就是该文档 vsim 最高的 chunk，输出排序后 ``[0]`` 仍是全局最相似 chunk，
    **命中判定口径（``vector_sim >= SIMILARITY_THRESHOLD``）不变**。

    ``anchor_quota``（锚定 top1 文档，默认 1 = 原行为）：保底阶段允许
    **top1 所在文档**（ranked_all 首位，即全局 vsim 最高 chunk 的文档）多占几个
    名额，其余文档仍各 1 个。动机见 docs/RAG优化方案.md 4.7：P0 上线后单文档
    问题的 top-5 里「期望文档」占位均值从 3.56 掉到 **1.00**，深度全丢；而在
    bge-m3 的相似度标尺上，「相关但非期望」与「期望」文档的 vsim 比值分布严重
    重叠（中位 0.898 vs 0.934），**用阈值区分问题类型行不通**；唯有「top1 文档
    是主文档」这个假设可靠（hit1 0.9158）。故把名额分配改成显式旋钮：
    anchor_quota 越大越偏深度（单文档问题受益），越小越偏广度（多文档受益）。

    ``min_sim``（相关性闸门，默认 0=关闭）：保底扫描到某文档首个（代表）chunk
    时，若其 vsim 低于该阈值则整篇跳过、不占名额。**实测在 bge-m3 上几乎无效**
    （候选池内各文档代表块的 vsim 普遍 ≥0.55），仅在 0.6 以上才微微起效且伴随
    多文档 coverage 下降，故生产默认关闭；保留实现以备换 embedding 模型后复用。
    **补位阶段刻意不设闸门**：若所有文档都不达标（难查询），保底会空手，此时
    补位必须能把全局 vsim 最高的若干 chunk 原样填回，否则会返回空结果。

    实测（285 问，生产 top_k=5，见 docs/RAG优化方案.md）：
    baseline 多文档 coverage 0.3755 / 单文档 0.9755 / Hit(任意) 0.9825
    → 本算法（anchor_quota=1）0.5052 / **1.0000** / **1.0000**，三项同时变好
    （帕累托改进）。对比硬去重（diverse50）：多文档仅 -1.35pp，但单文档
    +3.07pp、Hit +2.46pp。anchor_quota 的深度/广度权衡实测见 4.7。

    纯函数，便于单测。
    """

    def _did(pos: int) -> str:
        return doc_ids[pos] if pos < len(doc_ids) else f"c{pos}"

    # 锚定文档 = 全局 vsim 最高 chunk（ranked_all 首位）所属文档，可多占名额。
    anchor = _did(ranked_all[0][0]) if ranked_all else None

    # ① 保底：按 vsim 降序扫描，锚定文档最多 anchor_quota 个 chunk，
    #    其余文档各 1 个（= 该文档 vsim 最高的代表 chunk）。
    picked: list[tuple[int, float]] = []
    picked_pos: set[int] = set()
    counts: dict[str, int] = {}
    for pos, score in ranked_all:
        did = _did(pos)
        quota = anchor_quota if did == anchor else 1
        n = counts.get(did, 0)
        if n >= quota:
            continue
        # 相关性闸门：只在文档「首次出现」（代表 chunk）时判断；不达标就把
        # counts 直接置为 quota，等价于整篇跳过（同文档后续 chunk vsim 更低）。
        if n == 0 and min_sim > 0.0 and v_scores.get(pos, 0.0) < min_sim:
            counts[did] = quota
            continue
        counts[did] = n + 1
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


def dual_views(
    ranked_all: list[tuple[int, float]],
    v_scores: dict[int, float],
    doc_ids: list[str],
    top_k: int,
    breadth_quota: int,
    depth_quota: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """同一候选池产出「广度/深度」两个 top-k 名单（P1-② 双视图核心，纯函数）。

    `diversify_with_refill` 是纯函数 → 对同一份 ``ranked_all`` 按不同配额各排
    一次即得两视图，**零额外检索成本**（一次 embedding + 一次 RRF 的产物复用）：

    - 广度视图（``breadth_quota``，生产默认 = ``ANCHOR_QUOTA`` = 1）：来源列表用，
      与现网 ``search()`` 的输出**逐位一致**（同候选池 + 同配额，单测对拍守卫）；
    - 深度视图（``depth_quota``，默认 4）：合成上下文用，top1 文档多占名额，
      生成侧实测消除 20% 无谓拒答（116b933）。

    ``DOC_DEDUP_ENABLED`` 关闭时（非生产形态）调用方应让两视图退化为同一份
    rerank 结果，不经过本函数。输入契约同 diversify_with_refill：``ranked_all``
    须按 vsim 降序传入。纯函数，便于单测。
    """
    breadth = diversify_with_refill(
        ranked_all, v_scores, doc_ids, top_k, anchor_quota=breadth_quota
    )
    depth = diversify_with_refill(
        ranked_all, v_scores, doc_ids, top_k, anchor_quota=depth_quota
    )
    return breadth, depth


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

    def _hybrid_candidates(
        self, query: str
    ) -> tuple[dict[int, float], dict[int, float], dict[int, str]]:
        """search 公共主体：embedding → 向量 + BM25 → RRF 融合。

        ``search`` 与 ``search_dual`` 共用，保证两个视图天然同源（同一份候选
        池），不重复调用 embedding。返回 (rrf, v_scores, v_texts)。
        """
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

        return rrf, v_scores, v_texts

    def _rerank_scores(
        self,
        query: str,
        rrf: dict[int, float],
        v_texts: dict[int, str],
        top_k: int | None = None,
    ) -> dict[int, float]:
        """候选池打分（US-005）：只对 **RRF 排序前 ``RERANKER_TOP_N``** 个候选打
        cross-encoder 分，窗口外的候选按 0（只重排、不扩大召回，见 docs/Rerank方案.md）。

        ``top_k``（可选）：调用方（``search`` / ``search_dual``）传入本次请求的返回
        名额。窗口硬下限守卫（质量审计 P2-G）：``RERANKER_TOP_N`` 低于 ``top_k`` 时
        把窗口放大到 ``top_k`` 并记 warning——否则 top-k 名单会混入未打分候选
        （``rerank_score=None`` 按回退序占位），静默劣化排序。窗口只可能被放大、
        不会因它缩小（「只重排、不扩召回」的窗口上界仍由候选池决定）。

        开关与可用性都收在 ``get_active_reranker()`` 里：拿到 None 就直接返回空字典，
        不碰模型目录、不产生任何新增计算（``RERANKER_ENABLED=false`` 的部署零开销）。

        Returns:
            ``pos → 分数``；**未启用 / 不可用 / 打分失败时返回空字典**。
            空字典 = 「本次没有 rerank 分数」，调用方据此原样退回余弦排序。
            不要用「全 0 分」表达失败——那会让所有候选同分、静默改变排序。

        降级可观测性（US-009）：三类故障各有**一条含原因**的 warning ——
        模型文件缺失 / 加载失败由 ``reranker._load()`` 记（此处拿到 None 直接返回）；
        推理失败由 ``reranker.score()`` 记，这里再补一条**检索侧**的日志，说明
        「本次检索已退回余弦排序」。故障一律被吞成空字典，**绝不向上抛**。
        """
        reranker = get_active_reranker()
        if reranker is None:
            # 未启用（默认）或不可用。不可用时 reranker._load() 已记过含原因的 warning。
            return {}
        window_n = RERANKER_TOP_N
        if top_k is not None and top_k > RERANKER_TOP_N:
            window_n = top_k
            logger.warning(
                "RERANKER_TOP_N=%d 低于本次 top_k=%d，打分窗口已放大到 %d"
                "（配置疑误：窗口必须 ≥ top_k，否则 top-k 名单会混入未打分候选）",
                RERANKER_TOP_N,
                top_k,
                window_n,
            )
        window = sorted(rrf, key=lambda pos: -rrf[pos])[:window_n]
        if not window:
            return {}
        scores = reranker.score(query, [v_texts.get(pos, "") for pos in window])
        if not scores:
            # 空列表 = 不可用或推理失败（reranker 自己已记过含原因的 warning）。
            # 这条日志补的是「检索侧视角」：单看 reranker 那条只知道打分失败，
            # 不知道本次检索是否真的退回了余弦排序。与下面的「半截分数」分开写，
            # 避免把「整体打不出分」误报成「长度不符（0 != N）」。
            logger.warning(
                "rerank 未产出分数（%d 个候选），本次退回余弦排序：%s",
                len(window),
                getattr(reranker, "load_error", None) or "推理失败",
            )
            return {}
        if len(scores) != len(window):
            # reranker 的契约是「与入参等长，打不出分则空列表」。出现半截分数说明
            # 状态异常，宁可整体不启用，也不要用「部分分数 + 部分 0」的半开排序。
            logger.warning(
                "rerank 分数长度不符（%d != %d），本次退回余弦排序",
                len(scores),
                len(window),
            )
            return {}
        return dict(zip(window, scores, strict=True))

    def search(self, query: str, top_k: int) -> list[dict]:
        """混合检索，返回 top_k 条结果。

        每项含 doc_id/chunk_id/text/rrf_score/vector_sim/rerank_score。
        ``vector_sim`` 始终是 bge-m3 余弦（原义不变）；``rerank_score`` 是
        cross-encoder 分，rerank 未启用或该 chunk 不在打分窗口内时为 None。
        """
        rrf, v_scores, v_texts = self._hybrid_candidates(query)
        rerank = self._rerank_scores(query, rrf, v_texts, top_k=top_k)
        # 排序键：rerank 生效时用 cross-encoder 分（窗口外按 0），否则仍是余弦。
        rank_scores = rerank or v_scores

        # 多文档召回（可选，默认开）：文档级去重 + 保底补位。
        # 注意要在**全量 RRF 候选**上做——若只看已截断的 top_k，就永远救不回
        # 被同文档 chunk 挤掉的其它文档。见 diversify_with_refill 与 docs/RAG优化方案.md。
        # US-005：排序分数统一换成 rank_scores —— rerank 开启时「锚定文档」变为
        # cross-encoder 最高分所在文档，补位也按该分排序（函数本身一行不改）。
        if DOC_DEDUP_ENABLED:
            ranked_all = sorted(rrf.items(), key=lambda x: -rank_scores.get(x[0], 0.0))
            ranked = diversify_with_refill(
                ranked_all,
                rank_scores,
                self._doc_ids,
                top_k,
                min_sim=DOC_MIN_SIM,
                anchor_quota=ANCHOR_QUOTA,
            )
        else:
            # 基础排序只在非去重路径需要（去重分支自己全量排序）；放分支内避免
            # 生产形态下每次检索白算一次（质量审计 P3-R）。
            ranked = sorted(rrf.items(), key=lambda x: -x[1])[:top_k]
            # vsim 二次排序（可选）：用 bge-m3 余弦对 RRF 候选沉噪声。
            # 见 sort_by_vsim 函数文档与 scripts/experiments/ragas_eval/README.md。
            if RERANK_BY_VSIM:
                ranked = sort_by_vsim(ranked, rank_scores, top_k)

        return self._assemble_results(ranked, v_scores, v_texts, rerank)

    def search_dual(self, query: str, top_k: int) -> tuple[list[dict], list[dict]]:
        """一次检索产出双视图：(广度视图, 深度视图)（P1-②，见 docs/合成答案分流方案.md）。

        广度视图（BREADTH_QUOTA，默认 = ANCHOR_QUOTA）与 ``search()`` 输出**逐位
        一致**（同候选池 + 同配额，单测对拍守卫），供来源列表使用；深度视图
        （DEPTH_QUOTA，默认 4）top1 文档多占名额，仅供 rag_service 的合成路径
        作上下文，不出现在任何用户可见输出。

        DOC_DEDUP_ENABLED 关闭时（非生产形态）无法做名额分配，两个视图退化为
        同一份 rerank 结果。
        """
        rrf, v_scores, v_texts = self._hybrid_candidates(query)
        rerank = self._rerank_scores(query, rrf, v_texts, top_k=top_k)
        # 排序键：rerank 生效时用 cross-encoder 分（窗口外按 0），否则仍是余弦。
        rank_scores = rerank or v_scores

        if DOC_DEDUP_ENABLED:
            ranked_all = sorted(rrf.items(), key=lambda x: -rank_scores.get(x[0], 0.0))
            breadth, depth = dual_views(
                ranked_all,
                rank_scores,
                self._doc_ids,
                top_k,
                breadth_quota=BREADTH_QUOTA,
                depth_quota=DEPTH_QUOTA,
            )
            return (
                self._assemble_results(breadth, v_scores, v_texts, rerank),
                self._assemble_results(depth, v_scores, v_texts, rerank),
            )

        ranked = sort_by_vsim(
            sorted(rrf.items(), key=lambda x: -x[1])[:top_k], rank_scores, top_k
        )
        results = self._assemble_results(ranked, v_scores, v_texts, rerank)
        return results, results

    def _assemble_results(
        self,
        ranked: list[tuple[int, float]],
        v_scores: dict[int, float],
        v_texts: dict[int, str],
        rerank_scores: dict[int, float] | None = None,
    ) -> list[dict]:
        """把 (pos, rrf_score) 名单组装为结果 dict 列表（search/search_dual 共用）。

        ``rerank_scores``（US-005）是 cross-encoder 分（pos → 分）。rerank 未启用
        或该 chunk 不在打分窗口内 → ``rerank_score`` 为 None。``vector_sim`` 永远是
        bge-m3 余弦、语义不随 rerank 改变（命中判定读的是它，见 US-006）。
        """
        results: list[dict] = []
        for pos, score in ranked:
            rerank_score = (
                round(rerank_scores[pos], 6)
                if rerank_scores and pos in rerank_scores
                else None
            )
            results.append(
                {
                    # chunk_id = 全局位置编号（"c<pos>"），与向量库 / BM25 索引同源。
                    # 供评测做 **chunk 级** 覆盖率：doc 级口径只要某一篇期望文档的
                    # 任意一个 chunk 进 top-k 就算覆盖整篇，而一篇平均切 9.46 块
                    # （FAQ 达 25 块），会把「命中 6 块里的 1 块」也算满分。
                    "chunk_id": f"c{pos}",
                    "doc_id": self._doc_ids[pos]
                    if pos < len(self._doc_ids)
                    else f"c{pos}",
                    "text": v_texts.get(pos, ""),
                    "rrf_score": round(score, 6),
                    "vector_sim": round(v_scores.get(pos, 0.0), 4),
                    "rerank_score": rerank_score,
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
