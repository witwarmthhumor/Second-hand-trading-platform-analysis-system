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
    """假检索器：按问题文本返回预设的 doc_id 序列（模拟检索结果）。"""

    def __init__(self, mapping: dict[str, list[str]]):
        self._mapping = mapping

    def search(self, query: str, top_k: int) -> list[dict]:
        return [
            {"doc_id": d, "text": "", "rrf_score": 0.0, "vector_sim": 1.0}
            for d in self._mapping.get(query, [])[:top_k]
        ]


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
