"""多文档覆盖率诊断脚本的纯逻辑测试。

不依赖 Ollama / Chroma / LLM：用假检索器注入固定的 doc_id 结果，
验证 ``coverage_by_param._unique`` 去重保序，以及 ``measure`` 的分组统计口径
（多文档 coverage = 命中的期望文档数 / 期望文档数；单文档 coverage 退化为命中率）。

另验证 ``metrics.py``（被 run_eval.py 与 coverage_by_param.py 共用的口径实现）
与 ``run_eval.evaluate`` 把覆盖率作为一等指标输出——防止两处口径漂移，
以及防止 coverage 再次被从默认输出里漏掉（那曾导致「文档去重无效」的反向结论）。
"""

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "ragas_eval"
)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coverage_by_param as cbp  # noqa: E402
import metrics as M  # noqa: E402


class _StubRetriever:
    """假检索器：按问题文本返回预设结果（模拟检索结果）。

    ``mapping`` 的值元素既可以是 doc_id 字符串，也可以是 ``(doc_id, chunk_id)``
    元组——后者用于验证 chunk 级覆盖率。缺省 chunk_id 时用 ``""`` 占位
    （与生产旧缓存一致，应被视作未命中而不是崩）。
    """

    def __init__(self, mapping: dict[str, list]):
        self._mapping = mapping

    def search(self, query: str, top_k: int) -> list[dict]:
        out: list[dict] = []
        for item in self._mapping.get(query, [])[:top_k]:
            did, cid = item if isinstance(item, tuple) else (item, "")
            out.append(
                {
                    "chunk_id": cid,
                    "doc_id": did,
                    "text": "",
                    "rrf_score": 0.0,
                    "vector_sim": 1.0,
                }
            )
        return out


QUESTIONS = [
    {"id": "m1", "question": "多文档问", "expected_ids": ["A", "B", "C", "D"]},
    {"id": "m2", "question": "多文档问2", "expected_ids": ["E", "F"]},
    {"id": "s1", "question": "单文档问", "expected_ids": ["G"]},
    {"id": "s2", "question": "单文档问2", "expected_ids": ["H"]},
]


def test_unique_去重保序():
    assert cbp._unique(["A", "A", "B", "A", "C"]) == ["A", "B", "C"]
    assert cbp._unique([]) == []


def test_measure_分组统计口径():
    """多文档按覆盖率、单文档按命中率分别统计；同一文档多 chunk 只算一次。"""
    retriever = _StubRetriever(
        {
            "多文档问": ["A", "A", "B", "X", "Y"],  # 去重后 A/B/X/Y，命中 2/4
            "多文档问2": ["E"],  # 命中 1/2，top-5 只覆盖 1 个文档
            "单文档问": ["G"],  # 命中
            "单文档问2": ["Z"],  # 未命中
        }
    )

    m = cbp.measure(QUESTIONS, retriever, top_k=5)

    assert m["n"] == 4
    assert m["multi_n"] == 2
    assert m["single_n"] == 2
    # 多文档 coverage：(0.5 + 0.5) / 2
    assert m["multi_coverage"] == 0.5
    # 多文档 distinct_docs@5：4 与 1 的均值
    assert m["multi_distinct_docs"] == 2.5
    # 单文档 coverage：(1.0 + 0.0) / 2
    assert m["single_coverage"] == 0.5
    # 4 问里 3 问至少命中一篇
    assert m["hit_any"] == 0.75
    assert len(m["per_q"]) == 4
    assert m["per_q"][0]["ranked_ids"] == ["A", "B", "X", "Y"]


def test_measure_空期望文档不崩溃():
    """expected_ids 为空时按 0 覆盖率处理，不抛 ZeroDivisionError。"""
    m = cbp.measure(
        [{"id": "x", "question": "无标注", "expected_ids": []}],
        _StubRetriever({"无标注": ["A"]}),
        top_k=5,
    )
    assert m["multi_coverage"] == 0.0
    assert m["single_coverage"] == 0.0
    assert m["hit_any"] == 0.0


