"""兜底档位探针：验证「拒答档」边界（P1 验收）。

背景：兜底路径现在分三档（见 ``docs/RAG优化方案.md`` 第五节）——
    top1 ≥ SIMILARITY_THRESHOLD         → 命中直出（原行为）
    FALLBACK_CONTEXT_MIN_SIM ≤ top1 < 阈值 → 注入临界片段后调 LLM
    top1 < FALLBACK_CONTEXT_MIN_SIM      → 直接拒答，**不调 LLM**

本脚本测两件事：
1. **误伤面**：领域内 golden 问题里，有多少条会因相似度低于下限而被拒答
   （理想值应为 0——领域内问题本就不该被拒答）；
2. **档位分布**：一批领域外问题各自落在哪个档（拒答 / 注入 / 命中直出）。

用法::

    backend/.venv/Scripts/python scripts/experiments/ragas_eval/fallback_gate_probe.py
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/fallback_gate_probe.py --min-sim 0.45
    backend/.venv/Scripts/python scripts/experiments/ragas_eval/fallback_gate_probe.py --limit 50

输出：控制台表格 + ``results/fallback_gate_probe.json``。
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

from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

from app.rag.config import SIMILARITY_THRESHOLD
from app.rag.retriever import get_retriever

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 领域外问题样本（闲聊/通用知识，与二手交易知识库无关）。
# 用于确认兜底档位：应落到「拒答」或「注入」，不应「命中直出」无关原文。
OUT_OF_DOMAIN = [
    "今天天气怎么样",
    "你叫什么名字",
    "帮我写一首诗",
    "1+1等于几",
    "推荐一部电影",
    "Python怎么读取文件",
    "北京到上海高铁多久",
    "红烧肉怎么做",
    "明天股市会涨吗",
    "特朗普是谁",
    "给我讲个笑话",
    "怎么减肥比较快",
    "怎么学好英语口语",
    "推荐几本科幻小说",
    "iPhone 和安卓哪个好",
]


def _gate(v: float, min_sim: float, hit_threshold: float) -> str:
    """把 top1 相似度映射到档位名。"""
    if v < min_sim:
        return "拒答"
    if v < hit_threshold:
        return "注入"
    return "命中直出"


def main() -> int:
    ap = argparse.ArgumentParser(description="兜底档位探针（P1 验收）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条 golden（调试用）")
    ap.add_argument(
        "--min-sim",
        type=float,
        default=None,
        help="覆盖 FALLBACK_CONTEXT_MIN_SIM（扫描不同下限的误伤面）",
    )
    ap.add_argument("--out", default="fallback_gate_probe.json", help="输出文件名")
    args = ap.parse_args()

    from app.rag.config import FALLBACK_CONTEXT_MIN_SIM as CFG_MIN_SIM

    min_sim = args.min_sim if args.min_sim is not None else CFG_MIN_SIM
    hit_threshold = SIMILARITY_THRESHOLD

    retriever = get_retriever()
    if not retriever.is_ready:
        print("向量库未就绪，请先运行建库脚本。")
        return 1

    golden = load_golden()
    if args.limit:
        golden = golden[: args.limit]

    print(f"命中阈值={hit_threshold} | 兜底下限={min_sim} | golden {len(golden)} 问")
    print()

    t0 = time.time()

    # 1) 误伤面：领域内问题被拒答档截住的数量（理想 0）
    in_domain_rows: list[dict] = []
    for q in golden:
        results = retriever.search(q["question"], 5)
        top1 = results[0]["vector_sim"] if results else 0.0
        in_domain_rows.append(
            {"question": q["question"], "top1": round(top1, 4), "gate": _gate(top1, min_sim, hit_threshold)}
        )

    rejected = [r for r in in_domain_rows if r["gate"] == "拒答"]
    injected = [r for r in in_domain_rows if r["gate"] == "注入"]
    hit = [r for r in in_domain_rows if r["gate"] == "命中直出"]

    print("【误伤面】领域内问题档位分布")
    print(f"  命中直出 {len(hit)} | 注入 {len(injected)} | 拒答 {len(rejected)}  (共 {len(in_domain_rows)})")
    if rejected:
        print("  ⚠ 被拒答的领域内问题（应尽量为 0）：")
        for r in rejected[:10]:
            print(f"    {r['top1']:.4f}  {r['question']}")
    print()

    # 2) 领域外问题：档位分布
    out_rows: list[dict] = []
    for q in OUT_OF_DOMAIN:
        results = retriever.search(q, 5)
        top1 = results[0]["vector_sim"] if results else 0.0
        out_rows.append({"question": q, "top1": round(top1, 4), "gate": _gate(top1, min_sim, hit_threshold)})

    print("【档位分布】领域外问题 top1 相似度")
    for r in sorted(out_rows, key=lambda x: -x["top1"]):
        print(f"  {r['top1']:.4f} [{r['gate']}] {r['question']}")

    dist: dict[str, int] = {}
    for r in out_rows:
        dist[r["gate"]] = dist.get(r["gate"], 0) + 1
    print(f"  小计：{dist}")
    print()

    elapsed = time.time() - t0
    print(f"耗时 {elapsed:.1f}s")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "hit_threshold": hit_threshold,
        "fallback_min_sim": min_sim,
        "in_domain_total": len(in_domain_rows),
        "in_domain_gate": {"hit": len(hit), "inject": len(injected), "reject": len(rejected)},
        "in_domain_rejected": rejected,
        "out_of_domain": out_rows,
        "out_of_domain_gate": dist,
        "elapsed_s": round(elapsed, 1),
    }
    out_path = RESULTS_DIR / args.out
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
