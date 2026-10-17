"""检索链路接入 cross-encoder 精排（US-005）的离线测试。

被测对象：``app.rag.retriever.HybridRetriever.search`` / ``search_dual`` 与新增的
``_rerank_scores``。核心承诺（见 ``docs/Rerank方案.md`` §4.1）：

1. **只重排、不扩大召回**：只对 RRF 排序前 ``RERANKER_TOP_N`` 个候选打分，
   窗口外的候选按 0（不是丢弃、也不是补召回）；
2. **排序键替换**：``sorted(rrf.items(), key=...)`` 与传给
   ``diversify_with_refill`` / ``dual_views`` 的排序分数统一换成 cross-encoder 分，
   于是「锚定文档」与补位顺序都跟着变 —— 但那两个纯函数的**签名与算法一行不改**；
3. **关闭态零影响**：rerank 未启用 / 不可用 / 打分失败时，输出与纯余弦路径逐位一致；
4. ``_assemble_results`` 的 ``rerank_score`` 字段：窗口外或未启用时为 None，
   ``vector_sim`` 永远是 bge-m3 余弦（命中判定读它，见 US-006）。

不加载真模型、不打开 Chroma：候选池由 ``_hybrid_candidates`` 替身注入，
reranker 由 ``get_active_reranker`` 替身注入。
"""

from __future__ import annotations

import inspect

from app.rag import retriever as retriever_module
from app.rag.config import ANCHOR_QUOTA, DOC_MIN_SIM
from app.rag.retriever import HybridRetriever, diversify_with_refill, dual_views

# ---------------------------------------------------------------------------
# 候选池（8 个 chunk，4 篇文档）
# ---------------------------------------------------------------------------
# 刻意让「余弦序」与「cross-encoder 序」几乎相反：
#   * 余弦：A 的 0/1 最高（0.90 / 0.88），B 的 2~5 次之，C/D 垫底；
#   * rerank：B 的 2~5 包揽前四，A 的 0/1 垫底。
# 这样「排序键是否真的换掉了」一眼可辨（锚定文档从 A 翻到 B）。
_DOC_IDS = ["A", "A", "B", "B", "B", "B", "C", "D"]
_V_SCORES = {0: 0.90, 1: 0.88, 2: 0.86, 3: 0.84, 4: 0.80, 5: 0.78, 6: 0.70, 7: 0.68}
# RRF 序恰好与 pos 升序一致 → 打分窗口（前 RERANKER_TOP_N 个）就是 0,1,2,...
_RRF = {0: 0.040, 1: 0.038, 2: 0.036, 3: 0.034, 4: 0.032, 5: 0.030, 6: 0.028, 7: 0.026}
# 窗口内（= pos 0..7）的 cross-encoder 分
_RERANK = [0.20, 0.10, 0.99, 0.95, 0.90, 0.85, 0.80, 0.75]


def _texts() -> dict[int, str]:
    return {pos: f"chunk-{pos}" for pos in _RRF}


