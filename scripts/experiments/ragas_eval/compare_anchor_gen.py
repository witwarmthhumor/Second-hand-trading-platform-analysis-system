"""锚定文档名额 A/B：同一批问题、同一提示词，只改 `ANCHOR_QUOTA` 的生成侧对照。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare_anchor_gen.py \
        --base results/gen_synth_v1_aq1.json \
        --cand results/gen_synth_v1_aq4.json

读取 run_gen_eval.py 的产出（`{strategy, aggregate, per_q}`）。与 ``compare_gen.py``
的区别：本脚本对比的是「**检索策略**」而非「生成提示词」——两份结果用同一提示词
（默认 v1）、同一批问题（`--seed` 相同），唯一变量是检索层保底名额分配，因此
差异可归因到检索侧。

除常规生成指标外，另打印**检索组成诊断**（不依赖 LLM，直接由 per_q 的
`retrieved_ids` 算出），用于验证「锚定名额确实生效」：
- `distinct_docs`：top-5 里不同文档数（广度）
- `anchor_share`：top-5 里属于 top1 文档的块数（深度）
若两者没按预期变化，说明 A/B 的有效性存疑，不该采信生成指标差异。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

ROWS: list[tuple[str, str]] = [
    ("faithfulness", "faithfulness（答案是否忠于片段）"),
    ("answer_relevancy", "answer_relevancy（是否答到点上）"),
    ("coverage", "来源覆盖度（覆盖应含文档比例）"),
    ("answer_len", "答案平均字数"),
    ("latency_s", "生成平均耗时(秒)"),
]

SUBSETS: list[tuple[str, str]] = [
    ("faithfulness_multi", "忠实度 · 多文档问题"),
    ("faithfulness_single", "忠实度 · 单文档问题"),
    ("answer_relevancy_multi", "相关性 · 多文档问题"),
    ("answer_relevancy_single", "相关性 · 单文档问题"),
    ("coverage_multi", "覆盖度 · 多文档问题"),
    ("coverage_single", "覆盖度 · 单文档问题"),
]

FLIPS: list[str] = ["faithfulness", "answer_relevancy", "coverage"]


def load(name: str) -> dict:
    path = RESULTS / name
    if not path.exists():
        raise SystemExit(f"结果文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def delta(base, new) -> str:
    if base is None or new is None:
        return "—"
    d = new - base
    arrow = "↑" if d > 1e-9 else ("↓" if d < -1e-9 else "＝")
    return f"{arrow}{d:+.4f}"


def composition(payload: dict) -> dict:
    """由 per_q 的 retrieved_ids 算检索组成：广度（distinct）与深度（anchor_share）。"""
    distinct: list[int] = []
    anchor_share: list[float] = []
    for r in payload["per_q"]:
        ids = r.get("retrieved_ids") or []
        if not ids:
            continue
        distinct.append(len(set(ids)))
        anchor = ids[0]
        anchor_share.append(sum(1 for x in ids if x == anchor) / len(ids))
    n = len(distinct) or 1
    return {
        "distinct_docs": round(sum(distinct) / n, 3),
        "anchor_share": round(sum(anchor_share) / n, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="gen_synth_v1_aq1.json", help="基线（默认配额 1）")
    ap.add_argument("--cand", default="gen_synth_v1_aq4.json", help="候选（默认配额 4）")
    args = ap.parse_args()

    base, cand = load(args.base), load(args.cand)
    kb, kc = base["strategy"], cand["strategy"]
    n_base, n_cand = base["aggregate"]["n"], cand["aggregate"]["n"]
    if n_base != n_cand:
        print(f"!! 样本数不一致（{n_base} vs {n_cand}），对比仅供参考\n")

    print(f"## 生成侧 A/B（{n_base} 问，top_k={base['top_k']}，策略键 {kb}）\n")
    print(f"- 基线：`{args.base}`\n- 候选：`{args.cand}`\n")
    print("| 指标 | 基线 | 候选 | 候选相对基线 |")
    print("|---|---|---|---|")
    for key, label in ROWS:
        a, b = base["aggregate"].get(key), cand["aggregate"].get(key)
        print(f"| {label} | {fmt(a)} | {fmt(b)} | {delta(a, b)} |")

    print("\n### 分层（多文档 = 生成侧主要短板）\n")
    print("| 指标 | 基线 | 候选 | 候选相对基线 |")
    print("|---|---|---|---|")
    for key, label in SUBSETS:
        a, b = base["aggregate"].get(key), cand["aggregate"].get(key)
        print(f"| {label} | {fmt(a)} | {fmt(b)} | {delta(a, b)} |")

    print("\n### 检索组成诊断（不依赖 LLM；用于验证锚定名额是否真的生效）\n")
    cb, cc = composition(base), composition(cand)
    print("| 组成指标 | 基线 | 候选 | 说明 |")
    print("|---|---|---|---|")
    print(
        f"| distinct_docs（top-5 不同文档数） | {cb['distinct_docs']} | "
        f"{cc['distinct_docs']} | 广度：候选应**下降** |"
    )
    print(
        f"| anchor_share（top1 文档占比） | {cb['anchor_share']} | "
        f"{cc['anchor_share']} | 深度：候选应**上升** |"
    )

    print("\n### 逐问翻转（候选 vs 基线）\n")
    base_rows = {r["id"]: r for r in base["per_q"]}
    for key in FLIPS:
        better = worse = same = missing = 0
        for r in cand["per_q"]:
            a = base_rows.get(r["id"], {}).get(kb, {}).get(key)
            b = r.get(kc, {}).get(key)
            if a is None or b is None:
                missing += 1
            elif b > a + 1e-6:
                better += 1
            elif b < a - 1e-6:
                worse += 1
            else:
                same += 1
        tail = f"，缺失 {missing}" if missing else ""
        print(f"- {key}：变好 {better} / 变差 {worse} / 持平 {same}{tail}")

    print("\n### 候选组 faithfulness 明显偏低的样本（< 0.6，疑似编造）\n")
    low = [r for r in cand["per_q"] if (r.get(kc, {}).get("faithfulness") or 1.0) < 0.6]
    if not low:
        print("- 无")
    else:
        for r in low:
            print(f"- {r['id']} faith={r[kc]['faithfulness']:.2f} | {r['question'][:36]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
