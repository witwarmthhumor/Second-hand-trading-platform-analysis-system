"""生成侧评估主脚本：对比「直出原文」与「LLM 合成答案」（含提示词消融）。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_gen_eval.py
        [--sample 40] [--seed 42] [--top-k 5] [--concurrency 3]
        [--synth-styles v1,v2] [--skip-direct] [--resume | --no-resume]

评估指标（RAGAS，均为 LLM 评判）：
- ``faithfulness``：答案里的断言能否被检索片段支撑（1.0=完全有据，0=编造）。
  注意：直出原文的答案本身就是片段原文，其 faithfulness 天然接近 1.0，
  与合成答案对比时该指标对基线有利（天花板效应），需在归因中说明。
- ``answer_relevancy``：由答案反推问题，看与原始问题的语义一致性（是否答到点上）。

另有两条不依赖 LLM 的诊断指标：
- ``coverage``：答案实际覆盖了 expected 文档的比例（多文档聚合问题的关键）。
- ``answer_len`` / ``latency``：答案长度与生成耗时。

输出：results/gen_direct.json（基线）+ results/gen_synth_{style}.json（优化）。
直出结果已存在时可用 --skip-direct 复用（检索走 Redis 缓存，结果可对齐）。

健壮性（LLM 网关欠费/限流时不再白跑一轮）：
- **逐问落盘**：每问结束即写断点 ``results/gen_ckpt_*.json``（临时文件 + 原子替换）。
- **断点续跑**：加 ``--resume`` 跳过已完成的问题，只补跑缺的；``--no-resume`` 强制重跑。
- **致命即停**：网关 401/402/403（欠费/鉴权）立即中止，不再做无谓重试；
  已完成的逐问结果照常写出，部分样例汇总仍可查看。
- **成本闸门**：``--budget-cny``（默认 ¥2）按 token 实时折算花费，**在开跑下一问之前**
  检查，超限即停，不会"先花钱再报错"；单价取高峰价属保守上限，实际花费更低。
- 退出码：0=完整跑完（自动清理断点）；2=被网关中断；3=触达成本闸门（断点均保留，可续跑）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

from common import load_golden
from gen_answers import answer_direct, answer_synth, parse_citations
from llm_common import (
    DEFAULT_BUDGET_CNY,
    USAGE,
    BudgetExceeded,
    LLMFatalError,
    build_judge_embeddings,
    build_judge_llm,
    is_fatal_llm_error,
)
from ragas import SingleTurnSample
from ragas.metrics import AnswerRelevancy, Faithfulness

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.rag.retriever import get_retriever
from app.services import rag_service

RESULTS_DIR = Path(__file__).resolve().parent / "results"


class RunAborted(RuntimeError):
    """LLM 网关致命错误（欠费/鉴权）导致整轮中止；已完成的逐问结果仍会落盘。"""


# ---------------------------------------------------------------------------
# 抽样与检索
# ---------------------------------------------------------------------------


def stratified_sample(golden: list[dict], n: int, seed: int = 42) -> list[dict]:
    """分层抽样：单文档 / 多文档各占一半（多文档是生成侧的难点所在）。"""
    single = [q for q in golden if len(q["expected_ids"]) == 1]
    multi = [q for q in golden if len(q["expected_ids"]) > 1]
    rnd = random.Random(seed)
    rnd.shuffle(single)
    rnd.shuffle(multi)
    n_multi = min(len(multi), n // 2)
    n_single = min(len(single), n - n_multi)
    return sorted(single[:n_single] + multi[:n_multi], key=lambda q: q["id"])


async def retrieve(question: str, top_k: int, retriever) -> list[dict]:
    """检索：优先走生产 `rag_service.retrieve`（含缓存/singleflight），失败回退直调。"""
    try:
        return await rag_service.retrieve(question, top_k=top_k, retriever=retriever)
    except Exception as exc:  # noqa: BLE001 缓存依赖 Redis，不可用时回退纯检索
        print(f"    [warn] rag_service.retrieve 失败（{type(exc).__name__}），回退直调检索")
        return await asyncio.to_thread(retriever.search, question, top_k)


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


async def score(
    metric, sample: SingleTurnSample, sem: asyncio.Semaphore, attempts: int = 3
) -> float | None:
    """单条打分；裁判偶发解析失败时退避重试，仍失败返回 None（统计时按缺失处理）。

    但鉴权/欠费类错误（``is_fatal_llm_error``）不重试，直接上抛——
    否则网关欠费时会把整轮 40 问全部静默打成 None，白白消耗一次运行。
    """
    async with sem:
        for i in range(attempts):
            try:
                return float(await metric.single_turn_ascore(sample))
            except Exception as exc:  # 抖动重试，致命错误上抛
                if is_fatal_llm_error(exc):
                    raise LLMFatalError(str(exc)[:200]) from exc
                if i < attempts - 1:
                    await asyncio.sleep(min(2**i, 6))
    return None


def coverage(answer_docs: list[str], expected: list[str]) -> float:
    """答案实际覆盖的 expected 文档比例。"""
    if not expected:
        return 0.0
    hit = len({d.upper() for d in answer_docs} & {e.upper() for e in expected})
    return hit / len(expected)


def load_existing(strategy: str) -> dict[str, dict]:
    """读取已有结果文件的逐问明细（按 id 索引），供 --skip-direct 复用。"""
    path = RESULTS_DIR / f"gen_{strategy}.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in payload["per_q"]}


# ---------------------------------------------------------------------------
# 断点续跑：每问落盘一次，网关抖动/欠费中断后不丢已完成的工作
# ---------------------------------------------------------------------------


def ckpt_path(sample: int, styles: list[str], skip_direct: bool, seed: int) -> Path:
    """断点文件路径（按运行配置区分，避免不同配置互相覆盖）。"""
    key = "+".join(styles) + ("_nodirect" if skip_direct else "")
    if not key:
        key = "directonly"  # --skip-synth：只跑直出基线
    return RESULTS_DIR / f"gen_ckpt_s{sample}_{seed}_{key}.json"


def load_ckpt(path: Path) -> dict[str, dict]:
    """读取断点，返回 {id: row}；文件缺失或损坏时返回空。"""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"    [warn] 断点文件损坏，忽略：{path.name}")
        return {}
    return {r["id"]: r for r in payload.get("rows", [])}


def save_ckpt(path: Path, rows: list[dict], meta: dict) -> None:
    """写断点（先写临时文件再原子替换，避免中断时留下半截 JSON）。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def evaluate(
    questions: list[dict],
    top_k: int,
    concurrency: int,
    styles: list[str],
    skip_direct: bool,
    ckpt: Path,
    resume: bool,
    budget_cny: float,
) -> list[dict]:
    """逐问：检索 → 基线/各风格生成 → 打分，返回逐问明细（策略键为 direct/synth_{style}）。

    每问结束即写断点；网关欠费/鉴权失败时抛 ``RunAborted``，
    预估花费超过 ``budget_cny`` 时抛 ``BudgetExceeded``——两条路径都已完成的
    部分都不会丢失，且都在**开跑下一问之前**就中止，不会先花钱再报错。
    """
    retriever = get_retriever()
    if not retriever.is_ready:
        raise RuntimeError("向量库未就绪，请先运行 scripts/build_vector_store.py")

    done = load_ckpt(ckpt) if resume else {}
    if done:
        print(f"  断点续跑：已有 {len(done)} 问完成，本轮跳过")

    llm = build_judge_llm()
    emb = build_judge_embeddings()
    # strictness=1：answer_relevancy 每个样本要生成 strictness 个问题（n=strictness），
    # 而当前网关忽略 n>1，返回的 generations 长度不足会 IndexError。
    # strictness=1 等价于「只生成一个问题」，指标含义不变（官方默认 3 只为降方差）。
    metrics: dict[str, tuple] = {}
    if not skip_direct:
        metrics["direct"] = (
            Faithfulness(llm=llm),
            AnswerRelevancy(llm=llm, embeddings=emb, strictness=1),
        )
    for s in styles:
        metrics[f"synth_{s}"] = (
            Faithfulness(llm=llm),
            AnswerRelevancy(llm=llm, embeddings=emb, strictness=1),
        )

    cached_direct = load_existing("direct") if skip_direct else {}
    sem_gen = asyncio.Semaphore(concurrency)
    sem_score = asyncio.Semaphore(concurrency)
    meta = {
        "styles": styles,
        "top_k": top_k,
        "skip_direct": skip_direct,
        "sample": len(questions),
    }
    rows: list[dict] = []

    for idx, q in enumerate(questions, 1):
        if q["id"] in done:
            rows.append(done[q["id"]])
            continue

        # 预算闸门：在开始下一问（即产生新花费）之前检查，超预算立即停并落盘
        if USAGE.cost_cny > budget_cny:
            save_ckpt(ckpt, rows, meta)
            raise BudgetExceeded(f"{USAGE.summary()}，已超预算 ¥{budget_cny:.2f}")

        t0 = time.time()
        try:
            results = await retrieve(q["question"], top_k, retriever)
            expected = q["expected_ids"]
            ctx_texts = [r["text"] for r in results]
            row: dict = {
                "id": q["id"],
                "category": q.get("category", ""),
                "question": q["question"],
                "expected_ids": expected,
                "retrieved_ids": [r["doc_id"] for r in results],
                "n_expected": len(expected),
            }

            jobs = []
            if not skip_direct:
                text = answer_direct(results)
                jobs.append(("direct", text, 0.0, [results[0]["doc_id"]] if results else []))
            else:
                old = cached_direct.get(q["id"])
                if old:
                    row["direct"] = old["direct"]

            for s in styles:
                async with sem_gen:
                    text, latency = await answer_synth(q["question"], results, s)
                cited = parse_citations(text) or [r["doc_id"] for r in results]
                jobs.append((f"synth_{s}", text, latency, cited))

            for (key, text, latency, cited) in jobs:
                faith, relev = metrics.get(key, (None, None))
                f_v, r_v = await asyncio.gather(
                    score(faith, SingleTurnSample(user_input=q["question"], response=text, retrieved_contexts=ctx_texts), sem_score)
                    if faith
                    else _none(),
                    score(relev, SingleTurnSample(user_input=q["question"], response=text, retrieved_contexts=ctx_texts), sem_score)
                    if relev
                    else _none(),
                )
                row[key] = {
                    "answer": text,
                    "faithfulness": f_v,
                    "answer_relevancy": r_v,
                    "coverage": coverage(cited, expected),
                    "answer_len": len(text),
                    "latency": round(latency, 2),
                    "cited": cited,
                }
        except LLMFatalError as exc:
            save_ckpt(ckpt, rows, meta)
            raise RunAborted(str(exc)) from exc

        rows.append(row)
        save_ckpt(ckpt, rows, meta)
        brief = " ".join(
            f"{k}:{row[k]['faithfulness']}/{row[k]['answer_relevancy']}" for k in metrics if k in row
        )
        print(
            f"  [{idx}/{len(questions)}] {q['id']} 期望{len(expected)}篇 | {brief} "
            f"| {time.time() - t0:.1f}s | 累计 ¥{USAGE.cost_cny:.3f}",
            flush=True,
        )
    return rows


