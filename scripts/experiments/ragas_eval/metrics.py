"""检索质量指标：多文档覆盖率（coverage@k）、文档多样性、命中率。

动机（见 `RAGAS 评估实验.md` 第九节）：doc 级 Hit@5 只回答「期望文档里**至少有
一篇**进 top-5」，对多文档问题系统性偏乐观——它不惩罚「只中 1 篇、漏掉 4 篇」。
`coverage@k` 补上这个视角：top-k 命中的期望文档数 / 期望文档总数。

本模块被 `run_eval.py`（固化为默认输出的一等指标）与 `coverage_by_param.py`
（参数组横向对比）共用，**避免两处实现漂移**——两处口径不一致曾导致
「文档去重无效」的反向结论。

纯函数、无 IO、无外部依赖，便于单测。
"""

from __future__ import annotations

from collections.abc import Iterable

# 与生产 `ask()` 的 top_k 对齐：coverage@5 才是「用户实际看到的召回」
PRODUCTION_TOP_K = 5


def unique_ordered(ids: list[str]) -> list[str]:
    """按出现顺序去重（保序，便于看「哪篇排前面」）。"""
    return list(dict.fromkeys(ids))


def coverage_at_k(ranked_ids: list[str], expected: list[str], k: int) -> dict:
    """单个问题的覆盖率指标（doc 级去重口径）。

    返回：
    - ``coverage``：命中的期望文档数 / 期望文档总数（多文档的真实召回）；
    - ``distinct_docs``：top-k 里不同 doc_id 的个数（衡量「同文档多 chunk 抱团占位」）；
    - ``hit_any``：top-k 里是否至少命中一篇（= 传统 doc 级 Hit@k，对多文档偏乐观）；
    - ``is_multi``：期望文档是否多于一篇（决定聚合时归到多文档组还是单文档组）。

    ``expected`` 为空时 coverage 按 0 处理，不抛 ZeroDivisionError。
    """
    # ⚠️ 必须先截断 top-k、再去重计数。若反过来（去重后取前 k 个文档），
    # 输入列表越长 distinct 就越大——20 条候选里「去重后取 5」必然是 5 篇
    # 不同文档，会让 coverage@5 虚高（实测曾把 baseline 从 0.3755 抬到 0.4701）。
    uniq = unique_ordered(ranked_ids[:k])
    expected_set = set(expected)
    inter = len(set(uniq) & expected_set)
    return {
        "coverage": inter / len(expected) if expected else 0.0,
        "distinct_docs": len(uniq),
        "hit_any": int(inter > 0),
        "is_multi": len(expected) > 1,
    }


def aggregate(rows: Iterable[dict]) -> dict:
    """把逐问的 ``coverage_at_k`` 结果聚合成多/单文档分组指标。

    多文档与单文档**必须分开看**：多文档看 coverage（该中的都中了没），
    单文档 coverage 退化为命中率。合在一起会让两者互相掩盖。
    """
    multi_cov: list[float] = []
    single_cov: list[float] = []
    multi_distinct: list[float] = []
    hits: list[float] = []

    for row in rows:
        hits.append(float(row["hit_any"]))
        if row["is_multi"]:
            multi_cov.append(float(row["coverage"]))
            multi_distinct.append(float(row["distinct_docs"]))
        else:
            single_cov.append(float(row["coverage"]))

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "multi_n": len(multi_cov),
        "single_n": len(single_cov),
        "multi_coverage": _mean(multi_cov),
        "single_coverage": _mean(single_cov),
        "multi_distinct_docs": _mean(multi_distinct),
        "hit_any": _mean(hits),
    }
