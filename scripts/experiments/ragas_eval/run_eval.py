"""RAGAS 评估主脚本：对当前检索器跑 NonLLM context_precision / context_recall。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py
       [--strategy current|optimized] [--top-k 20] [--limit N] [--out results/baseline.json]

输出：控制台汇总 + JSON（逐问明细 + 总指标 + 对照的 Hit@5/MRR/nDCG）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math

# 允许直接运行导入 app 包
import sys
import time
from pathlib import Path

from common import load_golden, reference_contexts_for
from ragas import SingleTurnSample
from ragas.metrics import NonLLMContextPrecisionWithReference, NonLLMContextRecall

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

# 加载 backend/.env（database_url / jwt_secret 等为必填字段）
from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

from app.rag.retriever import get_retriever
from strategies import PARAM_SETS, ConfigurableRetriever

RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ---------------------------------------------------------------------------
# 对照指标（与 eval_retrieval.py 同口径，用于和 RAGAS 指标互证）
# ---------------------------------------------------------------------------


def _retrieval_metrics(ranked_ids: list[str], expected: list[str]) -> dict:
    """Hit@5 / RR@10 / nDCG@10，doc 级去重口径，与生产评测一致。"""
    uniq = list(dict.fromkeys(ranked_ids))
    hit5 = int(any(e in uniq[:5] for e in expected))
    rr = 0.0
    for rank, rid in enumerate(uniq[:10], 1):
        if rid in expected:
            rr = 1.0 / rank
            break
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, rid in enumerate(uniq[:10], 1)
        if rid in expected
    )
    ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(len(expected), 10) + 1))
    ndcg = dcg / ideal if ideal else 0.0
    return {"hit5": hit5, "rr": rr, "ndcg": ndcg}


async def evaluate(questions: list[dict], top_k: int, retriever=None) -> dict:
    """逐问检索 → 构造 SingleTurnSample → 跑两个非 LLM 指标。

    retriever 不传时用生产单例；传入则为实验版 ConfigurableRetriever。
    """
    if retriever is None:
        retriever = get_retriever()
    if not retriever.is_ready:
        raise RuntimeError("向量库未就绪，请先运行 build_vector_store")

    precision = NonLLMContextPrecisionWithReference()
    recall = NonLLMContextRecall()

    n = len(questions)
    prec_sum = 0.0
    recall_sum = 0.0
    hit5_total = 0
    rr_sum = 0.0
    ndcg_sum = 0.0
    per_q: list[dict] = []
    failed = 0

    start = time.time()
    for i, q in enumerate(questions, 1):
        question = q["question"]
        expected = q["expected_ids"]

        # 检索（关闭缓存影响，直接搜）
        results = retriever.search(question, top_k=top_k)
        retrieved = [r["text"] for r in results]
        ranked_ids = [r["doc_id"] for r in results]
        ref_ctxs = reference_contexts_for(expected)

        sample = SingleTurnSample(
            user_input=question,
            retrieved_contexts=retrieved,
            reference_contexts=ref_ctxs,
        )
        try:
            p = await precision.single_turn_ascore(sample)
            r = await recall.single_turn_ascore(sample)
        except Exception as exc:  # 单问失败不中断整体  # noqa: BLE001
            failed += 1
            p = 0.0
            r = 0.0
            if failed <= 3:
                print(f"  [warn] Q {q.get('id')} 评分失败: {type(exc).__name__}")

        prec_sum += p
        recall_sum += r

        m = _retrieval_metrics(ranked_ids, expected)
        hit5_total += m["hit5"]
        rr_sum += m["rr"]
        ndcg_sum += m["ndcg"]

        per_q.append(
            {
                "id": q.get("id"),
                "question": question,
                "expected_ids": expected,
                "context_precision": float(p),
                "context_recall": float(r),
                "hit5": m["hit5"],
                "rr": m["rr"],
                "ndcg": m["ndcg"],
                "ranked_ids": ranked_ids[:10],
            }
        )
        if i % 50 == 0:
            print(f"  进度 {i}/{n}")

    elapsed = time.time() - start
    return {
        "n": n,
        "top_k": top_k,
        "context_precision": prec_sum / n,
        "context_recall": recall_sum / n,
        "hit5": hit5_total / n,
        "mrr10": rr_sum / n,
        "ndcg10": ndcg_sum / n,
        "elapsed": round(elapsed, 1),
        "failed": failed,
        "per_q": per_q,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="current", help="结果标签（current/optimized）")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 问（调试用）")
    ap.add_argument("--params", default=None, help="PARAM_SETS 参数组名（baseline/pool50/...）")
    ap.add_argument("--out", default="results/baseline.json")
    args = ap.parse_args()

    golden = load_golden()
    if args.limit:
        golden = golden[: args.limit]

    retriever = None
    if args.params:
        if args.params not in PARAM_SETS:
            print(f"未知参数组 {args.params}，可用：{list(PARAM_SETS)}")
            return 2
        base = get_retriever()
        retriever = ConfigurableRetriever(base, PARAM_SETS[args.params])
        label = f"{args.strategy}::{args.params}"
    else:
        label = args.strategy

    print(f"RAGAS 非 LLM 评估（{label}）：{len(golden)} 问，top_k={args.top_k}")
    results = asyncio.run(evaluate(golden, args.top_k, retriever=retriever))
    results["strategy"] = label

    print("\n" + "=" * 52)
    print(f"context_precision = {results['context_precision']:.4f}  （相关块是否排前）")
    print(f"context_recall    = {results['context_recall']:.4f}  （是否漏检）")
    print(f"[对照] Hit@5={results['hit5']:.4f}  MRR@10={results['mrr10']:.4f}  "
          f"nDCG@10={results['ndcg10']:.4f}")
    print(f"失败 {results['failed']} 问，耗时 {results['elapsed']}s")

    out_path = RESULTS_DIR / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
