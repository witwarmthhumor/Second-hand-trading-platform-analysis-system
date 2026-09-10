"""多文档覆盖率诊断脚本的纯逻辑测试。

不依赖 Ollama / Chroma / LLM：用假检索器注入固定的 doc_id 结果，
验证 ``coverage_by_param._unique`` 去重保序，以及 ``measure`` 的分组统计口径
（多文档 coverage = 命中的期望文档数 / 期望文档数；单文档 coverage 退化为命中率）。
"""

import sys
from pathlib import Path

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "ragas_eval"
)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coverage_by_param as cbp  # noqa: E402


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