# ---------------------------------------------------------------------------
# metrics.py：run_eval 与 coverage_by_param 共用的口径实现
# ---------------------------------------------------------------------------


def test_metrics_coverage_at_k_口径():
    """coverage = 命中期望文档数 / 期望文档总数；distinct 为 k 内去重条数。"""
    r = M.coverage_at_k(["A", "A", "B", "X", "Y", "Z"], ["A", "B", "C"], 5)
    assert r["coverage"] == pytest.approx(2 / 3)
    assert r["distinct_docs"] == 4  # 先截前 5 个（A/A/B/X/Y）再去重 -> A/B/X/Y
    assert r["hit_any"] == 1
    assert r["is_multi"] is True


def test_metrics_coverage_at_k_先截断再去重():
    """必须先截断 top-k 再去重，否则「去重后取前 k 个文档」会人为放大 distinct。

    回归守卫：该顺序曾写反，导致 run_eval（top_k=20）报出 coverage@5=0.4701，
    而生产口径（top_k=5）只有 0.3755 —— 指标凭空虚高 9.5pp。
    """
    ranked = ["A", "A", "A", "A", "A", "B", "B", "C", "D", "E"]
    assert M.coverage_at_k(ranked, ["A", "B"], 5)["distinct_docs"] == 1  # 前 5 全是 A
    assert M.coverage_at_k(ranked, ["A", "B"], 5)["coverage"] == pytest.approx(0.5)
    assert M.coverage_at_k(ranked, ["A", "B"], 10)["distinct_docs"] == 5


def test_metrics_coverage_at_k_按k截断():
    """k 之外的名额不算覆盖（生产 top_k=5 与实验 top_k=20 口径可区分）。"""
    assert M.coverage_at_k(["A", "B", "C"], ["C"], 2)["coverage"] == 0.0
    assert M.coverage_at_k(["A", "B", "C"], ["C"], 3)["coverage"] == 1.0


def test_metrics_coverage_at_k_空期望不崩溃():
    r = M.coverage_at_k(["A"], [], 5)
    assert r["coverage"] == 0.0
    assert r["hit_any"] == 0
    assert r["is_multi"] is False


def test_metrics_aggregate_多单分组():
    """多文档与单文档必须分开聚合——合在一起会互相掩盖。"""
    rows = [
        M.coverage_at_k(["A", "B"], ["A", "B"], 5),  # 多文档 1.0, distinct 2
        M.coverage_at_k(["C"], ["C", "D"], 5),  # 多文档 0.5, distinct 1
        M.coverage_at_k(["E"], ["E"], 5),  # 单文档 1.0
        M.coverage_at_k([], ["F"], 5),  # 单文档 0.0
    ]
    a = M.aggregate(rows)
    assert a["multi_n"] == 2
    assert a["single_n"] == 2
    assert a["multi_coverage"] == pytest.approx(0.75)
    assert a["single_coverage"] == pytest.approx(0.5)
    assert a["multi_distinct_docs"] == pytest.approx(1.5)
    assert a["hit_any"] == pytest.approx(0.75)


def test_run_eval_输出覆盖率一等指标():
    """run_eval.evaluate 必须输出 coverage（与 Hit@5 并列），且逐问可下钻。"""
    pytest.importorskip("ragas")
    import run_eval

    rtv = _StubRetriever(
        {
            "多文档问": ["A", "A", "B", "X", "Y"],
            "多文档问2": ["E"],
            "单文档问": ["G"],
            "单文档问2": ["Z"],
        }
    )
    rtv.is_ready = True  # evaluate 会检查就绪标志

    res = asyncio.run(run_eval.evaluate(QUESTIONS, top_k=5, retriever=rtv))

    assert res["coverage"]["production_k"] == 5
    at5 = res["coverage"]["at5"]
    # 与 test_measure_分组统计口径 同源，数字必须一致（防两处实现漂移）
    assert at5["multi_coverage"] == pytest.approx(0.5)
    assert at5["single_coverage"] == pytest.approx(0.5)
    assert at5["hit_any"] == pytest.approx(0.75)
    assert at5["multi_distinct_docs"] == pytest.approx(2.5)
    assert res["per_q"][0]["coverage_at5"] == pytest.approx(0.5)
    assert res["per_q"][0]["distinct_docs_at5"] == 4


