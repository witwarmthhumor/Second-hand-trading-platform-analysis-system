"""P1-② Step 3：SYNTH_MODE 三档生成侧 A/B（direct vs multi_doc vs all）。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare_synth_mode.py
        [--sample 40] [--seed 42] [--top-k 5] [--concurrency 3] [--budget-cny 2]

三个 arm 共用同一份双视图检索（策略变量在**生成侧**，检索缓存天然复用，无需逐 arm 清缓存）：
- ``direct``：top1 原文 —— = 现网 SYNTH_MODE=off 行为，零生成成本；
- ``multi_doc``：广度 sources ≥ 2 → 生产合成路径，否则直出（复刻生产 ask() 分流）；
- ``all``：全部走生产合成路径。

合成走**生产代码** ``rag_service._llm_synthesize``（深度视图上下文 + 剥标 + 拒答判定 +
降级语义），保证评测对象与生产形态一致；生成失败/拒答降级直出并单独计数。

成本：合成生成走生产 CostTracker（**实验前 reset 清零**，避免历史累计触发 precheck
熔断导致全部降级——那会把 A/B 跑成三份直出）；RAGAS 裁判走 llm_common USAGE；
预算闸门 = 两者之和，超限在开跑下一问之前停（退出码 3）。

输出：results/synth_mode_ab.json（meta + per_q 三 arm 逐问 + 分桶汇总 + 预登记判定）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from common import load_golden
from gen_answers import answer_direct
from llm_common import (
    USAGE,
    BudgetExceeded,
    build_judge_embeddings,
    build_judge_llm,
)
from ragas import SingleTurnSample
from ragas.metrics import AnswerRelevancy, Faithfulness

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from run_gen_eval import (  # noqa: E402
    RESULTS_DIR,
    LLMFatalError,
    RunAborted,
    score,
    stratified_sample,
    summarize,
)

from app.ai.cost import get_cost_tracker  # noqa: E402
from app.rag.retriever import get_retriever  # noqa: E402
from app.services import rag_service  # noqa: E402

ARMS = ("direct", "multi_doc", "all")


async def _synth_once(question: str, breadth: list[dict], depth: list[dict]) -> dict:
    """每问合成一次（生产路径：深度上下文 + 剥标 + 拒答判定 + 降级语义）。

    返回 {answer, cited, outcome, latency, ctx}：
    - 成功：answer=剥标合成文本，ctx=**深度视图**（答案的生成依据，RAGAS
      retrieved_contexts 必须用它，否则裁判会因上下文缺块误判无支撑）；
    - 失败/拒答：降级直出 top1 原文，ctx=广度视图。
    """
    t0 = time.time()
    synth = await rag_service._llm_synthesize(question, depth)
    latency = round(time.time() - t0, 2)
    if synth is None:
        return {
            "answer": answer_direct(breadth),
            "cited": [breadth[0]["doc_id"]] if breadth else [],
            "outcome": "fallback_direct",
            "latency": latency,
            "ctx": breadth,
        }
    cleaned, cited = synth
    return {
        "answer": cleaned,
        "cited": cited or ([breadth[0]["doc_id"]] if breadth else []),
        "outcome": "ok",
        "latency": latency,
        "ctx": depth,
    }


async def evaluate_arms(
    questions: list[dict], top_k: int, concurrency: int, budget_cny: float
) -> list[dict]:
    """逐问：双视图检索 → 三 arm 答案 → RAGAS 打分。"""
    rtv = get_retriever()
    if not rtv.is_ready:
        raise RuntimeError("向量库未就绪，请先运行 scripts/build_vector_store.py")

    tracker = get_cost_tracker()
    tracker.reset()
    start_cost = tracker.total_cost_cny

    llm = build_judge_llm()
    emb = build_judge_embeddings()
    # strictness=1：网关忽略 n>1（见 run_gen_eval 同注释）
    judges = {
        arm: (Faithfulness(llm=llm), AnswerRelevancy(llm=llm, embeddings=emb, strictness=1))
        for arm in ARMS
    }
    sem = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    for idx, q in enumerate(questions, 1):
        spent = USAGE.cost_cny + (tracker.total_cost_cny - start_cost)
        if spent > budget_cny:
            raise BudgetExceeded(f"累计 ¥{spent:.3f} 已超预算 ¥{budget_cny:.2f}")

        t0 = time.time()
        try:
            breadth, depth = await rag_service.retrieve(
                q["question"], top_k=top_k, retriever=rtv, with_depth=True
            )
            expected = q["expected_ids"]
            hit = rag_service.build_hit_gate(breadth)
            n_sources = len(hit["sources"]) if hit else 0
            top1 = breadth[0]["doc_id"] if breadth else ""

            # 每问只合成一次：multi_doc 触发时与 all 的合成完全相同（同问同深度
            # 上下文），共享结果即可把生成成本减半。
            arm_all = await _synth_once(q["question"], breadth, depth)
            row: dict = {
                "id": q["id"],
                "category": q.get("category", ""),
                "question": q["question"],
                "expected_ids": expected,
                "n_expected": len(expected),
                "n_sources_breadth": n_sources,
                "synth_outcome": arm_all["outcome"],
                "synth_latency": arm_all["latency"],
            }

            # jobs[arm] = (答案, 引用, 评分用的上下文)。
            # ⚠️ ctx 必须与答案的生成依据一致：direct/降级直出 → 广度；合成 → 深度。
            #    若把广度传给合成答案，裁判会因「top1 文档其余块不在上下文」而误判无支撑。
            # multi_doc 触发时与 all 是**同一份合成**（同答案同上下文）→ 评分直接
            # 共享 all 的结果：既省一次裁判费，也消除裁判对同一答案两次打分的随机噪音。
            jobs: dict[str, tuple[str, list[str], list[dict]]] = {
                "direct": (
                    answer_direct(breadth),
                    [top1],
                    breadth,
                ),
                "all": (arm_all["answer"], arm_all["cited"], arm_all["ctx"]),
            }

            for arm, (text, cited, ctx) in jobs.items():
                faith, relev = judges[arm]
                ctx_texts = [r["text"] for r in ctx]
                f_v, r_v = await asyncio.gather(
                    score(
                        faith,
                        SingleTurnSample(
                            user_input=q["question"],
                            response=text,
                            retrieved_contexts=ctx_texts,
                        ),
                        sem,
                    ),
                    score(
                        relev,
                        SingleTurnSample(
                            user_input=q["question"],
                            response=text,
                            retrieved_contexts=ctx_texts,
                        ),
                        sem,
                    ),
                )
                row[arm] = {
                    "answer": text,
                    "faithfulness": f_v,
                    "answer_relevancy": r_v,
                    "coverage": _coverage(cited, expected),
                    "answer_len": len(text),
                    "cited": cited,
                }

            # multi_doc：触发（src≥2）= all 形态；未触发 = direct 形态。
            # 两种情况都与既有 arm 同答案同上下文，评分直接复制。
            row["multi_doc"] = dict(row["all"] if n_sources >= 2 else row["direct"])
        except LLMFatalError as exc:
            raise RunAborted(str(exc)) from exc

        rows.append(row)
        spent = USAGE.cost_cny + (tracker.total_cost_cny - start_cost)
        brief = " ".join(
            f"{arm}:{row[arm]['faithfulness']}/{row[arm]['answer_relevancy']}"
            for arm in ARMS
            if arm in row
        )
        print(
            f"  [{idx}/{len(questions)}] {q['id']} 期望{len(expected)}篇 src={n_sources} "
            f"| {brief} | {time.time() - t0:.1f}s | 累计 ¥{spent:.3f}",
            flush=True,
        )
    return rows


def _coverage(answer_docs: list[str], expected: list[str]) -> float:
    """答案实际覆盖的 expected 文档比例（与 run_gen_eval.coverage 同口径）。"""
    if not expected:
        return 0.0
    hit = len({d.upper() for d in answer_docs} & {e.upper() for e in expected})
    return hit / len(expected)


def judge(rows: list[dict]) -> dict:
    """预登记判定（docs/合成答案分流方案.md §五，对 multi_doc 档）。"""
    s_md = summarize(rows, "multi_doc")
    s_all = summarize(rows, "all")
    checks = {
        "multi_doc_faith_total>=0.965": (s_md["faithfulness"] or 0) >= 0.965,
        "multi_doc_relev>=0.8455": (s_md["answer_relevancy"] or 0) >= 0.8455,
        "multi_doc_coverage>=0.6025": (s_md["coverage"] or 0) >= 0.6025,
        "all_faith_total>=0.965": (s_all["faithfulness"] or 0) >= 0.965,
        "all_relev>=0.8455": (s_all["answer_relevancy"] or 0) >= 0.8455,
        "all_coverage>=0.6025": (s_all["coverage"] or 0) >= 0.6025,
    }
    return {
        "multi_doc_pass": all(
            v for k, v in checks.items() if k.startswith("multi_doc")
        ),
        "all_pass": all(v for k, v in checks.items() if k.startswith("all_")),
        "checks": checks,
        "summary": {arm: summarize(rows, arm) for arm in ARMS},
    }


async def main_async(args) -> int:
    golden = load_golden()
    questions = stratified_sample(golden, args.sample, args.seed)
    print(f"抽样 {len(questions)} 问（seed={args.seed}），三 arm：{', '.join(ARMS)}")

    rows = await evaluate_arms(questions, args.top_k, args.concurrency, args.budget_cny)

    verdict = judge(rows)
    summary = verdict.pop("summary")
    out = {
        "meta": {
            "sample": len(questions),
            "seed": args.seed,
            "top_k": args.top_k,
            "arms": list(ARMS),
            "cost_cny_ragas": round(USAGE.cost_cny, 3),
        },
        "summary": summary,
        "verdict": verdict,
        "per_q": rows,
    }
    path = RESULTS_DIR / "synth_mode_ab.json"
    path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n===== 汇总（multi_doc / all / direct）=====")
    for arm in ARMS:
        s = summary[arm]
        print(
            f"{arm:10s} faith={s['faithfulness']} relev={s['answer_relevancy']} "
            f"cov={s['coverage']} | 多文档 faith={s['faithfulness_multi']} "
            f"cov={s['coverage_multi']} | 单文档 faith={s['faithfulness_single']} "
            f"relev={s['answer_relevancy_single']}"
        )
    print(f"判定：multi_doc_pass={verdict['multi_doc_pass']} all_pass={verdict['all_pass']}")
    print(f"裁判成本 ¥{USAGE.cost_cny:.3f}；产物 {path.name}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--budget-cny", type=float, default=2.0)
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except BudgetExceeded as exc:
        print(f"[停止] {exc}")
        return 3
    except RunAborted as exc:
        print(f"[中止] 网关致命错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
