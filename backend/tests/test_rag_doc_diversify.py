"""「保底 + 补位」文档多样性去重的纯逻辑测试。

被测对象：``app.rag.retriever.diversify_with_refill``（生产代码）。它是
「多文档召回」优化方案的核心算法（详见 ``docs/RAG优化方案.md``）：

1. 保底：每篇文档只留一个代表 chunk（vsim 最高的那个），保证 top_k 覆盖
   尽可能多的不同文档，解决「同文档多 chunk 抱团占位」；
2. 补位：文档数不足 top_k 时（单文档/双文档问题），回填同文档次优 chunk，
   避免去重把名额白白扔掉。

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