async def _none() -> None:
    """占位协程：某策略被跳过时保持 gather 结构一致。"""
    return


def summarize(rows: list[dict], key: str) -> dict:
    """汇总某一策略的均值指标（缺失值不计入分母，另记有效样本数）。"""

    def mean(field: str) -> float | None:
        vals = [r[key][field] for r in rows if key in r and r[key].get(field) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def mean_subset(field: str, multi: bool) -> float | None:
        vals = [
            r[key][field]
            for r in rows
            if key in r and (r["n_expected"] > 1) == multi and r[key].get(field) is not None
        ]
        return round(sum(vals) / len(vals), 4) if vals else None

    present = [r for r in rows if key in r]
    scored = [r for r in present if r[key].get("faithfulness") is not None]
    return {
        "n": len(present),
        "n_scored": len(scored),
        "faithfulness": mean("faithfulness"),
        "answer_relevancy": mean("answer_relevancy"),
        "coverage": mean("coverage"),
        "answer_len": mean("answer_len"),
        "latency_s": round(sum(r[key].get("latency") or 0 for r in present) / len(present), 2)
        if present
        else None,
        # 分层：多文档问题是生成侧的主要短板
        "faithfulness_multi": mean_subset("faithfulness", True),
        "faithfulness_single": mean_subset("faithfulness", False),
        "answer_relevancy_multi": mean_subset("answer_relevancy", True),
        "answer_relevancy_single": mean_subset("answer_relevancy", False),
        "coverage_multi": mean_subset("coverage", True),
        "coverage_single": mean_subset("coverage", False),
    }


def write_outputs(rows: list[dict], args, styles: list[str], planned: int) -> None:
    """把逐问明细按策略写成结果文件（direct / synth_*），供 compare_gen.py 读取。

    空结果不写盘——避免中止在首问之前时用空文件覆盖掉已有数据。
    """
    if not rows:
        print("   没有已完成的问题，跳过写盘（保护已有结果文件）")
        return
    keys = ([] if args.skip_direct else ["direct"]) + [f"synth_{s}" for s in styles]
    for key in keys:
        payload = {
            "strategy": key,
            "sample": len(rows),
            "sample_planned": planned,
            "aborted": len(rows) < planned,
            "seed": args.seed,
            "top_k": args.top_k,
            "aggregate": summarize(rows, key),
            "per_q": rows,
        }
        path = RESULTS_DIR / f"gen_{key}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 {path.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40, help="抽样问数（单/多文档各半）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top-k", type=int, default=5, help="与生产 ask() 一致")
    ap.add_argument("--concurrency", type=int, default=3, help="并发（过高易触网关限流）")
    ap.add_argument("--synth-styles", default="v1", help="合成提示词风格，逗号分隔（v1/v2）")
    ap.add_argument("--skip-direct", action="store_true", help="复用已有 gen_direct.json，不重跑基线")
    ap.add_argument("--skip-synth", action="store_true", help="只跑直出原文基线，不做 LLM 合成")
    ap.add_argument("--resume", action="store_true", help="从断点续跑，跳过已完成的问")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点，从头重跑")
    ap.add_argument(
        "--budget-cny",
        type=float,
        default=DEFAULT_BUDGET_CNY,
        help=f"预估花费上限（人民币元，默认 {DEFAULT_BUDGET_CNY}），超过即中止并保留断点",
    )
    args = ap.parse_args()

    styles = [] if args.skip_synth else [s.strip() for s in args.synth_styles.split(",") if s.strip()]
    if args.skip_synth and args.skip_direct:
        print("!! --skip-synth 与 --skip-direct 同时指定：没有任何要跑的项目")
        return 1
    golden = load_golden()
    questions = stratified_sample(golden, args.sample, args.seed)
    n_multi = sum(1 for q in questions if len(q["expected_ids"]) > 1)
    print(
        f"生成侧评估：抽样 {len(questions)} 问（多文档 {n_multi} / 单文档 "
        f"{len(questions) - n_multi}），top_k={args.top_k}，并发={args.concurrency}，"
        f"合成风格={styles}"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ckpt = ckpt_path(args.sample, styles, args.skip_direct, args.seed)
    resume = args.resume and not args.no_resume

    t0 = time.time()
    USAGE.reset()  # 本轮独立计费
    rows: list[dict] = []
    aborted_msg = ""
    budget_hit = False
    try:
        rows = asyncio.run(
            evaluate(
                questions,
                args.top_k,
                args.concurrency,
                styles,
                args.skip_direct,
                ckpt,
                resume,
                args.budget_cny,
            )
        )
    except BudgetExceeded as exc:
        aborted_msg = f"已达成本闸门：{exc}"
        budget_hit = True
        rows = list(load_ckpt(ckpt).values())
    except RunAborted as exc:
        aborted_msg = str(exc)
        rows = list(load_ckpt(ckpt).values())
    except KeyboardInterrupt:
        aborted_msg = "用户中断"
        rows = list(load_ckpt(ckpt).values())
    print(f"\n总耗时 {time.time() - t0:.1f}s | 本轮花费 {USAGE.summary()}")

    if aborted_msg:
        if budget_hit:
            print(
                f"\n!! 评估因【成本闸门】中止：{aborted_msg}\n"
                f"   预算上限 ¥{args.budget_cny:.2f} 已用满，未继续产生花费。\n"
                "   已完成的逐问结果保留在断点文件中，确认后可加 --resume 继续。"
            )
        else:
            print(
                f"\n!! 评估中止：{aborted_msg}\n"
                "   已完成的逐问结果保留在断点文件中，网关恢复后加 --resume 可继续。\n"
                "   下面按已完成的部分样例出汇总（样本不足，仅供参考）。"
            )

    write_outputs(rows, args, styles, planned=len(questions))

    for key in ([] if args.skip_direct else ["direct"]) + [f"synth_{s}" for s in styles]:
        agg = summarize(rows, key)
        print(
            f"  {key:12s} n={agg['n']} faithfulness={agg['faithfulness']} "
            f"relevancy={agg['answer_relevancy']} coverage={agg['coverage']} len={agg['answer_len']}"
        )

    if aborted_msg:
        return 3 if budget_hit else 2
    ckpt.unlink(missing_ok=True)  # 完整跑完则清理断点
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