# ---------------------------------------------------------------------------
# chunk 级覆盖率：把分母从「文档」细化到「块」
# ---------------------------------------------------------------------------


def test_chunk_coverage_基本口径():
    """recall=命中块/期望块；recall_norm=命中块/min(k,期望块)；precision=命中块/k。"""
    r = M.chunk_coverage_at_k(["c1", "c2", "c9"], ["c1", "c2", "c3", "c4"], 5)
    assert r["recall"] == pytest.approx(2 / 4)
    assert r["recall_norm"] == pytest.approx(2 / 4)  # min(5, 4) = 4
    assert r["precision"] == pytest.approx(2 / 5)
    assert r["coverable"] == pytest.approx(1.0)  # 名额够，理论上界 1.0
    assert r["n_expected"] == 4
    assert r["hit1"] == 1


def test_chunk_coverage_先截断再计数():
    """必须先在候选列表上截断 top-k 再求交，否则长候选列表会凭空虚高命中。

    回归守卫：doc 级 coverage 曾因「先去重再取前 k」虚高 9.5pp
    （0.3755 → 0.4701），chunk 级沿用同一纪律。
    """
    ranked = ["c1", "c2", "c3", "c4", "c5", "c6"]
    assert M.chunk_coverage_at_k(ranked, ["c6"], 5)["recall"] == 0.0
    assert M.chunk_coverage_at_k(ranked, ["c6"], 6)["recall"] == 1.0


def test_chunk_coverage_规模干扰与名额内归一():
    """文档越大 recall 天花板越低；recall_norm 把上界拉回 1.0，可跨规模比较。

    recall_norm = 命中块 / min(k, 期望块数)，上界恒为 1.0（= 把可用名额全用在
    目标文档上）。小文档名额足够（2 块都用得上）却只中 1 块 → 0.5，说明**还有
    本可召回的内容没召回**；大文档名额只有 5 个、中了 1 块 → 0.2，是**名额不够**。
    两者原因不同，但都被同一个 0~1 标尺量出来。
    """
    small = M.chunk_coverage_at_k(["c1"], ["c1", "c2"], 5)  # 2 块的文档，中 1 块
    big = M.chunk_coverage_at_k(["c1"], [f"c{i}" for i in range(1, 21)], 5)  # 20 块
    assert small["recall"] == pytest.approx(0.5)
    assert big["recall"] == pytest.approx(0.05)
    assert small["recall_norm"] == pytest.approx(0.5)  # min(5,2)=2
    assert big["recall_norm"] == pytest.approx(1 / 5)  # min(5,20)=5
    assert small["coverable"] == pytest.approx(1.0)
    assert big["coverable"] == pytest.approx(0.25)


def test_chunk_coverage_空期望不崩溃():
    r = M.chunk_coverage_at_k(["c1"], [], 5)
    assert r["recall"] == 0.0
    assert r["recall_norm"] == 0.0
    assert r["precision"] == 0.0
    assert r["hit1"] == 0


def test_chunk_hit1_是doc级命中的下界():
    """top-1 落在期望块内 ⟹ 该文档必在 top-k 中（doc 级 hit_any 必为 1）。

    这是 doc 级与 chunk 级两级口径能互相对拍的依据（交叉校验）。
    """
    cases = [
        (["c1", "c2"], ["c1"]),  # top1 命中
        (["c9", "c1"], ["c1"]),  # top1 未命中、后续命中
        (["c9", "c8"], ["c1"]),  # 全未命中
    ]
    for ranked, expected in cases:
        c = M.chunk_coverage_at_k(ranked, expected, 5)
        d = M.coverage_at_k(ranked, expected, 5)
        assert c["hit1"] <= d["hit_any"]


