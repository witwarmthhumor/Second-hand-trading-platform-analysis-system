"""生成侧对比：直出原文 vs LLM 合成（v1/v2 提示词），输出 Markdown 对比表。

用法（项目根目录）：
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/compare_gen.py

读取 run_gen_eval.py 产出的 results/gen_direct.json / gen_synth_v1.json / gen_synth_v2.json。
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# 展示顺序 = 列顺序
STRATEGIES: list[tuple[str, str]] = [
    ("direct", "直出原文（基线）"),
    ("synth_v1", "合成·正文内联来源"),
    ("synth_v2", "合成·来源末尾集中"),
]

ROWS: list[tuple[str, str]] = [
    ("faithfulness", "faithfulness（答案是否忠于片段）"),
    ("answer_relevancy", "answer_relevancy（是否答到点上）"),
    ("coverage", "来源覆盖度（覆盖应含文档比例）"),
    ("answer_len", "答案平均字数"),
    ("latency_s", "生成平均耗时(秒)"),
]

SUBSETS: list[tuple[str, str]] = [
    ("coverage_multi", "覆盖度 · 多文档问题"),
    ("coverage_single", "覆盖度 · 单文档问题"),
    ("answer_relevancy_multi", "相关性 · 多文档问题"),
    ("answer_relevancy_single", "相关性 · 单文档问题"),
    ("faithfulness_multi", "忠实度 · 多文档问题"),
    ("faithfulness_single", "忠实度 · 单文档问题"),
]


def load(strategy: str) -> dict | None:
    path = RESULTS / f"gen_{strategy}.json"
    if not path.exists():
        return None
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


def main() -> int:
    loaded = {name: load(name) for name, _ in STRATEGIES}
    present = {k: v for k, v in loaded.items() if v}
    if len(present) < 2:
        print("结果文件不足（至少需要 gen_direct.json + 一个 gen_synth_*.json）")
        return 1

    cols = [(k, label) for k, label in STRATEGIES if k in present]
    base_key = cols[0][0]

    print(f"## 总体对比（{present[base_key]['aggregate']['n']} 问，top_k={present[base_key]['top_k']}）\n")
    print("| 指标 | " + " | ".join(label for _, label in cols) + " | " + " | ".join(f"{label} 相对基线" for _, label in cols[1:]) + " |")
    print("|---" * (len(cols) + len(cols) - 1) + "|")
    for key, label in ROWS:
        cells = [fmt(present[c]["aggregate"].get(key)) for c, _ in cols]
        deltas = [delta(present[base_key]["aggregate"].get(key), present[c]["aggregate"].get(key)) for c, _ in cols[1:]]
        print(f"| {label} | " + " | ".join(cells) + " | " + " | ".join(deltas) + " |")

    print("\n## 分层对比（多文档 = 生成侧主要短板）\n")
    print("| 指标 | " + " | ".join(label for _, label in cols) + " |")
    print("|---" * len(cols) + "|")
    for key, label in SUBSETS:
        print(f"| {label} | " + " | ".join(fmt(present[c]["aggregate"].get(key)) for c, _ in cols) + " |")

    # 与基线的逐问翻转（以 answer_relevancy 计）
    if len(cols) > 1:
        print("\n## 逐问翻转（answer_relevancy，相对直出基线）\n")
        base_rows = {r["id"]: r for r in present[base_key]["per_q"]}
        for c, label in cols[1:]:
            better = worse = same = 0
            for r in present[c]["per_q"]:
                a = base_rows.get(r["id"], {}).get("direct", {}).get("answer_relevancy")
                b = r.get(c, {}).get("answer_relevancy")
                if a is None or b is None:
                    continue
                if b > a + 1e-6:
                    better += 1
                elif b < a - 1e-6:
                    worse += 1
                else:
                    same += 1
            print(f"- {label}：变好 {better} / 变差 {worse} / 持平 {same}")

    # 编造检查：合成答案里 faithfulness 明显偏低的样本
    for c, label in cols[1:]:
        low = [r for r in present[c]["per_q"] if (r.get(c, {}).get("faithfulness") or 1.0) < 0.6]
        print(f"\n## {label} faithfulness < 0.6 的样本（疑似编造）\n")
        if not low:
            print("- 无")
            continue
        for r in low:
            print(f"- {r['id']} faith={r[c]['faithfulness']:.2f} | {r['question'][:36]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
