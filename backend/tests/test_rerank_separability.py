"""``scripts/experiments/ragas_eval/rerank_separability.py`` 的离线测试（US-003）。

该脚本是**上线前置闸门**：它判「通过 / 否决」直接决定后面 6 个 story 要不要做。
所以这里不跑真模型、不碰向量库（各自有手工冒烟），只守住闸门自身最怕出错的三件事：

1. **判据常量**必须真的源自 P0-2 的 0.036（×3），不能被随手调松——闸门判错一次
   就是「先实现再验证」的旧账重演；
2. **两条判据各自的失效形态**都能判「否决」：中位差不够（余弦那种窄分布）、
   多档阈值扫描逐位相同（P0-2 的「判据没触发」）；
3. **候选池与打分手势**与 US-005 的计划接入口径一致：只对 RRF 前 20 打 rerank 分、
   绝不扩大召回；打不出分时**显式失败**而不是把空列表当真分数用。

脚本不在 backend 包内，按 ``test_download_reranker.py`` 的做法用
``importlib.util.spec_from_file_location`` 挂载后导入。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "experiments"
    / "ragas_eval"
    / "rerank_separability.py"
)
_spec = importlib.util.spec_from_file_location("rerank_separability", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
separability = importlib.util.module_from_spec(_spec)
sys.modules["rerank_separability"] = separability
_spec.loader.exec_module(separability)


# ---------------------------------------------------------------------------
# 假对象（不加载 570MB 模型、不打开 Chroma）
# ---------------------------------------------------------------------------


class _FakeReranker:
    def __init__(self, scores: list[float], available: bool = True, load_error=None):
        self._scores = scores
        self.available = available
        self.load_error = load_error
        self.seen_passages: list[list[str]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        # 真 reranker 的契约是「与入参等长」（打不出分时返回空列表）
        self.seen_passages.append(list(passages))
        return list(self._scores[: len(passages)])


class _FakeRetriever:
    def __init__(self, rrf, v_scores, v_texts, doc_ids):
        self._rrf = rrf
        self._v_scores = v_scores
        self._v_texts = v_texts
        self._doc_ids = doc_ids
        self.is_ready = True

    def _hybrid_candidates(self, query: str):
        return self._rrf, self._v_scores, self._v_texts


def _records(expected_base: float, noise_base: float, n: int = 100):
    """造 [(rerank, 余弦, 是否期望块)]，两组分数分别落在两个区间。"""
    out = []
    for i in range(n):
        out.append((expected_base + i * 0.0001, 0.90 + i * 0.0001, True))
        out.append((noise_base + i * 0.0001, 0.88 + i * 0.0001, False))
    return out


# ---------------------------------------------------------------------------
# 判据常量：必须锚在 P0-2 的 0.036 上
# ---------------------------------------------------------------------------


def test_gate_threshold_is_three_times_p02_gap():
    """门槛 = P0-2 的 0.036 × 3，且严格大于 P0-2（否则等于没改）。"""
    assert separability.P02_MEDIAN_GAP == 0.036
    assert separability.SIGNIFICANCE_MULTIPLIER > 1
    assert separability.MIN_MEDIAN_GAP == 0.108
    assert separability.MIN_MEDIAN_GAP > separability.P02_MEDIAN_GAP


def test_top_n_matches_planned_integration():
    """只重排候选池、不扩大召回：打分窗口 = 生产的 QUERY_N_RESULTS。"""
    from app.rag.config import QUERY_N_RESULTS

    assert separability.RERANKER_TOP_N == QUERY_N_RESULTS == 20


def test_script_has_no_llm_or_network_dependency():
    """闸门要求「无网络、无真实 LLM」也能跑：源码里不该出现 LLM 调用/HTTP 客户端。"""
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("httpx", "rag_service", "llm_fallback", "requests."):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# 纯函数：统计量 / AUC / 重叠区 / 扫描
# ---------------------------------------------------------------------------


def test_distribution_reports_median_and_quantiles():
    stats = separability.distribution([0.0, 0.5, 1.0, 2.0, 3.0])

    assert stats["n"] == 5
    assert stats["median"] == 1.0
    assert stats["p5"] == 0.0
    assert stats["p95"] == 3.0


def test_distribution_of_empty_list_is_zeroed():
    assert separability.distribution([]) == {
        "n": 0,
        "mean": 0.0,
        "median": 0.0,
        "p5": 0.0,
        "p95": 0.0,
    }


def test_overlap_region_detects_disjoint_core_intervals():
    expected = {"p5": 0.90, "p95": 0.99}
    noise = {"p5": 0.01, "p95": 0.05}

    region = separability.overlap_region(expected, noise)

    assert region["width"] == 0.0
    assert region["overlapped"] is False


def test_overlap_region_detects_intersecting_core_intervals():
    """余弦那种窄分布：核心区间大面积相交。"""
    region = separability.overlap_region({"p5": 0.80, "p95": 0.95}, {"p5": 0.78, "p95": 0.93})

    assert region["overlapped"] is True
    assert region["width"] > 0.0


def test_auc_perfect_separation_is_one_and_random_is_half():
    assert separability.auc([0.9, 0.95, 0.99], [0.01, 0.02, 0.03]) == 1.0
    # 完全无法区分（含并列）→ 0.5
    assert separability.auc([0.5, 0.5], [0.5, 0.5]) == 0.5


def test_threshold_sweep_uses_score_quantiles_and_moves():
    """扫描阈值取实测分位数：分数跨区间分布时，各档结果**不得**逐位相同。"""
    records = _records(expected_base=0.9, noise_base=0.05)

    thresholds, rows = separability.threshold_sweep(records)

    assert len(thresholds) == 5
    assert thresholds == sorted(thresholds)
    assert separability.sweep_is_informative(rows) is True
    # 期望块的 Youden J 必须显著为正（分数真的分得开）
    assert max(r["youden_j"] for r in rows) > 0.8


def test_sweep_of_constant_scores_is_not_informative():
    """分数退化（全部同分）→ 分位数塌成一档 → P0-2 式「判据没触发」→ 否决。"""
    records = [(0.5, 0.9, True)] * 50 + [(0.5, 0.9, False)] * 50

    thresholds, rows = separability.threshold_sweep(records)

    assert thresholds == [0.5]
    assert separability.sweep_is_informative(rows) is False


# ---------------------------------------------------------------------------
# 两条判据 AND 出结论
# ---------------------------------------------------------------------------


def test_separated_groups_pass_the_gate():
    records = _records(expected_base=0.9, noise_base=0.05)
    rerank_stats, cosine_stats = separability._group_stats(records)
    _, sweep_rows = separability.threshold_sweep(records)

    verdict = separability.build_verdict(rerank_stats, cosine_stats, sweep_rows)

    assert verdict["median_gap_ok"] is True
    assert verdict["sweep_ok"] is True
    assert verdict["passed"] is True
    assert verdict["conclusion"] == "通过"
    assert "US-004" in verdict["next_action"]
    # 并排对照：余弦的中位差明显小于 rerank（0.036 量级的窄分布）
    assert verdict["cosine_median_gap"] < verdict["min_median_gap"]


def test_narrow_cosine_like_distribution_is_rejected_by_median_gap():
    """复刻 P0-2：两组中位数只差 0.02（< 0.108）→ 否决。"""
    records = [(0.90 + i * 0.0001, 0.9, True) for i in range(100)]
    records += [(0.88 + i * 0.0001, 0.9, False) for i in range(100)]
    rerank_stats, cosine_stats = separability._group_stats(records)
    _, sweep_rows = separability.threshold_sweep(records)

    verdict = separability.build_verdict(rerank_stats, cosine_stats, sweep_rows)

    assert verdict["median_gap_ok"] is False
    assert verdict["passed"] is False
    assert verdict["conclusion"] == "否决"
    assert "0.108" in verdict["next_action"] or "中位差" in verdict["next_action"]


def test_degenerate_scores_are_rejected_by_sweep_gate():
    records = [(0.5, 0.9, True)] * 50 + [(0.5, 0.9, False)] * 50
    rerank_stats, cosine_stats = separability._group_stats(records)
    _, sweep_rows = separability.threshold_sweep(records)

    verdict = separability.build_verdict(rerank_stats, cosine_stats, sweep_rows)

    assert verdict["sweep_ok"] is False
    assert verdict["passed"] is False
    assert verdict["conclusion"] == "否决"
    assert "逐位相同" in verdict["next_action"]


# ---------------------------------------------------------------------------
# 采集：候选池截断 + 分组标签 + 打不出分就失败
# ---------------------------------------------------------------------------


def test_collect_records_truncates_to_rrf_top_n_and_labels_groups():
    retriever = _FakeRetriever(
        rrf={0: 0.03, 1: 0.02, 2: 0.01},
        v_scores={0: 0.70, 1: 0.80, 2: 0.60},
        v_texts={0: "t0", 1: "t1", 2: "t2"},
        doc_ids=["DOC-1", "DOC-2", "DOC-3"],
    )
    reranker = _FakeReranker(scores=[0.9, 0.1, 0.2])
    questions = [{"id": "Q1", "question": "q", "expected_ids": ["DOC-1"]}]

    records, per_q = separability.collect_records(retriever, reranker, questions, top_n=2)

    # 只对 RRF 前 2 个打分（c2 被截断，不参与）
    assert reranker.seen_passages == [["t0", "t1"]]
    assert records == [(0.9, 0.70, True), (0.1, 0.80, False)]
    assert per_q == [{"id": "Q1", "candidates": 2, "expected_chunks": 1}]


def test_collect_records_raises_when_reranker_returns_nothing():
    """降级契约：打不出分（空列表）必须显式失败，不能把空分数当真分数用。"""
    retriever = _FakeRetriever(
        rrf={0: 0.03, 1: 0.02},
        v_scores={0: 0.7, 1: 0.8},
        v_texts={0: "t0", 1: "t1"},
        doc_ids=["DOC-1", "DOC-2"],
    )
    reranker = _FakeReranker(scores=[], available=False, load_error="模型文件缺失: x.onnx")

    questions = [{"id": "Q1", "question": "q", "expected_ids": ["DOC-1"]}]

    try:
        separability.collect_records(retriever, reranker, questions, top_n=2)
    except RuntimeError as exc:
        assert "模型文件缺失" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("rerank 未打分时应抛 RuntimeError")


# ---------------------------------------------------------------------------
# main：模型缺失可读失败 / 报告带结论与后续动作
# ---------------------------------------------------------------------------


def test_main_reports_missing_model_readably(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["rerank_separability.py"])
    monkeypatch.setattr(
        separability,
        "get_reranker",
        lambda: _FakeReranker([], available=False, load_error="模型文件缺失: model_int8.onnx"),
    )

    assert separability.main() == 1
    err = capsys.readouterr().err
    assert "模型文件缺失" in err
    assert "download_reranker.py" in err


def test_main_writes_report_with_verdict_and_next_action(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["rerank_separability.py"])
    retriever = _FakeRetriever(
        rrf={0: 0.03, 1: 0.02},
        v_scores={0: 0.70, 1: 0.60},
        v_texts={0: "t0", 1: "t1"},
        doc_ids=["DOC-1", "DOC-2"],
    )
    # 期望块（c0）高分、非期望块（c1）低分 → 闸门通过
    reranker = _FakeReranker(scores=[0.95, 0.01])
    questions = [
        {"id": f"Q{i}", "question": f"q{i}", "expected_ids": ["DOC-1"]}
        for i in range(6)
    ]

    monkeypatch.setattr(separability, "get_reranker", lambda: reranker)
    monkeypatch.setattr(separability, "get_retriever", lambda: retriever)
    monkeypatch.setattr(separability, "load_golden", lambda: questions)
    monkeypatch.setattr(separability, "RESULTS_DIR", tmp_path)

    assert separability.main() == 0

    report = json.loads((tmp_path / "rerank_separability.json").read_text(encoding="utf-8"))
    assert report["story"] == "US-003"
    assert report["n_questions"] == 6
    assert report["reranker_top_n"] == 20
    # 并排对照：rerank 与余弦两份分布都要有
    assert set(report["rerank"]) == set(report["cosine"])
    assert report["rerank"]["auc"] == 1.0
    verdict = report["verdict"]
    assert verdict["conclusion"] in ("通过", "否决")
    assert verdict["next_action"]
    assert len(report["threshold_sweep"]) >= 2