class _FakeReranker:
    """真 reranker 的契约替身：``score`` 与入参等长（打不出分则空列表）。"""

    def __init__(self, scores: list[float], available: bool = True):
        self._scores = list(scores)
        self.available = available
        self.load_error = None
        self.seen_passages: list[list[str]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.seen_passages.append(list(passages))
        return self._scores[: len(passages)]


def _build(monkeypatch, reranker):
    """造一个只暴露候选池的检索器替身，返回 (实例, 可中途切换精排器的盒子)。

    绕开 ``__init__``（会建 Chroma PersistentClient）；``_hybrid_candidates`` 用
    实例属性覆盖，reranker 经模块级 ``get_active_reranker`` 注入（消费方模块属性，
    见 Codebase Patterns 的「模块级属性注入」约定）。
    """
    box = {"value": reranker}
    monkeypatch.setattr(retriever_module, "get_active_reranker", lambda: box["value"])
    inst = HybridRetriever.__new__(HybridRetriever)
    inst._doc_ids = list(_DOC_IDS)
    monkeypatch.setattr(
        inst, "_hybrid_candidates", lambda query: (dict(_RRF), dict(_V_SCORES), _texts())
    )
    return inst, box


def _ids(results: list[dict]) -> list[str]:
    return [r["chunk_id"] for r in results]


# ---------------------------------------------------------------------------
# 关闭态：与改造前的纯余弦路径逐位一致
# ---------------------------------------------------------------------------


def test_关闭态_search_与余弦路径逐位一致(monkeypatch):
    """rerank 未启用时输出与「纯余弦 + diversify_with_refill」逐位一致。"""
    inst, _ = _build(monkeypatch, None)
    out = inst.search("q", top_k=5)

    ranked_all = sorted(_RRF.items(), key=lambda x: -_V_SCORES[x[0]])
    expected = diversify_with_refill(
        ranked_all, _V_SCORES, _DOC_IDS, 5, min_sim=DOC_MIN_SIM, anchor_quota=ANCHOR_QUOTA
    )

    assert _ids(out) == [f"c{pos}" for pos, _ in expected] == ["c0", "c1", "c2", "c6", "c7"]
    assert [r["vector_sim"] for r in out] == [_V_SCORES[pos] for pos, _ in expected]
    # rerank_score 字段存在但为 None（字段口径稳定，消费方不必判 key 是否存在）
    assert set(out[0]) == {
        "chunk_id",
        "doc_id",
        "text",
        "rrf_score",
        "vector_sim",
        "rerank_score",
    }
    assert [r["rerank_score"] for r in out] == [None] * 5


def test_关闭态_search_dual_广度视图与search逐位一致(monkeypatch):
    """关闭态下 dual_views 的广度视图仍 == search() 输出（既有承诺不因本 story 漂移）。"""
    inst, _ = _build(monkeypatch, None)
    single = inst.search("q", top_k=5)
    breadth, depth = inst.search_dual("q", top_k=5)

    assert _ids(breadth) == _ids(single)
    assert [r["rerank_score"] for r in breadth + depth] == [None] * 10
    # 锚定文档仍是全局余弦最高的 chunk（pos0，文档 A）
    assert depth[0]["doc_id"] == "A"


# ---------------------------------------------------------------------------
# 开启态：排序键换成 cross-encoder 分
# ---------------------------------------------------------------------------


def test_开启态_search_按cross_encoder分排序(monkeypatch):
    """开启后排序键是 cross-encoder 分：B 的 2~5 上位，余弦最高的 pos0 掉到末位。"""
    inst, _ = _build(monkeypatch, _FakeReranker(_RERANK))
    out = inst.search("q", top_k=5)

    assert _ids(out) == ["c2", "c3", "c6", "c7", "c0"]
    assert [r["rerank_score"] for r in out] == [0.99, 0.95, 0.80, 0.75, 0.20]
    # vector_sim 保持 bge-m3 余弦原义：首位 c2 是 0.86，而余弦最高的 pos0(0.90) 排末位
    assert [r["vector_sim"] for r in out] == [0.86, 0.84, 0.70, 0.68, 0.90]
    assert out[0]["doc_id"] == "B"


def test_开启态_锚定文档随排序键切换_深度视图吃到名额(monkeypatch):
    """锚定文档 = 排序分最高所在文档：开启后从 A 变成 B，深度视图的配额随之给到 B。"""
    inst, _ = _build(monkeypatch, _FakeReranker(_RERANK))
    breadth, depth = inst.search_dual("q", top_k=5)

    assert _ids(breadth) == ["c2", "c3", "c6", "c7", "c0"]
    # DEPTH_QUOTA=4 全部给了锚定文档 B（c2/c3/c4/c5），广度仍覆盖 4 篇文档
    assert _ids(depth) == ["c2", "c3", "c4", "c5", "c6"]
    assert depth[0]["doc_id"] == "B"
    assert len({r["doc_id"] for r in breadth}) == 4


def test_开关两态输出对拍(monkeypatch):
    """同一候选池：关 → 余弦序；开 → cross-encoder 序。两态确实不同且各自自洽。"""
    inst, box = _build(monkeypatch, None)
    off = inst.search("q", top_k=5)

    box["value"] = _FakeReranker(_RERANK)
    on = inst.search("q", top_k=5)

    assert _ids(off) == ["c0", "c1", "c2", "c6", "c7"]
    assert _ids(on) == ["c2", "c3", "c6", "c7", "c0"]
    # 两态读的是同一份候选池与同一份余弦：rerank 只改排序，vector_sim 一律是原余弦
    for r in off + on:
        assert r["vector_sim"] == _V_SCORES[int(r["chunk_id"][1:])]


# ---------------------------------------------------------------------------
# 打分窗口：只对 RRF 前 RERANKER_TOP_N 个打分，其余按 0
# ---------------------------------------------------------------------------


def test_只对RRF前N个打分_窗口外按0且不计分(monkeypatch):
    """窗口 = RRF 前 N（不扩召回）；窗口外候选 rerank_score 为 None、排序按 0。

    search 部分：top_k=3 时保底序为 [c1(A锚定), c2(B), c6(C)]——c6 在窗口外
    （TOP_N=3），被「按 0 参与排序」的保底带进名额，它的 ``rerank_score`` 必须
    是 None 而非 0.0（None = 未参与 rerank，区别于真打了 0 分）。
    """
    fake = _FakeReranker([0.5, 0.9, 0.4])
    inst, _ = _build(monkeypatch, fake)
    monkeypatch.setattr(retriever_module, "RERANKER_TOP_N", 3)

    assert inst._rerank_scores("q", dict(_RRF), _texts()) == {0: 0.5, 1: 0.9, 2: 0.4}
    assert fake.seen_passages == [["chunk-0", "chunk-1", "chunk-2"]]

    out = inst.search("q", top_k=3)
    assert _ids(out) == ["c1", "c2", "c6"]
    assert [r["rerank_score"] for r in out] == [0.9, 0.4, None]


def test_窗口低于top_k时自动放大并告警(monkeypatch, caplog):
    """守卫（质量审计 P2-G）：``RERANKER_TOP_N`` < top_k 时窗口放大到 top_k 并告警。

    防的是 env 误配下的**静默劣化**：不放大时 pos3/pos4（RRF 前 5）没有分数、
    按 0 参与排序——本例 c3（真分 0.95）会从第 2 位掉到补位末位。放大后 RRF
    前 top_k 全部有真分。窗口外的 c6/c7 由保底带入，``rerank_score`` 仍是 None
    （窗口口径不变：None = 不在打分窗口内，与误配无关）。
    """
    import logging

    inst, _ = _build(monkeypatch, _FakeReranker(_RERANK))
    monkeypatch.setattr(retriever_module, "RERANKER_TOP_N", 3)

    with caplog.at_level(logging.WARNING, logger="app.rag.retriever"):
        out = inst.search("q", top_k=5)

    assert any("低于本次 top_k" in r.message for r in caplog.records)
    # 放大后 RRF 前 5（pos 0..4）全部计分：c3 以 0.95 升到第 2 位；
    # c6/c7 在窗口外（由保底带入）→ None
    assert _ids(out) == ["c2", "c3", "c0", "c6", "c7"]
    assert [r["rerank_score"] for r in out] == [0.99, 0.95, 0.20, None, None]


def test_空候选池不调用打分(monkeypatch):
    """候选池为空时不触发任何推理（省掉一次无谓的模型调用）。"""
    fake = _FakeReranker(_RERANK)
    inst, _ = _build(monkeypatch, fake)

    assert inst._rerank_scores("q", {}, {}) == {}
    assert fake.seen_passages == []


# ---------------------------------------------------------------------------
# 降级：打分失败一律退回余弦排序（US-009 的前置契约）
# ---------------------------------------------------------------------------


def test_打分返回空列表_退回余弦排序(monkeypatch):
    """reranker 打不出分（空列表）→ 与关闭态逐位一致，且不留半截分数。"""
    inst, box = _build(monkeypatch, None)
    off = inst.search("q", top_k=5)

    box["value"] = _FakeReranker([])  # 真 reranker 的失败契约
    failed = inst.search("q", top_k=5)

    assert _ids(failed) == _ids(off)
    assert [r["rerank_score"] for r in failed] == [None] * 5


def test_分数长度不符_整体不启用(monkeypatch):
    """半截分数（长度不符）宁可整体不启用，也不要「部分分数 + 部分 0」的半开排序。"""
    inst, box = _build(monkeypatch, None)
    off = inst.search("q", top_k=5)

    box["value"] = _FakeReranker([0.9, 0.8])  # 8 个候选只回 2 个分
    partial = inst.search("q", top_k=5)

    assert _ids(partial) == _ids(off)
    assert [r["rerank_score"] for r in partial] == [None] * 5


# ---------------------------------------------------------------------------
# 纯函数守卫：本 story 只换「传进去的分数」，不动算法本身
# ---------------------------------------------------------------------------


def test_纯函数签名未被改动():
    """AC 明文要求 diversify_with_refill / dual_views 的签名与算法一行不改。"""
    assert list(inspect.signature(diversify_with_refill).parameters) == [
        "ranked_all",
        "v_scores",
        "doc_ids",
        "top_k",
        "min_sim",
        "anchor_quota",
    ]
    assert list(inspect.signature(dual_views).parameters) == [
        "ranked_all",
        "v_scores",
        "doc_ids",
        "top_k",
        "breadth_quota",
        "depth_quota",
    ]
