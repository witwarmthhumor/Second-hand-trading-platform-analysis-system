"""「保底 + 补位」文档多样性去重的纯逻辑测试。

被测对象：``app.rag.retriever.diversify_with_refill``（生产代码）。它是
「多文档召回」优化方案的核心算法（详见 ``docs/RAG优化方案.md``）：

1. 保底：每篇文档只留一个代表 chunk（vsim 最高的那个），保证 top_k 覆盖
   尽可能多的不同文档，解决「同文档多 chunk 抱团占位」；
2. 补位：文档数不足 top_k 时（单文档/双文档问题），回填同文档次优 chunk，
   避免去重把名额白白扔掉；
3. 相关性闸门（P0-2，``min_sim``）：保底阶段文档代表 chunk 的 vsim 低于阈值
   则整篇不占名额。缺了它，只要「存在」的低相关文档也能占掉格子，把本该
   属于好文档次优 chunk 的名额挤走（单文档问题 chunk 级 precision 0.71→0.20）。
   补位阶段**不设**闸门，保证难查询不会返回空结果。

不依赖 Ollama / Chroma / LLM，纯函数直接构造输入。
"""

from app.rag.retriever import diversify_with_refill


def _ranked(v_scores: dict[int, float]) -> list[tuple[int, float]]:
    """按 vsim 降序构造 ranked_all（模拟 rerank 后的全序候选）。"""
    return [(pos, 0.0) for pos, _ in sorted(v_scores.items(), key=lambda x: -x[1])]


def test_多文档_每篇文档保底一个代表():
    """5 篇不同文档、top_k=3：保底取前 3 篇，各 1 个 chunk。"""
    doc_ids = ["A", "B", "C", "D", "E"]
    v_scores = {0: 0.90, 1: 0.85, 2: 0.80, 3: 0.75, 4: 0.70}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=3)
    assert [p for p, _ in out] == [0, 1, 2]


def test_同文档抱团_去重后只留最高的():
    """同一文档多个 chunk 抱团，只保留 vsim 最高的那个作为代表。"""
    doc_ids = ["A", "A", "A", "B", "B"]
    v_scores = {0: 0.90, 1: 0.88, 2: 0.86, 3: 0.70, 4: 0.68}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=2)
    assert [p for p, _ in out] == [0, 3]


def test_单文档_补位填满topk且首位仍是全局最高():
    """只有 1 篇文档时，保底 1 个 + 补位 4 个 = top_k，且首位是全局最相似 chunk。"""
    doc_ids = ["A"] * 6
    v_scores = {0: 0.90, 1: 0.80, 2: 0.70, 3: 0.60, 4: 0.50, 5: 0.40}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=5)
    assert len(out) == 5
    assert [p for p, _ in out] == [0, 1, 2, 3, 4]  # 首位仍是 vsim 最高的 pos=0
    # 输出整体按 vsim 降序（命中判定只看 results[0]，口径不变）
    sims = [v_scores[p] for p, _ in out]
    assert sims == sorted(sims, reverse=True)


def test_双文档_保底两篇再补位():
    """2 篇文档、top_k=5：保底 2 篇，补位 3 个同文档次优 chunk。"""
    doc_ids = ["A", "A", "A", "B", "B"]
    v_scores = {0: 0.90, 1: 0.80, 2: 0.70, 3: 0.60, 4: 0.50}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=5)
    assert len(out) == 5
    # 候选全部用上（不足 5 篇文档但候选够 5 个），首位仍是全局最高
    assert [p for p, _ in out] == [0, 1, 2, 3, 4]


def test_候选不足topk_返回全部不报错():
    """候选数少于 top_k 时返回全部候选，不抛异常、不补出空位。"""
    doc_ids = ["A", "B"]
    v_scores = {0: 0.90, 1: 0.80}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=5)
    assert [p for p, _ in out] == [0, 1]


def test_越界doc_id兜底为chunk占位():
    """doc_id 列表短于候选位置时退化为 c{pos}，不抛 IndexError。"""
    doc_ids = ["A"]  # 只有一个 doc_id，但候选有 3 个位置
    v_scores = {0: 0.90, 1: 0.80, 2: 0.70}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# 相关性闸门（P0-2）
# ---------------------------------------------------------------------------
# 场景：单文档问题的检索候选 = 好文档 A 的若干块 + 两个「沾边但不相干」文档。
# 不带闸门时，B/C 的代表块也会各占一个保底名额，把本该属于 A 次优块的名额
# 挤走 —— 这正是 doc 级指标看不到、chunk 级 precision 才暴露的「深度损失」。


def test_闸门_低相关文档不占名额_让位给同文档次优():
    """A 有 5 块高分，B/C 各 1 块低分：开闸门后 top-5 全部来自 A。"""
    doc_ids = ["A", "A", "A", "A", "A", "B", "C"]
    v_scores = {0: 0.90, 1: 0.86, 2: 0.82, 3: 0.78, 4: 0.74, 5: 0.45, 6: 0.40}
    ranked = _ranked(v_scores)

    # 不开闸门（= 旧形态）：B/C 各占一个保底名额，只剩 2 个名额给 A 的次优块
    out_off = diversify_with_refill(ranked, v_scores, doc_ids, top_k=5, min_sim=0.0)
    assert [p for p, _ in out_off] == [0, 1, 2, 5, 6]

    # 开闸门：B(0.45)/C(0.40) 不达标 → 名额全部由 A 的次优块补满
    out_on = diversify_with_refill(ranked, v_scores, doc_ids, top_k=5, min_sim=0.5)
    assert [p for p, _ in out_on] == [0, 1, 2, 3, 4]
    assert {doc_ids[p] for p, _ in out_on} == {"A"}


