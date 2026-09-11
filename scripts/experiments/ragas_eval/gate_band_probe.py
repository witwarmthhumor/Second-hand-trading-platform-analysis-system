"""相关性闸门判据诊断：为什么「用 vsim 阈值区分该不该占名额」行不通。

动机（docs/RAG优化方案.md 4.7.1）：P0-2 原方案给保底阶段加绝对阈值
``DOC_MIN_SIM``（文档代表块 vsim < 阈值则不占名额）。实测 0.3/0.4/0.5 三档与
「无闸门」四项指标**逐位相同**——闸门根本没触发；0.6 才微生效且伴随多文档
coverage 下降。本脚本量化根因。

做法：逐问复现 ``ranked_all``（按 vsim 降序的全量 RRF 候选），统计各文档
**代表块**（该文档 vsim 最高的 chunk）的 vsim，然后比较两类分布：
- 单文档问题的**非期望**文档代表块 vsim / best —— 「相关但非目标」
- 多文档问题的**期望**文档代表块 vsim / best —— 「目标」
若两分布重叠，则任何绝对阈值或相对带宽都无法区分「该占席位」与「不该占」。

结论（285 问实测）：前者中位 0.898、后者中位 0.934，**严重重叠**；bge-m3 把
余弦相似度压在窄区间（候选池内各文档代表块普遍 ≥0.55），故 0.5 以下阈值完全
无效。这类信息不在相似度标尺上 → 改用「锚定文档名额」旋钮（见 4.7.2）。

用法（项目根目录，纯检索、无 LLM 成本）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/gate_band_probe.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv(ROOT / "backend" / ".env")

from common import load_golden  # noqa: E402

from app.rag.embedding import query_embedding  # noqa: E402
from app.rag.retriever import get_retriever  # noqa: E402

QUERY_N_RESULTS = 20  # 与生产 config.QUERY_N_RESULTS 对齐
RRF_K = 60.0  # 与生产 config.RRF_K 对齐

SAMPLE = 0  # 0 = 全部问题（与其余实验的 285 问口径一致）；>0 时取前 N 问快速验证


def _pct(values: list[float], p: float) -> float:
    """取分位数（p ∈ [0,1]），不引 numpy。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(p * len(ordered)))
    return ordered[idx]


