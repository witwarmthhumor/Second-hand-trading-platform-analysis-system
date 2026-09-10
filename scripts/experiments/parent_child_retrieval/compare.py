"""阶段 3：聚合三策略结果 → 对比表 + 归因分析。

读取 ``results/<strategy>.json``，输出：

- Markdown 对比表（recall@1/3/5/10 + Hit@5 + MRR@10 + nDCG@10）
- 分类目 Hit@5 对比
- 逐问翻转分析：以最优基线为基准，找出「失→得 / 得→失」的 query，
  并给出归因所需的数据（候选池中文档数、命中位次分布）。
- 结果写入 ``results/comparison.json``

用法：
    backend/.venv/Scripts/python scripts/experiments/parent_child_retrieval/compare.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RESULTS_DIR  # noqa: E402

STRATEGIES = ["heading", "recursive", "child400", "parent_child"]
LABELS = {
    "heading": "标题分块基线（生产·chunk900）",
    "recursive": "递归分块基线（900/10%）",
    "child400": "消融·仅子块（400）",
    "parent_child": "父子文档检索（父1600/子400）",
}
METRICS = ["recall@1", "recall@3", "recall@5", "recall@10", "hit@5", "mrr@10", "ndcg@10"]


def load_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in STRATEGIES:
        p = RESULTS_DIR / f"{name}.json"
        if p.exists():
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    return out


def comparison_table(results: dict[str, dict]) -> str:
    """生成总指标 Markdown 对比表。"""
    names = [n for n in STRATEGIES if n in results]
    head = "| 指标 | " + " | ".join(LABELS[n] for n in names) + " |"
    sep = "|------|" + "|".join("------" for _ in names) + "|"
    lines = [head, sep]
    for m in METRICS:
        cells = [f"{results[n]['aggregate'][m]:.4f}" for n in names]
        lines.append(f"| {m} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def category_table(results: dict[str, dict]) -> str:
    """生成分类目 Hit@5 对比表。"""
    names = [n for n in STRATEGIES if n in results]
    cats = sorted(results[names[0]]["aggregate"]["cat_hit5"].keys())
    head = "| 类目 | 问数 | " + " | ".join(LABELS[n] for n in names) + " |"
    sep = "|------|" + "|".join("------" for _ in range(len(names) + 1)) + "|"
    lines = [head, sep]
    for c in cats:
        n_q = results[names[0]]["aggregate"]["cat_n"].get(c, 0)
        cells = [f"{results[n]['aggregate']['cat_hit5'].get(c, 0):.4f}" for n in names]
        lines.append(f"| {c} | {n_q} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _pq_by_id(result: dict) -> dict[str, dict]:
    return {q["id"]: q for q in result["per_query"]}


def flip_analysis(results: dict[str, dict], base: str, other: str, k: int = 5) -> dict:
    """对比 base 与 other 在 recall@k 上的逐问翻转。"""
    b = _pq_by_id(results[base])
    o = _pq_by_id(results[other])
    gained, lost = [], []
    for qid, qo in o.items():
        rb = b[qid]["metrics"][f"recall@{k}"]
        ro = qo["metrics"][f"recall@{k}"]
        if ro > rb:
            gained.append((qid, qo["expected"], qo["ranked"][:k]))
        elif ro < rb:
            lost.append((qid, qo["expected"], qo["ranked"][:k]))
    return {"base": base, "other": other, "k": k, "gained": gained, "lost": lost}


def pool_stats(results: dict[str, dict]) -> dict:
    """每策略：平均候选池去重后文档数（反映 doc 级覆盖）。"""
    stats = {}
    for name, r in results.items():
        pools = [len(q["ranked"]) for q in r["per_query"]]
        stats[name] = round(sum(pools) / len(pools), 2) if pools else 0.0
    return stats


def ablation_table(results: dict[str, dict]) -> str:
    """归因消融：把「子块粒度效应」与「父块回填效应」分离。

    - Δ 粒度 = child400 − recursive(900)：仅缩小分块粒度带来的变化
    - Δ 回填 = parent_child − child400：在相同子块粒度上，叠加父块回填带来的变化
    """
    if not {"recursive", "child400", "parent_child"} <= set(results):
        return "（缺少消融结果，需运行 --strategy child400）"
    head = (
        "| 指标 | recursive(900) | child400(子块) | parent_child | "
        "Δ粒度(400−900) | Δ回填(父−子) |"
    )
    sep = "|---|---|---|---|---|---|"
    lines = [head, sep]
    for m in METRICS:
        r = results["recursive"]["aggregate"][m]
        c = results["child400"]["aggregate"][m]
        p = results["parent_child"]["aggregate"][m]
        lines.append(
            f"| {m} | {r:.4f} | {c:.4f} | {p:.4f} | "
            f"{c - r:+.4f} | {p - c:+.4f} |"
        )
    return "\n".join(lines)


def main() -> int:
    results = load_all()
    if not results:
        print("没有结果文件，请先运行 run_all.py")
        return 1

    print("## 总指标对比\n")
    print(comparison_table(results))
    print("\n## 分类目 Hit@5\n")
    print(category_table(results))
    print("\n## 归因消融（粒度效应 vs 父块回填效应）\n")
    print(ablation_table(results))

    if "parent_child" in results:
        # 基准取两种基线里 recall@5 更高的那个
        bases = [n for n in ("heading", "recursive") if n in results]
        base = max(bases, key=lambda n: results[n]["aggregate"]["recall@5"])
        print(f"\n## 翻转分析（基准 = {LABELS[base]} → 父子检索, recall@5）\n")
        fa = flip_analysis(results, base, "parent_child", k=5)
        print(f"- 失→得（父子检索补回）：{len(fa['gained'])} 问")
        print(f"- 得→失（父子检索丢失）：{len(fa['lost'])} 问")
        print("\n失→得样例（前 10）：")
        for qid, exp, topk in fa["gained"][:10]:
            print(f"  - {qid} 期望={exp} top5={topk}")
        if fa["lost"]:
            print("\n得→失样例（前 10）：")
            for qid, exp, topk in fa["lost"][:10]:
                print(f"  - {qid} 期望={exp} top5={topk}")
        print(f"\n候选池平均去重文档数：{pool_stats(results)}")
        payload = {n: results[n]["aggregate"] for n in results}
        payload["flip"] = {"base": base, "gained": fa["gained"], "lost": fa["lost"]}
        payload["pool_stats"] = pool_stats(results)
        (RESULTS_DIR / "comparison.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n对比数据已写入：{RESULTS_DIR / 'comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