def test_闸门_全部不达标_补位回填不空手():
    """所有文档都不达标时保底空手，补位必须把全局 vsim 最高的若干块原样填回。"""
    doc_ids = ["A", "B", "C"]
    v_scores = {0: 0.45, 1: 0.40, 2: 0.35}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=3, min_sim=0.5)
    # 不因闸门返回空结果，len 与顺序都与「不开闸门」一致
    assert [p for p, _ in out] == [0, 1, 2]


def test_闸门_等于阈值算达标():
    """闸门是闭区间（>=）：vsim 恰好等于 min_sim 的文档仍占名额。"""
    doc_ids = ["A", "A", "A", "B"]
    v_scores = {0: 0.90, 1: 0.86, 2: 0.82, 3: 0.50}
    ranked = _ranked(v_scores)

    # B 的 0.50 ≥ 0.5 → 保住名额，补位只能捞到 1 个 A 次优块
    assert [
        p
        for p, _ in diversify_with_refill(ranked, v_scores, doc_ids, top_k=3, min_sim=0.5)
    ] == [0, 1, 3]


def test_闸门_首位始终是全局最相似chunk():
    """闸门不改变命中判定口径：输出首位仍是全局 vsim 最高的 chunk。"""
    doc_ids = ["A", "A", "B"]
    v_scores = {0: 0.90, 1: 0.50, 2: 0.45}
    out = diversify_with_refill(_ranked(v_scores), v_scores, doc_ids, top_k=2, min_sim=0.5)
    assert out[0][0] == 0
    sims = [v_scores[p] for p, _ in out]
    assert sims == sorted(sims, reverse=True)


def test_闸门_默认参数等价于关闭():
    """min_sim 默认 0.0 → 与显式传 0.0 行为完全一致（向后兼容守卫）。"""
    doc_ids = ["A", "A", "A", "B", "C"]
    v_scores = {0: 0.90, 1: 0.86, 2: 0.82, 3: 0.45, 4: 0.40}
    ranked = _ranked(v_scores)
    default = [p for p, _ in diversify_with_refill(ranked, v_scores, doc_ids, top_k=4)]
    explicit = [
        p
        for p, _ in diversify_with_refill(ranked, v_scores, doc_ids, top_k=4, min_sim=0.0)
    ]
    assert default == explicit == [0, 1, 3, 4]


# ---------------------------------------------------------------------------
# 锚定 top1 文档（P0-2 修正方案）
# ---------------------------------------------------------------------------
# 阈值判据被实测证伪（噪声/期望文档的 vsim 比值分布重叠），改用显式名额旋钮：
# 让 top1 所在文档多占几个名额，把「深度」还给单文档问题。下面用「候选里同时
# 有 5 篇以上文档」的场景，才能看出与 anchor_quota=1 的区别。


def test_锚定_配额1_保底被其他文档占满():
    """anchor_quota=1（原行为）：5 篇不同文档各占 1 个，锚定文档只有 1 块。"""
    doc_ids = ["A", "A", "A", "A", "A", "B", "C", "D", "E", "F"]
    v_scores = {
        0: 0.90, 1: 0.86, 2: 0.82, 3: 0.78, 4: 0.74,
        5: 0.70, 6: 0.68, 7: 0.66, 8: 0.64, 9: 0.62,
    }
    out = diversify_with_refill(
        _ranked(v_scores), v_scores, doc_ids, top_k=5, anchor_quota=1
    )
    assert [p for p, _ in out] == [0, 5, 6, 7, 8]
    assert {doc_ids[p] for p, _ in out} == {"A", "B", "C", "D", "E"}


def test_锚定_配额3_深度恢复_广度让位():
    """anchor_quota=3：锚定文档占 3 块，广度文档从 5 篇降到 3 篇。"""
    doc_ids = ["A", "A", "A", "A", "A", "B", "C", "D", "E", "F"]
    v_scores = {
        0: 0.90, 1: 0.86, 2: 0.82, 3: 0.78, 4: 0.74,
        5: 0.70, 6: 0.68, 7: 0.66, 8: 0.64, 9: 0.62,
    }
    out = diversify_with_refill(
        _ranked(v_scores), v_scores, doc_ids, top_k=5, anchor_quota=3
    )
    assert [p for p, _ in out] == [0, 1, 2, 5, 6]
    assert [doc_ids[p] for p, _ in out] == ["A", "A", "A", "B", "C"]


def test_锚定_候选不足时配额不超发_补位兜底():
    """只剩 1 篇文档的 3 个块时，配额 2 只能取到 2 块，其余由补位/候选上限兜底。"""
    doc_ids = ["A", "A", "A"]
    v_scores = {0: 0.90, 1: 0.86, 2: 0.82}
    out = diversify_with_refill(
        _ranked(v_scores), v_scores, doc_ids, top_k=5, anchor_quota=2
    )
    assert [p for p, _ in out] == [0, 1, 2]


def test_锚定_默认配额等价于1():
    """anchor_quota 默认 1 → 与显式传 1 行为完全一致（向后兼容守卫）。"""
    doc_ids = ["A", "A", "A", "B", "C", "D", "E", "F"]
    v_scores = {
        0: 0.90, 1: 0.86, 2: 0.82, 3: 0.70, 4: 0.68, 5: 0.66, 6: 0.64, 7: 0.62
    }
    ranked = _ranked(v_scores)
    default = [p for p, _ in diversify_with_refill(ranked, v_scores, doc_ids, top_k=5)]
    explicit = [
        p
        for p, _ in diversify_with_refill(ranked, v_scores, doc_ids, top_k=5, anchor_quota=1)
    ]
    assert default == explicit == [0, 3, 4, 5, 6]