def main() -> None:
    base = get_retriever()
    col = base._collection
    bm25 = base._bm25
    doc_ids = base._doc_ids

    def ranked_all_for(query: str) -> tuple[list[tuple[int, float]], dict[int, float]]:
        """复现生产检索的 ranked_all（全量 RRF 候选，按 vsim 降序）。"""
        qemb = query_embedding(query)
        vres = col.query(query_embeddings=[qemb], n_results=QUERY_N_RESULTS)
        v_scores: dict[int, float] = {}
        for cid, dist in zip(vres["ids"][0], vres["distances"][0], strict=False):
            v_scores[int(cid[1:])] = 1.0 - float(dist)
        b_scores = dict(bm25.search(query, top_n=QUERY_N_RESULTS)) if bm25 is not None else {}

        rrf: dict[int, float] = {}
        for rank, pos in enumerate(
            sorted(v_scores, key=lambda k: -v_scores[k])[:QUERY_N_RESULTS], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + 1.0 / (RRF_K + rank)
        for rank, pos in enumerate(
            sorted(b_scores, key=lambda k: -b_scores[k])[:QUERY_N_RESULTS], 1
        ):
            rrf[pos] = rrf.get(pos, 0.0) + 1.0 / (RRF_K + rank)
        ranked = sorted(rrf.items(), key=lambda x: -v_scores.get(x[0], 0.0))
        return ranked, v_scores

    def reps_vsim(ranked: list[tuple[int, float]], v: dict[int, float]):
        """按 vsim 降序取每篇文档首次出现的代表块，返回 [(doc_id, vsim), ...]。"""
        seen: set[str] = set()
        out: list[tuple[str, float]] = []
        for pos, _ in ranked:
            did = doc_ids[pos] if pos < len(doc_ids) else f"c{pos}"
            if did in seen:
                continue
            seen.add(did)
            out.append((did, v.get(pos, 0.0)))
        return out

    questions = load_golden()
    if SAMPLE > 0:
        questions = questions[:SAMPLE]
    single = [q for q in questions if len(q["expected_ids"]) == 1]
    multi = [q for q in questions if len(q["expected_ids"]) > 1]
    print(f"共 {len(questions)} 问（单文档 {len(single)} / 多文档 {len(multi)}），"
          f"候选池 {QUERY_N_RESULTS}\n")

    # ---- 单文档问题：非期望文档代表块（噪声）的 vsim 与比值 ----
    noise_sims: list[float] = []
    noise_ratios: list[float] = []
    exp_slots: list[int] = []
    for q in single:
        exp = q["expected_ids"][0]
        ranked, v = ranked_all_for(q["question"])
        reps = reps_vsim(ranked, v)
        best = reps[0][1]
        exp_slots.append(sum(1 for d, _ in reps[:5] if d == exp))
        for d, s in reps[1:5]:  # top-5 内的其它文档
            if d == exp:
                continue
            noise_sims.append(s)
            noise_ratios.append(s / best if best else 0.0)

    print("=== 单文档问题：非期望文档代表块（「相关但非目标」）===")
    print(f"  噪声块 vsim    中位 {statistics.median(noise_sims):.4f} "
          f"p5 {_pct(noise_sims, 0.05):.4f} max {max(noise_sims):.4f} n={len(noise_sims)}")
    print(f"  噪声块 s/best  中位 {statistics.median(noise_ratios):.4f} "
          f"p10 {_pct(noise_ratios, 0.10):.4f} p90 {_pct(noise_ratios, 0.90):.4f}")
    print(f"  单文档问题 top-5 里期望文档占位均值 {statistics.mean(exp_slots):.2f}")
    print("  —— 绝对阈值下的噪声保留率（越低越能挡）：")
    for thr in (0.4, 0.5, 0.55, 0.6, 0.65, 0.7):
        keep = sum(1 for s in noise_sims if s >= thr)
        print(f"     {thr}: {keep / len(noise_sims):.3f} ({keep}/{len(noise_sims)})")
    print("  —— 相对带宽下的噪声保留率：")
    for r_thr in (0.8, 0.85, 0.9, 0.95):
        keep = sum(1 for x in noise_ratios if x >= r_thr)
        print(f"     {r_thr}: {keep / len(noise_ratios):.3f}")

    # ---- 多文档问题：期望文档代表块（目标）的比值 ----
    exp_sims: list[float] = []
    exp_ratios: list[float] = []
    for q in multi:
        exps = set(q["expected_ids"])
        ranked, v = ranked_all_for(q["question"])
        reps = reps_vsim(ranked, v)
        best = reps[0][1]
        for d, s in reps:
            if d in exps:
                exp_sims.append(s)
                exp_ratios.append(s / best if best else 0.0)

    print("\n=== 多文档问题：期望文档代表块（「目标」）===")
    print(f"  期望块 vsim    中位 {statistics.median(exp_sims):.4f} n={len(exp_sims)}")
    print(f"  期望块 s/best  中位 {statistics.median(exp_ratios):.4f} "
          f"p10 {_pct(exp_ratios, 0.10):.4f} min {min(exp_ratios):.4f}")
    print("  —— 绝对阈值下的期望保留率（越高越不误伤）：")
    for thr in (0.5, 0.6, 0.65, 0.7):
        keep = sum(1 for s in exp_sims if s >= thr)
        print(f"     {thr}: {keep / len(exp_sims):.3f} ({keep}/{len(exp_sims)})")
    print("  —— 相对带宽下的期望保留率：")
    for r_thr in (0.8, 0.85, 0.9, 0.95):
        keep = sum(1 for x in exp_ratios if x >= r_thr)
        print(f"     {r_thr}: {keep / len(exp_ratios):.3f}")

    print("\n判读：两类分布的 s/best 中位仅差 %.3f，绝对阈值与相对带宽都无区分度"
          % (statistics.median(exp_ratios) - statistics.median(noise_ratios)))
    print("     → 相关性闸门判据不成立，改用锚定文档名额（docs/RAG优化方案.md 4.7.2）。")


if __name__ == "__main__":
    main()