def test_aggregate_chunks_分组与分箱():
    """多/单文档分组 + 期望块数分箱（小≤6 / 中7~12 / 大>12）都必须可用。"""
    rows = [
        {**M.chunk_coverage_at_k(["c1"], ["c1", "c2"], 5), "is_multi": False},
        {**M.chunk_coverage_at_k(["c9"], ["c1", "c2"], 5), "is_multi": True},
        {
            **M.chunk_coverage_at_k(
                [f"c{i}" for i in range(1, 6)], [f"c{i}" for i in range(1, 10)], 5
            ),
            "is_multi": True,
        },
    ]
    a = M.aggregate_chunks(rows)
    assert a["overall"]["n"] == 3
    assert a["single"]["n"] == 1
    assert a["multi"]["n"] == 2
    assert a["by_size"]["小(≤6块)"]["n"] == 2
    assert a["by_size"]["中(7~12块)"]["n"] == 1
    assert a["by_size"]["大(>12块)"]["n"] == 0


def test_measure_桩对象缺chunk_id视作未命中():
    """没有 chunk_id（旧缓存 / 桩对象）时应记 0 而不是崩——与 doc 级结果对照。"""
    m = cbp.measure(
        [{"id": "x", "question": "q", "expected_ids": ["A"]}],
        _StubRetriever({"q": ["A"]}),
        top_k=5,
    )
    assert m["single_coverage"] == pytest.approx(1.0)  # doc 级命中
    assert m["chunks"]["overall"]["recall"] == 0.0  # chunk 级因缺 id 未命中
    assert m["chunks"]["overall"]["hit1"] == 0.0


def test_doc_chunk_ids_与bm25索引逐位对拍():
    """``common.doc_chunk_ids()`` 展平后必须与 bm25_index.json 的 doc_ids 逐位一致。

    这是 chunk 级覆盖率成立的前提：期望块 id 必须与检索返回的 chunk_id
    **同源同序**，否则指标比的是两套编号，数字没有意义。
    """
    import json

    idx = Path(__file__).resolve().parents[2] / "rag_data/vector_store/bm25_index.json"
    if not idx.exists():
        pytest.skip("向量库索引不存在（需先 build_vector_store）")

    import common as C

    mapping = C.doc_chunk_ids()
    pos: dict[int, str] = {}
    for doc, ids in mapping.items():
        for cid in ids:
            pos[int(cid[1:])] = doc
    bm = json.loads(idx.read_text(encoding="utf-8"))["doc_ids"]
    assert len(pos) == len(bm), "chunk 总数与 bm25 索引不一致（语料/索引不同步）"
    assert all(pos.get(i) == bm[i] for i in range(len(bm))), "doc_id 逐位不一致"


def test_run_eval_输出chunk级覆盖率():
    """run_eval 必须把 chunk 级覆盖率与 doc 级并列输出，且逐问可下钻。"""
    pytest.importorskip("ragas")
    import common as C
    import run_eval

    mapping = C.doc_chunk_ids()
    if not mapping:
        pytest.skip("语料未就绪")
    doc = next(iter(mapping))
    chunks = mapping[doc]

    rtv = _StubRetriever({"真实问题": [(doc, c) for c in chunks[:5]]})
    rtv.is_ready = True
    res = asyncio.run(
        run_eval.evaluate(
            [{"id": "r1", "question": "真实问题", "expected_ids": [doc]}],
            top_k=5,
            retriever=rtv,
        )
    )

    o = res["chunk_coverage"]["at5"]["overall"]
    assert o["n"] == 1
    assert o["recall"] == pytest.approx(min(5, len(chunks)) / len(chunks))
    assert o["hit1"] == 1  # top-1 就是期望文档的块
    assert res["coverage"]["at5"]["hit_any"] == 1.0  # doc 级同时命中
    assert res["per_q"][0]["chunk_hit1"] == 1
