"""RAGAS 优化前后对比：读取 baseline.json 与 optimized_prod.json 生成对比表。

输出：控制台对比表 + 归因分析（提升来自哪）。
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

METRICS = [
    ("hit5", "Hit@5"),
    ("mrr10", "MRR@10"),
    ("ndcg10", "nDCG@10"),
    ("context_precision", "context_precision"),
    ("context_recall", "context_recall"),
]


def _load(name: str) -> dict:
    return json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))


def main() -> int:
    base = _load("baseline.json")
    opt = _load("optimized_prod.json")

    print("## 总指标对比\n")
    print("| 指标 | 优化前 | 优化后 | Δ |")
    print("|------|--------|--------|-----|")
    for key, label in METRICS:
        b, o = base[key], opt[key]
        delta = o - b
        sign = "+" if delta >= 0 else ""
        print(f"| {label} | {b:.4f} | {o:.4f} | {sign}{delta:.4f} |")

    # 覆盖率（一等指标）：旧结果文件无此字段时整段跳过（向后兼容）
    cov_b = (base.get("coverage") or {}).get("at5")
    cov_o = (opt.get("coverage") or {}).get("at5")
    if cov_b and cov_o:
        print("\n### 覆盖率 @5（多文档 / 单文档必须分开看）\n")
        print("| 指标 | 优化前 | 优化后 | Δ |")
        print("|------|--------|--------|-----|")
        for key, label in (
            ("multi_coverage", "多文档 coverage@5"),
            ("single_coverage", "单文档 coverage@5"),
            ("hit_any", "Hit(任意@5)"),
            ("multi_distinct_docs", "多文档 distinct_docs@5"),
        ):
            b, o = cov_b[key], cov_o[key]
            delta = o - b
            sign = "+" if delta >= 0 else ""
            print(f"| {label} | {b:.4f} | {o:.4f} | {sign}{delta:.4f} |")

    # 归因：单文档 vs 多文档的 precision/recall 变化
    bq = {q["id"]: q for q in base["per_q"]}
    oq = {q["id"]: q for q in opt["per_q"]}

    def split(pq):
        s = [q for q in pq if len(q["expected_ids"]) == 1]
        m = [q for q in pq if len(q["expected_ids"]) > 1]
        return s, m

    bs, bm = split(base["per_q"])
    os_, om = split(opt["per_q"])
    print("\n## 分组归因\n")
    print("| 分组 | 指标 | 优化前 | 优化后 |")
    print("|------|------|--------|--------|")
    print(
        f"| 单文档({len(bs)}) | precision | {sum(q['context_precision'] for q in bs)/len(bs):.4f} "
        f"| {sum(q['context_precision'] for q in os_)/len(os_):.4f} |"
    )
    print(
        f"| 多文档({len(bm)}) | precision | {sum(q['context_precision'] for q in bm)/len(bm):.4f} "
        f"| {sum(q['context_precision'] for q in om)/len(om):.4f} |"
    )
    print(
        f"| 单文档({len(bs)}) | recall | {sum(q['context_recall'] for q in bs)/len(bs):.4f} "
        f"| {sum(q['context_recall'] for q in os_)/len(os_):.4f} |"
    )
    print(
        f"| 多文档({len(bm)}) | recall | {sum(q['context_recall'] for q in bm)/len(bm):.4f} "
        f"| {sum(q['context_recall'] for q in om)/len(om):.4f} |"
    )

    # 翻转统计
    better = sum(
        1
        for q in base["per_q"]
        if oq[q["id"]]["context_precision"] > bq[q["id"]]["context_precision"] + 1e-6
    )
    worse = sum(
        1
        for q in base["per_q"]
        if oq[q["id"]]["context_precision"] < bq[q["id"]]["context_precision"] - 1e-6
    )
    same = len(base["per_q"]) - better - worse
    print(f"\nprecision 逐问变化：变好 {better} / 变差 {worse} / 持平 {same}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
