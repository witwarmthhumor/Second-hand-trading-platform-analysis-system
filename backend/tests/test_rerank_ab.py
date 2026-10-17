"""``scripts/experiments/ragas_eval/rerank_ab.py`` 的离线测试（US-008）。

对拍报告是**上线判定**的产物：它说「达到验收标准」就意味着可以改默认值上线。
所以这里不跑真检索、不加载 570MB 模型（真跑一次约 1 小时，见目录 CLAUDE.md），
只守住判定逻辑最怕出错的几件事：

1. **任何一项效果指标劣化都要判不达标**（PRD §8 的「不劣化」是硬要求）；
2. **Hit@5 必须保持 1.0000**，掉了就是回归；
3. **候选池必须逐问一致**（FR-2），池子不同说明两次跑的不是同一个检索面，
   对比本身就失去意义——这条要在报告里显式失败，而不是悄悄照常出表；
4. **延迟增量 > 300ms 要判不达标**（这是本次实测最可能踩线的一项）；
5. 主入口**拒绝误用**：两份同态、或非线上口径 top_k=5 的结果，不能当对拍报告。

另测 ``run_eval.py`` 的 ``--capture-pool`` 留痕：它记录的是候选池本身，
必须「不额外调用 embedding」（否则会把 285 问的评测耗时翻倍），且用完还原实例方法。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

RAGAS_EVAL_DIR = (
    Path(__file__).resolve().parents[2] / "scripts" / "experiments" / "ragas_eval"
)
if str(RAGAS_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(RAGAS_EVAL_DIR))

_SCRIPT_PATH = RAGAS_EVAL_DIR / "rerank_ab.py"
_spec = importlib.util.spec_from_file_location("rerank_ab", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
rerank_ab = importlib.util.module_from_spec(_spec)
sys.modules["rerank_ab"] = rerank_ab
_spec.loader.exec_module(rerank_ab)


# ---------------------------------------------------------------------------
# 造假结果（形状与 run_eval.evaluate 的返回值逐字段一致）
# ---------------------------------------------------------------------------


def _per_q(qid: str, elapsed_s: float, pool: list[str], result: list[str]) -> dict:
    return {
        "id": qid,
        "question": qid,
        "elapsed_s": elapsed_s,
        "candidate_ids": pool,
        "result_chunk_ids": result,
    }


def _res(
    *,
    reranker_enabled: bool,
    hit5: float = 1.0,
    mrr10: float = 0.8,
    ndcg10: float = 0.7,
    multi_cov: float = 0.5,
    single_cov: float = 1.0,
    chunk_recall: float = 0.4,
    per_q: list[dict],
    top_k: int = 5,
) -> dict:
    return {
        "n": len(per_q),
        "top_k": top_k,
        "strategy": f"rerank_{'on' if reranker_enabled else 'off'}",
        "reranker_enabled": reranker_enabled,
        "hit5": hit5,
        "mrr10": mrr10,
        "ndcg10": ndcg10,
        "coverage": {
            "at5": {"multi_coverage": multi_cov, "single_coverage": single_cov}
        },
        "chunk_coverage": {
            "at5": {
                "overall": {
                    "recall": chunk_recall,
                    "recall_norm": chunk_recall,
                    "precision": chunk_recall,
                    "hit1": 1.0,
                }
            }
        },
        "per_q": per_q,
    }


_POOL = ["c1", "c2", "c3", "c4"]


def _pair(**over) -> tuple[dict, dict]:
    """一对同池、不劣化的两态结果；用 over 覆盖 on 态指标以制造劣化。

    ``multi_cov`` / ``single_cov`` / ``chunk_recall`` 落在嵌套结构里，
    单独处理（直接 ``on[key] = v`` 会写成一个没人读的顶层键）。
    """
    off = _res(
        reranker_enabled=False,
        per_q=[_per_q("q1", 1.0, _POOL, ["c1", "c2"]), _per_q("q2", 1.0, _POOL, ["c3"])],
    )
    on = _res(
        reranker_enabled=True,
        per_q=[_per_q("q1", 1.05, _POOL, ["c2", "c1"]), _per_q("q2", 1.05, _POOL, ["c3"])],
    )
    for key, value in over.items():
        if key == "multi_cov":
            on["coverage"]["at5"]["multi_coverage"] = value
        elif key == "single_cov":
            on["coverage"]["at5"]["single_coverage"] = value
        elif key == "chunk_recall":
            on["chunk_coverage"]["at5"]["overall"]["recall"] = value
        else:
            on[key] = value
    return off, on


# ---------------------------------------------------------------------------
# 效果指标：任何一项劣化都要判不达标
# ---------------------------------------------------------------------------


def test_不劣化且同池且延迟达标时判达到验收标准():
    off, on = _pair()
    v = rerank_ab.build_verdict(off, on)
    assert v["passed"] is True
    assert v["verdict"] == "达到验收标准"
    assert v["failed_criteria"] == []
    assert v["pool"]["same"] is True
    assert v["latency"]["passed"] is True


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("mrr10", "MRR@10"),
        ("ndcg10", "nDCG@10"),
        ("hit5", "Hit@5"),
    ],
)
def test_顶层指标劣化即判不达标(field, label):
    off, on = _pair(**{field: 0.5})
    v = rerank_ab.build_verdict(off, on)
    assert v["passed"] is False
    assert label in v["failed_criteria"]


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("multi_cov", "多文档 coverage@5"),
        ("single_cov", "单文档 coverage@5"),
        ("chunk_recall", "chunk 级 coverage@5（recall）"),
    ],
)
def test_覆盖率指标劣化即判不达标(field, label):
    off, on = _pair(**{field: 0.1})
    v = rerank_ab.build_verdict(off, on)
    assert v["passed"] is False
    assert label in v["failed_criteria"]


def test_持平不算劣化也不算提升():
    off, on = _pair()
    rows = {r["key"]: r for r in rerank_ab.metric_rows(off, on)}
    assert rows["mrr10"]["passed"] is True
    assert rows["mrr10"]["improved"] is False


def test_提升记录在improved_main_metrics里():
    off, on = _pair()
    on["mrr10"] = 0.9
    v = rerank_ab.build_verdict(off, on)
    assert v["passed"] is True
    assert "MRR@10" in v["improved_main_metrics"]
    assert "nDCG@10" not in v["improved_main_metrics"]


# ---------------------------------------------------------------------------
# 候选池一致性（FR-2）
# ---------------------------------------------------------------------------


def test_候选池逐问不一致判不达标():
    off, on = _pair()
    on["per_q"][0]["candidate_ids"] = ["c1", "c2", "c9"]
    v = rerank_ab.build_verdict(off, on)
    assert v["pool"]["same"] is False
    assert v["pool"]["mismatched"] == ["q1"]
    assert v["passed"] is False
    assert "两组候选池一致（FR-2）" in v["failed_criteria"]


def test_返回块落在池外判不达标():
    """排序若引入了候选池外的新块，说明「只改排序不改成员」被破坏。"""
    off, on = _pair()
    on["per_q"][0]["result_chunk_ids"] = ["c1", "c999"]
    v = rerank_ab.build_verdict(off, on)
    assert v["pool"]["same"] is False
    assert v["pool"]["not_in_pool"] == ["q1"]


def test_池比较按集合而非顺序():
    """rerank 的作用就是改顺序，池成员顺序变化不算不一致。"""
    off, on = _pair()
    on["per_q"][0]["candidate_ids"] = list(reversed(_POOL))
    assert rerank_ab.pool_check(off, on)["same"] is True


# ---------------------------------------------------------------------------
# 延迟
# ---------------------------------------------------------------------------


def test_延迟增量按问题配对且中位数反映稳态():
    """首问的模型加载会抬高平均值；中位数不该被它带偏。"""
    off = _res(
        reranker_enabled=False,
        per_q=[_per_q("q1", 1.0, _POOL, ["c1"])] + [
            _per_q(f"q{i}", 1.0, _POOL, ["c1"]) for i in range(2, 12)
        ],
    )
    on = _res(
        reranker_enabled=True,
        per_q=[_per_q("q1", 5.0, _POOL, ["c1"])] + [
            _per_q(f"q{i}", 1.1, _POOL, ["c1"]) for i in range(2, 12)
        ],
    )
    lat = rerank_ab.latency_stats(off, on)
    assert lat["n"] == 11
    assert lat["delta_median_ms"] == pytest.approx(100.0, abs=1.0)
    assert lat["delta_avg_ms"] > lat["delta_median_ms"]  # 首问被摊进均值


def test_延迟增量超300ms判不达标():
    off = _res(
        reranker_enabled=False,
        per_q=[_per_q(f"q{i}", 0.5, _POOL, ["c1"]) for i in range(5)],
    )
    on = _res(
        reranker_enabled=True,
        per_q=[_per_q(f"q{i}", 0.5 + 0.301, _POOL, ["c1"]) for i in range(5)],
    )
    v = rerank_ab.build_verdict(off, on)
    assert v["latency"]["passed"] is False
    assert "平均延迟增量 ≤300ms" in v["failed_criteria"]


def test_延迟增量恰好300ms算达标():
    off = _res(
        reranker_enabled=False,
        per_q=[_per_q(f"q{i}", 0.5, _POOL, ["c1"]) for i in range(5)],
    )
    on = _res(
        reranker_enabled=True,
        per_q=[_per_q(f"q{i}", 0.8, _POOL, ["c1"]) for i in range(5)],
    )
    assert rerank_ab.build_verdict(off, on)["latency"]["passed"] is True


def test_分位数_单元素与端点():
    assert rerank_ab._percentile([], 0.95) == 0.0
    assert rerank_ab._percentile([7.0], 0.95) == 7.0
    assert rerank_ab._percentile([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.0)
    assert rerank_ab._percentile([1.0, 2.0, 3.0, 4.0], 1.0) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# 呈现
# ---------------------------------------------------------------------------


def test_markdown含七项指标与明确结论():
    off, on = _pair()
    md = rerank_ab.render_markdown(rerank_ab.build_verdict(off, on), off, on)
    for label in (
        "Hit@5",
        "MRR@10",
        "nDCG@10",
        "多文档 coverage@5",
        "单文档 coverage@5",
        "chunk 级 coverage@5（recall）",
    ):
        assert label in md
    assert "**达到验收标准**" in md
    assert "ms/问" in md
    assert "候选池" in md


def test_markdown未达标时列出失败判据():
    off, on = _pair(mrr10=0.1)
    md = rerank_ab.render_markdown(rerank_ab.build_verdict(off, on), off, on)
    assert "**未达到验收标准**" in md
    assert "- MRR@10" in md


def test_markdown达标但无提升时提示人工评审():
    off, on = _pair()
    md = rerank_ab.render_markdown(rerank_ab.build_verdict(off, on), off, on)
    assert "人工评审" in md


# ---------------------------------------------------------------------------
# 主入口的误用防护
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: dict) -> None:
    (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_主入口拒绝两份同态(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rerank_ab, "RESULTS_DIR", tmp_path)
    off, on = _pair()
    on["reranker_enabled"] = False  # 误把关闭态跑了两遍
    _write(tmp_path, "rerank_ab_off.json", off)
    _write(tmp_path, "rerank_ab_on.json", on)
    monkeypatch.setattr(
        sys, "argv", ["rerank_ab.py", "--off", "rerank_ab_off.json", "--on", "rerank_ab_on.json"]
    )
    with pytest.raises(SystemExit) as exc:
        rerank_ab.main()
    assert "reranker_enabled" in str(exc.value)


def test_主入口拒绝非线上topk(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank_ab, "RESULTS_DIR", tmp_path)
    off, on = _pair()
    on["top_k"] = 20
    _write(tmp_path, "rerank_ab_off.json", off)
    _write(tmp_path, "rerank_ab_on.json", on)
    monkeypatch.setattr(
        sys, "argv", ["rerank_ab.py", "--off", "rerank_ab_off.json", "--on", "rerank_ab_on.json"]
    )
    with pytest.raises(SystemExit) as exc:
        rerank_ab.main()
    assert "top_k" in str(exc.value)


def test_主入口拒绝问数不同的两组(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank_ab, "RESULTS_DIR", tmp_path)
    off, on = _pair()
    on["n"] = 1
    _write(tmp_path, "rerank_ab_off.json", off)
    _write(tmp_path, "rerank_ab_on.json", on)
    monkeypatch.setattr(
        sys, "argv", ["rerank_ab.py", "--off", "rerank_ab_off.json", "--on", "rerank_ab_on.json"]
    )
    with pytest.raises(SystemExit) as exc:
        rerank_ab.main()
    assert "问数不同" in str(exc.value)


def test_主入口落盘json与markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank_ab, "RESULTS_DIR", tmp_path)
    off, on = _pair()
    _write(tmp_path, "rerank_ab_off.json", off)
    _write(tmp_path, "rerank_ab_on.json", on)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rerank_ab.py", "--off", "rerank_ab_off.json", "--on", "rerank_ab_on.json"],
    )
    assert rerank_ab.main() == 0
    saved = json.loads((tmp_path / "rerank_ab.json").read_text(encoding="utf-8"))
    assert saved["passed"] is True
    report = (tmp_path / "rerank_ab_report.md").read_text(encoding="utf-8")
    assert "**达到验收标准**" in report


def test_缺少结果文件时给可读提示(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank_ab, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc:
        rerank_ab._load("nope.json")
    assert "缺少结果文件" in str(exc.value)


# ---------------------------------------------------------------------------
# run_eval 的候选池留痕（--capture-pool）
# ---------------------------------------------------------------------------


class _CaptureRetriever:
    """只有 `_hybrid_candidates` + `search` 的最小假检索器。

    `search` 从候选池里取前 top_k 个位置当结果，足以验证「返回块 ⊆ 候选池」。
    """

    def __init__(self, pools: dict[str, list[int]], doc_ids: list[str]):
        self._pools = pools
        self._doc_ids = doc_ids
        self.is_ready = True
        self.pool_calls = 0

    def _hybrid_candidates(self, query: str):
        self.pool_calls += 1
        rrf = {pos: 1.0 / (pos + 1) for pos in self._pools[query]}
        v_scores = {pos: 1.0 - pos / 100.0 for pos in rrf}
        v_texts = {pos: f"text{pos}" for pos in rrf}
        return rrf, v_scores, v_texts

    def search(self, query: str, top_k: int) -> list[dict]:
        rrf, v_scores, v_texts = self._hybrid_candidates(query)
        out = []
        for pos in list(rrf)[:top_k]:
            out.append(
                {
                    "chunk_id": f"c{pos}",
                    "doc_id": self._doc_ids[pos % len(self._doc_ids)],
                    "text": v_texts[pos],
                    "rrf_score": 0.0,
                    "vector_sim": v_scores[pos],
                    "rerank_score": None,
                }
            )
        return out


def _run_capture(questions, retriever, capture_pool):
    pytest.importorskip("ragas")
    import run_eval

    return asyncio.run(
        run_eval.evaluate(questions, top_k=5, retriever=retriever, capture_pool=capture_pool)
    )


_QUESTIONS = [
    {"id": "q1", "question": "问题一", "expected_ids": ["A"]},
    {"id": "q2", "question": "问题二", "expected_ids": ["A"]},
]


def test_capture_pool记录候选池且不额外调用检索():
    """留痕必须复用 search 内部那一次 `_hybrid_candidates`，不能重算（否则耗时翻倍）。"""
    rtv = _CaptureRetriever({"问题一": [1, 2, 3], "问题二": [4, 5]}, ["A", "B"])
    res = _run_capture(_QUESTIONS, rtv, capture_pool=True)

    assert res["per_q"][0]["candidate_ids"] == ["c1", "c2", "c3"]
    assert res["per_q"][1]["candidate_ids"] == ["c4", "c5"]
    # 每题只算一次池子（search 内部那一次）
    assert rtv.pool_calls == len(_QUESTIONS)


def test_capture_pool记录返回块且必为候选池子集():
    rtv = _CaptureRetriever({"问题一": [1, 2, 3], "问题二": [4, 5]}, ["A", "B"])
    res = _run_capture(_QUESTIONS, rtv, capture_pool=True)
    for q in res["per_q"]:
        assert q["result_chunk_ids"]
        assert set(q["result_chunk_ids"]) <= set(q["candidate_ids"])


def test_capture_pool用完还原实例方法():
    """同一进程里连着跑两组（rerank_ab 的用法）不能把包装留着。"""
    rtv = _CaptureRetriever({"问题一": [1], "问题二": [2]}, ["A"])
    original = rtv._hybrid_candidates
    _run_capture(_QUESTIONS, rtv, capture_pool=True)
    assert rtv._hybrid_candidates == original


def test_默认不记录候选池保持输出形状不变():
    rtv = _CaptureRetriever({"问题一": [1], "问题二": [2]}, ["A"])
    res = _run_capture(_QUESTIONS, rtv, capture_pool=False)
    assert "candidate_ids" not in res["per_q"][0]
    assert "result_chunk_ids" not in res["per_q"][0]


def test_逐问耗时被记录且为正():
    rtv = _CaptureRetriever({"问题一": [1], "问题二": [2]}, ["A"])
    res = _run_capture(_QUESTIONS, rtv, capture_pool=False)
    assert all(q["elapsed_s"] >= 0 for q in res["per_q"])
