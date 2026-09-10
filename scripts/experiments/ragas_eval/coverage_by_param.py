"""多文档覆盖率诊断：各检索参数组在「生产 top_k=5」下的期望文档覆盖对比。

动机：此前用 doc 级 Hit@5 评价检索策略，它只回答「期望文档里**至少有一篇**
进 top-5」，对多文档问题系统性偏乐观——不惩罚「只中 1 篇、漏掉 4 篇」。
本脚本补测两个更贴多文档场景的指标：

- ``coverage@5``：top-5 命中的期望文档数 / 期望文档总数（多文档的真实召回）
- ``distinct_docs@5``：top-5 里不同 doc_id 的个数（衡量「同文档多 chunk 占位」）

用途：判断「多文档召回」短板到底出在候选池、融合权重，还是排序阶段的文档聚簇。
注意本脚本按**生产 top_k=5** 跑，而 rerank 是在 RRF top-5 内部重排，因此
结果与 ``run_eval.py`` 默认 top_k=20 的口径**不可直接比较**。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/coverage_by_param.py
      [--params baseline,rerank,diverse50] [--top-k 5] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from common import load_golden

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

# 加载 backend/.env（database_url / jwt_secret 等为必填字段）
from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

from app.rag.retriever import get_retriever
from strategies import PARAM_SETS, ConfigurableRetriever

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 重点关注：生产默认 vs 已上线的 rerank vs 文档多样性两组
DEFAULT_PARAMS = "baseline,rerank,pool50,diverse50,diverse80"


def _unique(ids: list[str]) -> list[str]:
    """按出现顺序去重（保序，便于看「哪篇排前面」）。"""
    return list(dict.fromkeys(ids))


def measure(questions: list[dict], retriever, top_k: int) -> dict:
    """逐问检索，统计多/单文档的覆盖率与 top-5 文档多样性。"""
    multi_cov: list[float] = []
    single_cov: list[float] = []
    multi_distinct: list[int] = []
    hits: list[int] = []
    per_q: list[dict] = []

    for q in questions:
        expected = q["expected_ids"]
        results = retriever.search(q["question"], top_k=top_k)
        ids = _unique([r["doc_id"] for r in results])
        inter = len(set(ids) & set(expected))
        cov = inter / len(expected) if expected else 0.0
        hits.append(int(inter > 0))

        if len(expected) > 1:
            multi_cov.append(cov)
            multi_distinct.append(len(ids))
        else:
            single_cov.append(cov)

        per_q.append(
            {
                "id": q.get("id"),
                "question": q["question"],
                "expected_ids": expected,
                "ranked_ids": ids[:top_k],
                "coverage": cov,
            }
        )

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "n": len(questions),
        "multi_n": len(multi_cov),
        "single_n": len(single_cov),
        "multi_coverage": _mean(multi_cov),
        "single_coverage": _mean(single_cov),
        "multi_distinct_docs": _mean([float(x) for x in multi_distinct]),
        "hit_any": _mean([float(x) for x in hits]),
        "per_q": per_q,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default=DEFAULT_PARAMS, help="逗号分隔的 PARAM_SETS 组名")
    ap.add_argument("--top-k", type=int, default=5, help="与生产 ask() 一致")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 问（调试用）")
    ap.add_argument("--out", default="coverage_by_param.json", help="结果 JSON 文件名（写入 results/）")
    args = ap.parse_args()

    names = [s.strip() for s in args.params.split(",") if s.strip()]
    unknown = [n for n in names if n not in PARAM_SETS]
    if unknown:
        print(f"未知参数组 {unknown}，可用：{list(PARAM_SETS)}")
        return 2

    questions = load_golden()
    if args.limit:
        questions = questions[: args.limit]
    n_multi = sum(1 for q in questions if len(q["expected_ids"]) > 1)
    print(
        f"多文档覆盖率诊断：{len(questions)} 问（多文档 {n_multi} / 单文档 "
        f"{len(questions) - n_multi}），top_k={args.top_k}"
    )

    base = get_retriever()
    if not base.is_ready:
        print("向量库未就绪，请先运行 scripts/build_vector_store.py")
        return 2

    rows: list[tuple[str, dict, float]] = []
    for name in names:
        t0 = time.time()
        rtv = ConfigurableRetriever(base, PARAM_SETS[name])
        m = measure(questions, rtv, args.top_k)
        elapsed = time.time() - t0
        rows.append((name, m, elapsed))
        print(
            f"  [{name}] 多文档 coverage@{args.top_k}={m['multi_coverage']:.4f} "
            f"(distinct={m['multi_distinct_docs']:.2f}) "
            f"单文档 coverage={m['single_coverage']:.4f} "
            f"Hit(任意)={m['hit_any']:.4f} | {elapsed:.1f}s"
        )

    print(f"\n## 多文档召回诊断（{len(questions)} 问，top_k={args.top_k}）\n")
    print(
        f"| 参数组 | 多文档 coverage@{args.top_k} | 多文档 distinct_docs@{args.top_k} "
        "| 单文档 coverage | Hit(任意一篇) | 耗时(s) |"
    )
    print("|---|---|---|---|---|---|")
    for name, m, elapsed in rows:
        print(
            f"| {name} | {m['multi_coverage']:.4f} | {m['multi_distinct_docs']:.2f} "
            f"| {m['single_coverage']:.4f} | {m['hit_any']:.4f} | {elapsed:.1f} |"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "n": len(questions),
        "top_k": args.top_k,
        "aggregate": {
            name: {
                "multi_coverage": m["multi_coverage"],
                "multi_distinct_docs": m["multi_distinct_docs"],
                "single_coverage": m["single_coverage"],
                "hit_any": m["hit_any"],
                "elapsed": round(elapsed, 1),
            }
            for name, m, elapsed in rows
        },
        "per_q": {name: m["per_q"] for name, m, _ in rows},
    }
    out_path = RESULTS_DIR / args.out
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
