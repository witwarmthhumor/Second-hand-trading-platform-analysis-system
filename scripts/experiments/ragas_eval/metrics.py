"""检索质量指标：多文档覆盖率（coverage@k）、文档多样性、命中率。

动机（见 `RAGAS 评估实验.md` 第九节）：doc 级 Hit@5 只回答「期望文档里**至少有
一篇**进 top-5」，对多文档问题系统性偏乐观——它不惩罚「只中 1 篇、漏掉 4 篇」。
`coverage@k` 补上这个视角：top-k 命中的期望文档数 / 期望文档总数。

本模块被 `run_eval.py`（固化为默认输出的一等指标）与 `coverage_by_param.py`
（参数组横向对比）共用，**避免两处实现漂移**——两处口径不一致曾导致
「文档去重无效」的反向结论。

另提供 **chunk 级** 口径（``chunk_coverage_at_k`` / ``aggregate_chunks``）：
doc 级只要命中某篇文档的任意一个 chunk 就算覆盖整篇（而一篇平均 9.46 块），
对「文档内的内容覆盖」系统性高估——chunk 级补上这个视角。

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


# ---------------------------------------------------------------------------
# chunk 级覆盖率：把 doc 级口径从「文档」细化到「块」
# ---------------------------------------------------------------------------


def chunk_coverage_at_k(
    ranked_chunk_ids: list[str],
    expected_chunk_ids: list[str],
    k: int,
) -> dict:
    """单个问题的 **chunk 级** 覆盖率（doc 级口径的内容细化版）。

    动机：``coverage_at_k`` 的分母是「期望文档数」，某篇文档只要有**任意一个**
    chunk 进 top-k 就算整篇覆盖。但一篇文档平均切成 9.46 块（MD 6~8 块、
    FAQ 20~25 块），「命中 1 块」远不等于「拿到该文档的内容」。chunk 级口径
    才能回答「文档内的内容覆盖了多少」，也才能解释「检索层文档覆盖已到 0.5，
    而生成侧 coverage 仍只有 0.40」——进对了文档，却只带了一小块上下文。

    ⚠️ 与 ``coverage_at_k`` 同样的坑：必须 **先截断 top-k、再计数**。若先对
    期望集合去重再与长候选列表求交，输入越长命中越多，会凭空虚高。

    返回：
    - ``recall``：命中块数 / 期望块总数 —— **内容覆盖率**（天花板受名额限制）；
    - ``recall_norm``：命中块数 / min(k, 期望块总数) —— **名额内召回效率**（≤1），
      消除「文档越大天花板越低」的规模干扰，可跨文档规模横向比较；
    - ``precision``：命中块数 / k —— top-k 里有多少块属于**目标文档**（有效上下文占比）；
    - ``coverable``：min(k, 期望块总数) / 期望块总数 —— recall 的理论上界（供解读：
      低于它说明还有名额内本可召回的块没召回）；
    - ``n_expected``：期望块总数（聚合分箱用）；
    - ``hit1``：top-1 是否落在期望块内 —— 即直出路径「给用户的第一个片段是不是
      目标文档的段落」。它是 doc 级 ``hit_any`` 的下界（top-1 属期望文档
      ⟹ 期望文档必在 top-k 中），可作为交叉校验。
    """
    uniq = unique_ordered(ranked_chunk_ids[:k])
    expected_set = set(expected_chunk_ids)
    inter = len(set(uniq) & expected_set)
    n_exp = len(expected_chunk_ids)
    cap = min(k, n_exp)
    top1 = ranked_chunk_ids[0] if ranked_chunk_ids else None
    return {
        "recall": inter / n_exp if n_exp else 0.0,
        "recall_norm": inter / cap if cap else 0.0,
        "precision": inter / k if k else 0.0,
        "coverable": cap / n_exp if n_exp else 0.0,
        "n_expected": n_exp,
        "hit1": int(top1 in expected_set) if top1 else 0,
    }


# 期望块数分箱：文档越大，top-k 能覆盖的比例天然越低，必须分箱看
CHUNK_SIZE_BINS: tuple[tuple[str, int, int], ...] = (
    ("小(≤6块)", 1, 6),
    ("中(7~12块)", 7, 12),
    ("大(>12块)", 13, 10**9),
)


def _mean_of(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate_chunks(rows: Iterable[dict]) -> dict:
    """把逐问 ``chunk_coverage_at_k`` 聚合成「多/单文档」+「期望块数分箱」两组。

    分组维度：
    - **overall / multi / single**：与 doc 级口径对齐，便于互相对照
      （rows 需带 ``is_multi`` 键，由调用方按期望文档数 > 1 补齐）；
    - **by_size（小/中/大）**：按期望块总数分箱，看「文档越大覆盖越差」的规律
      （FAQ 一篇 20~25 块，是拉低整体内容覆盖率的主要来源）。
    """

    def _summ(items: list[dict]) -> dict:
        return {
            "n": len(items),
            "recall": _mean_of([float(r["recall"]) for r in items]),
            "recall_norm": _mean_of([float(r["recall_norm"]) for r in items]),
            "precision": _mean_of([float(r["precision"]) for r in items]),
            "coverable": _mean_of([float(r["coverable"]) for r in items]),
            "hit1": _mean_of([float(r["hit1"]) for r in items]),
        }

    rows = list(rows)
    by_bin = {
        name: _summ([r for r in rows if lo <= int(r.get("n_expected", 0)) <= hi])
        for name, lo, hi in CHUNK_SIZE_BINS
    }
    return {
        "overall": _summ(rows),
        "multi": _summ([r for r in rows if r.get("is_multi")]),
        "single": _summ([r for r in rows if not r.get("is_multi")]),
        "by_size": by_bin,
    }
