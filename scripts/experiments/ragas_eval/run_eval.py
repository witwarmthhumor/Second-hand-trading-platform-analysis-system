"""RAGAS 评估主脚本：对当前检索器跑 NonLLM context_precision / context_recall。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py
       [--strategy current|optimized] [--top-k 20] [--limit N] [--out results/baseline.json]

输出：控制台汇总 + JSON（逐问明细 + 总指标 + 对照的 Hit@5/MRR/nDCG + **覆盖率**）。

**覆盖率是一等指标**（与 Hit@5 并列输出），分两级：

- **doc 级**（多文档 coverage@5 + 单文档 coverage@5）：Hit@5 只问「期望文档里
  至少有一篇进 top-5」，对多文档问题偏乐观，不惩罚「只中 1 篇、漏掉 4 篇」；
  coverage 才能暴露「该召回的文档有没有都进来」。两者必须一起看——只看 Hit@5
  曾把 `diverse` 这类有效策略误判为无效（见 `RAGAS 评估实验.md` 第九节）。
- **chunk 级**（recall / recall_norm / precision / hit1）：doc 级只要命中某篇
  期望文档的任意一块就算整篇覆盖，而一篇平均切 9.46 块，会系统性高估「内容
  覆盖」；chunk 级把分母细化到块，并按期望块数分箱（小/中/大）看
  「文档越大覆盖越差」。

口径实现均在 `metrics.py`。
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

from common import expected_chunk_ids, load_golden, reference_contexts_for
from metrics import (
    PRODUCTION_TOP_K,
    aggregate,
    aggregate_chunks,
    chunk_coverage_at_k,
    coverage_at_k,
)
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
    cov_rows_5: list[dict] = []
    cov_rows_k: list[dict] = []
    chunk_rows_5: list[dict] = []
    chunk_rows_k: list[dict] = []
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

        # 覆盖率（一等指标）：@生产口径 5 与 @本次 top_k 各算一份。
        # coverage@5 始终取排名前 5 个 chunk（先截断再去重），保证口径与
        # 「生产直接以 top_k=5 检索」一致——前提是 top_k == PRODUCTION_TOP_K，
        # 否则候选窗口更大，数字会略优于线上（故额外输出 production_comparable 标记）。
        cov5 = coverage_at_k(ranked_ids, expected, PRODUCTION_TOP_K)
        covk = coverage_at_k(ranked_ids, expected, top_k)
        cov_rows_5.append(cov5)
        cov_rows_k.append(covk)

        # chunk 级覆盖率：分母从「文档」细化到「块」（期望块 = 期望文档下全部
        # chunk）。doc 级只要某篇文档的任意一块进 top-k 就算整篇覆盖，会高估
        # 「内容覆盖」——chunk 级才看得出「进对了文档，却只带了一小块上下文」。
        # chunk_id 缺失（旧缓存 / 桩对象）用 "" 兜底，视作未命中。
        exp_chunks = expected_chunk_ids(expected)
        ranked_chunks = [r.get("chunk_id") or "" for r in results]
        chunk5 = chunk_coverage_at_k(ranked_chunks, exp_chunks, PRODUCTION_TOP_K)
        chunkk = chunk_coverage_at_k(ranked_chunks, exp_chunks, top_k)
        chunk5["is_multi"] = cov5["is_multi"]
        chunkk["is_multi"] = covk["is_multi"]
        chunk_rows_5.append(chunk5)
        chunk_rows_k.append(chunkk)

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
                "coverage_at5": cov5["coverage"],
                "distinct_docs_at5": cov5["distinct_docs"],
                "coverage_at_k": covk["coverage"],
                "distinct_docs_at_k": covk["distinct_docs"],
                "chunk_recall_at5": chunk5["recall"],
                "chunk_recall_norm_at5": chunk5["recall_norm"],
                "chunk_hit1": chunk5["hit1"],
                "ranked_ids": ranked_ids[:10],
            }
        )
        if i % 50 == 0:
            print(f"  进度 {i}/{n}")

    elapsed = time.time() - start
    cov5 = aggregate(cov_rows_5)
    covk = aggregate(cov_rows_k)
    ch5 = aggregate_chunks(chunk_rows_5)
    chk = aggregate_chunks(chunk_rows_k)
    return {
        "n": n,
        "top_k": top_k,
        "context_precision": prec_sum / n,
        "context_recall": recall_sum / n,
        "hit5": hit5_total / n,
        "mrr10": rr_sum / n,
        "ndcg10": ndcg_sum / n,
        # 覆盖率：多/单文档分开（多文档看 coverage，单文档 coverage 退化为命中率）
        "coverage": {
            "production_k": PRODUCTION_TOP_K,
            "production_comparable": top_k == PRODUCTION_TOP_K,
            "at5": cov5,
            "at_k": covk,
        },
        # chunk 级覆盖率：overall / multi / single / by_size（见 metrics.aggregate_chunks）
        "chunk_coverage": {
            "production_k": PRODUCTION_TOP_K,
            "at5": ch5,
            "at_k": chk,
        },
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
    c5 = results["coverage"]["at5"]
    print(
        f"[召回覆盖] 多文档 coverage@5={c5['multi_coverage']:.4f} "
        f"(distinct_docs={c5['multi_distinct_docs']:.2f}, n={c5['multi_n']})  "
        f"单文档 coverage@5={c5['single_coverage']:.4f} (n={c5['single_n']})  "
        f"Hit(任意@5)={c5['hit_any']:.4f}"
    )
    o = results["chunk_coverage"]["at5"]["overall"]
    print(
        f"[内容覆盖] chunk recall@5={o['recall']:.4f} "
        f"(名额内={o['recall_norm']:.4f}, 上界={o['coverable']:.4f})  "
        f"precision@5={o['precision']:.4f}  hit1={o['hit1']:.4f}"
    )
    for _name, _b in results["chunk_coverage"]["at5"]["by_size"].items():
        if _b["n"]:
            print(
                f"           {_name} n={_b['n']} recall={_b['recall']:.4f} "
                f"名额内={_b['recall_norm']:.4f} hit1={_b['hit1']:.4f}"
            )
    if not results["coverage"]["production_comparable"]:
        print(
            f"  ⚠️ 本次 top_k={results['top_k']} ≠ 生产 {PRODUCTION_TOP_K}："
            "覆盖率的候选窗口比线上更大，数字会略优于线上；要线上口径请加 --top-k 5"
        )
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
