"""父子文档检索实验 —— 主运行脚本。

对指定策略跑同一 golden 测试集（285 问），输出 doc 级指标到
``results/<strategy>.json``。

用法（项目根目录，用 backend 的 venv）：
    backend/.venv/Scripts/python scripts/experiments/parent_child_retrieval/run_all.py --strategy all
    backend/.venv/Scripts/python .../run_all.py --strategy parent_child
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 允许从任意目录运行：把本脚本目录加入 sys.path，使 `common`/`strategies` 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RESULTS_DIR, aggregate, doc_metrics, load_golden  # noqa: E402
from strategies import BUILDERS, DOC_LIMIT  # noqa: E402


def evaluate(strategy_name: str) -> dict:
    """构建索引 + 跑全集，返回 {aggregate, per_query}。"""
    golden = load_golden()
    print(f"[{strategy_name}] 构建索引中 ...")
    t_build = time.time()
    retriever = BUILDERS[strategy_name]()
    build_elapsed = time.time() - t_build
    print(f"[{strategy_name}] 索引就绪，构建耗时 {build_elapsed:.1f}s")

    per_query: list[dict] = []
    t_eval = time.time()
    for i, q in enumerate(golden, 1):
        ranked = retriever.doc_pool(q["question"], limit=DOC_LIMIT)
        expected = q["expected_ids"]
        per_query.append(
            {
                "id": q["id"],
                "series": q.get("series", ""),
                "question": q["question"],
                "expected": expected,
                "ranked": ranked,
                "metrics": doc_metrics(ranked, expected),
            }
        )
        if i % 50 == 0:
            print(f"  [{strategy_name}] {i}/{len(golden)}")
    eval_elapsed = time.time() - t_eval

    agg = aggregate(per_query)
    agg.update(
        {
            "strategy": strategy_name,
            "build_elapsed": round(build_elapsed, 1),
            "eval_elapsed": round(eval_elapsed, 1),
        }
    )
    return {"aggregate": agg, "per_query": per_query}


def save(strategy_name: str, result: dict) -> Path:
    """落盘结果 JSON。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{strategy_name}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _print_summary(name: str, agg: dict) -> None:
    print(f"\n[{name}] 结果（n={agg['n']}）")
    for k in ("recall@1", "recall@3", "recall@5", "recall@10", "mrr@10", "ndcg@10"):
        print(f"  {k:>10} = {agg[k]:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="父子文档检索实验")
    parser.add_argument(
        "--strategy",
        default="all",
        choices=[*BUILDERS.keys(), "all"],
        help="要运行的策略（all = 依次全部运行）",
    )
    args = parser.parse_args()

    targets = list(BUILDERS.keys()) if args.strategy == "all" else [args.strategy]
    for name in targets:
        result = evaluate(name)
        out = save(name, result)
        _print_summary(name, result["aggregate"])
        print(f"[{name}] 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
