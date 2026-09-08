"""检索质量评测：用 golden_queries.yaml（285 问）对建好的向量库做检索评测。

指标与 B 项目（RAGreflow-second-hand）同口径：Hit@5 / MRR@10 / nDCG@10。
golden 问题的 ``expected_ids`` 是期望命中的文档 id（如 A-001 / FAQ-001 / CASE-001）。

用法（在项目根目录）：
    backend/.venv/Scripts/python scripts/eval_retrieval.py

输出：控制台汇总 + docs/检索评测报告.md
"""

from __future__ import annotations

import math
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

# 允许直接运行或 -m 运行两种方式导入 app 包
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.rag.retriever import get_retriever

GOLDEN_PATH = ROOT / "rag_data" / "eval" / "golden_queries.yaml"
REPORT_PATH = ROOT / "docs" / "检索评测报告.md"

# 达标线（对齐 B 项目计划 10.5）
PASS_HIT5 = 0.90
PASS_MRR10 = 0.75
PASS_NDCG10 = 0.75


def _metrics(ranked_ids: list[str], expected: list[str]) -> dict:
    """单问指标：Hit@5、RR@10、nDCG@10（口径与 B 项目 _metrics 一致）。"""
    uniq = list(dict.fromkeys(ranked_ids))  # 同文档多 chunk 先去重
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


def _load_golden() -> list[dict]:
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["queries"]


def _doc_category(doc_id: str) -> str:
    """doc_id → 一级类目（用于分类目 Hit@5 统计）。"""
    prefix = doc_id.split("-")[0].upper()
    mapping = {
        "A": "平台规则制度",
        "B": "买家指南",
        "C": "卖家指南",
        "D": "验货质检手册",
        "E": "售后仲裁SOP",
        "CASE": "真实场景案例库",
        "NEWS": "公告与课程",
        "FAQ": "结构化FAQ",
    }
    return mapping.get(prefix, "未知")


def run() -> dict:
    retriever = get_retriever()
    if not retriever.is_ready:
        raise RuntimeError("向量库未就绪，请先运行 bash scripts/build_vector_store.sh")

    golden = _load_golden()
    n = len(golden)
    print(f"golden 共 {n} 问，开始检索评测...")

    hit5_total = 0
    rr_sum = 0.0
    ndcg_sum = 0.0
    cat_hit: Counter = Counter()
    cat_n: Counter = Counter()
    per_q: list[dict] = []

    start = time.time()
    for i, q in enumerate(golden, 1):
        ranked = retriever.search(q["question"], top_k=20)
        ranked_ids = [r["doc_id"] for r in ranked]
        expected = q["expected_ids"]
        m = _metrics(ranked_ids, expected)

        hit5_total += m["hit5"]
        rr_sum += m["rr"]
        ndcg_sum += m["ndcg"]

        # 分类目（按第一个 expected_id 归类，口径同 B 项目）
        cat = _doc_category(expected[0]) if expected else "未知"
        cat_hit[cat] += m["hit5"]
        cat_n[cat] += 1

        per_q.append({"q": q, "ranked": ranked_ids, "expected": expected, "m": m})
        if i % 50 == 0:
            print(f"  进度 {i}/{n}")

    elapsed = time.time() - start
    results = {
        "n": n,
        "hit5": hit5_total / n,
        "mrr10": rr_sum / n,
        "ndcg10": ndcg_sum / n,
        "elapsed": elapsed,
        "cat": {c: cat_hit[c] / cat_n[c] for c in sorted(cat_n)},
        "cat_n": dict(cat_n),
        "per_q": per_q,
    }
    return results


def _write_report(results: dict) -> Path:
    n = results["n"]
    hit5, mrr10, ndcg10 = results["hit5"], results["mrr10"], results["ndcg10"]
    lines = [
        "# 检索评测报告（二手交易知识库问答）",
        "",
        "对建好的向量库做检索质量评测，复用 RAGreflow-second-hand 的",
        "`golden_queries.yaml`（285 问），指标口径与源项目一致。",
        "",
        f"- 评测问数：{n}",
        "- embedding：bge-m3（1024 维）",
        "- chunk_size：900（重叠 10%）",
        "- 检索：向量 + BM25 + RRF(k=60) 混合",
        f"- 耗时：{results['elapsed']:.1f}s",
        "",
        "## 总指标",
        "",
        "| 指标 | 值 | 达标线 | 是否达标 |",
        "|------|-----|--------|----------|",
        f"| Hit@5 | {hit5:.4f} | ≥ {PASS_HIT5} | {'✅' if hit5 >= PASS_HIT5 else '❌'} |",
        f"| MRR@10 | {mrr10:.4f} | ≥ {PASS_MRR10} | {'✅' if mrr10 >= PASS_MRR10 else '❌'} |",
        f"| nDCG@10 | {ndcg10:.4f} | ≥ {PASS_NDCG10} | {'✅' if ndcg10 >= PASS_NDCG10 else '❌'} |",
        "",
        "## 每个一级类目 Hit@5",
        "",
        "| 类目 | Hit@5 | 问数 | 达标(≥0.85) |",
        "|------|-------|------|-------------|",
    ]
    for cat in sorted(results["cat"]):
        v = results["cat"][cat]
        cnt = results["cat_n"].get(cat, 0)
        lines.append(f"| {cat} | {v:.4f} | {cnt} | {'✅' if v >= 0.85 else '❌'} |")

    lines += [
        "",
        "> **同源偏差声明**：golden 问题与语料出自同一生成流程，评测结果天然偏乐观。",
        "> 达标线参考 B 项目计划 10.5：混合检索 Hit@5 ≥ 0.90、MRR@10 ≥ 0.75。",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT_PATH


def main() -> int:
    results = run()
    n = results["n"]
    print("\n" + "=" * 50)
    print(f"评测完成（{n} 问，耗时 {results['elapsed']:.1f}s）")
    print(f"  Hit@5   = {results['hit5']:.4f}  (达标线 ≥ {PASS_HIT5})")
    print(f"  MRR@10  = {results['mrr10']:.4f}  (达标线 ≥ {PASS_MRR10})")
    print(f"  nDCG@10 = {results['ndcg10']:.4f}  (达标线 ≥ {PASS_NDCG10})")
    print("\n每个一级类目 Hit@5：")
    for cat in sorted(results["cat"]):
        print(f"  {cat}: {results['cat'][cat]:.4f}")
    out = _write_report(results)
    print(f"\n报告已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
