"""RAG 问答模块测试。

纯逻辑测试（不依赖 Ollama/Chroma/LLM）：
- chunking：frontmatter 剥离、标题分块、FAQ 分块
- rag_service：doc_id → title 反查
- 未就绪时 ask 返回兜底提示（mock 未就绪的 retriever）
"""

from app.rag.chunking import (
    SeedRow,
    _split_headings,
    _strip_fm,
    chunk_faq,
    chunk_markdown,
)


def _seed() -> SeedRow:
    return SeedRow(
        id="A-001",
        l1_category="平台规则制度",
        series="A1 账户与入驻",
        title="用户注册规则",
        template_type="rule",
    )


# ---------------- 分块 ----------------


def test_strip_fm():
    md = "---\nid: A-001\ntitle: 用户注册规则\ncategory: 平台规则制度\n---\n# 标题\n正文内容"
    meta, body = _strip_fm(md)
    assert meta["id"] == "A-001"
    assert meta["title"] == "用户注册规则"
    assert "# 标题" in body


def test_strip_fm_no_frontmatter():
    md = "# 没有 frontmatter\n正文"
    meta, body = _strip_fm(md)
    assert meta == {}
    assert body == md


def test_split_headings():
    body = "导语段落\n## 第一节\n内容一\n## 第二节\n内容二"
    sections = _split_headings(body)
    assert sections[0][0] == "（导语）"
    assert "导语段落" in sections[0][1]
    assert sections[1] == ("第一节", "内容一")
    assert sections[2] == ("第二节", "内容二")


def test_chunk_markdown_injects_title():
    md = "---\nid: A-001\n---\n# 用户注册规则\n## 第一章\n第1条 内容"
    chunks = chunk_markdown(_seed(), md)
    assert chunks, "应至少分出一个块"
    # 正文应注入《标题》
    assert "《用户注册规则》" in chunks[0].text
    assert chunks[0].doc_id == "A-001"
    assert chunks[0].category == "平台规则制度"


def test_chunk_faq_each_item_is_chunk():
    payload = {
        "items": [
            {
                "q_main": "密码错误怎么办",
                "q_variants": ["密码不对"],
                "a": "用忘记密码重置",
                "role": "buyer",
            },
            {
                "q_main": "收不到验证码",
                "q_variants": [],
                "a": "检查拦截",
                "role": "buyer",
            },
        ]
    }
    chunks = chunk_faq(_seed(), payload)
    assert len(chunks) == 2
    assert "密码错误怎么办" in chunks[0].text
    assert chunks[0].meta["faq_index"] == 1
    assert chunks[1].meta["faq_index"] == 2


# ---------------- rag_service 纯逻辑 ----------------


def test_title_from_doc_id():
    from app.services.rag_service import _title_from_doc_id

    # A-001 在 seeds 中存在（若 seeds CSV 已就位），否则返回 doc_id 本身
    title = _title_from_doc_id("A-001")
    assert title in ("用户注册规则", "A-001")  # 有 CSV 时返回标题，无则回退 id


def test_title_from_doc_id_unknown():
    from app.services.rag_service import _title_from_doc_id

    assert _title_from_doc_id("ZZZ-999") == "ZZZ-999"


async def test_ask_when_not_ready(monkeypatch):
    """向量库未就绪时，ask 返回兜底提示而非抛错。"""
    from app.services import rag_service

    class _NotReady:
        is_ready = False

        def search(self, *a, **k):
            raise AssertionError("未就绪时不应触发检索")

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _NotReady())
    resp = await rag_service.ask("忘记密码怎么办")
    assert resp.mode == "fallback"
    assert "知识库尚未就绪" in resp.answer


# ---------------- rag_service 命中 / 兜底分支 ----------------


class _ReadyRetriever:
    """就绪的 mock retriever，search 返回预设结果。"""

    is_ready = True

    def __init__(self, results):
        self._results = results

    def search(self, question, top_k=5):
        return self._results


def _hit_results():
    """构造命中结果：首条 vector_sim 高，且含重复 doc_id 用于测去重。"""
    return [
        {"doc_id": "A-006", "text": "企业卖家需营业执照", "vector_sim": 0.72},
        {"doc_id": "C-001", "text": "个人卖家需实名", "vector_sim": 0.70},
        {"doc_id": "A-006", "text": "企业卖家需营业执照（续）", "vector_sim": 0.68},
    ]


async def test_ask_hit_returns_direct_answer(monkeypatch):
    """命中：mode=hit，answer 直出首条原文，来源按 doc_id 去重。"""
    from app.services import rag_service

    # 默认档位已是 multi_doc；本用例验证「直出」契约，显式固定 off
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "off")
    monkeypatch.setattr(
        rag_service, "get_retriever", lambda: _ReadyRetriever(_hit_results())
    )
    resp = await rag_service.ask("卖家入驻需要什么资质")

    assert resp.mode == "hit"
    assert resp.answer == "企业卖家需营业执照"
    # 去重后 A-006 只保留一次，共 2 个来源
    assert [s.doc_id for s in resp.sources] == ["A-006", "C-001"]
    assert resp.question == "卖家入驻需要什么资质"


async def test_ask_gray_zone_injects_context_and_returns_sources(monkeypatch):
    """灰区（0.4 ≤ top1 < 阈值）走兜底：临界片段注入 prompt，并作为来源返回供核验。"""
    from app.services import rag_service

    gray = [
        {"doc_id": "A-006", "text": "入驻需营业执照", "vector_sim": 0.52},
        {"doc_id": "C-001", "text": "个人卖家实名", "vector_sim": 0.45},
    ]
    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(gray))
    captured = {}

    async def _fake_fallback(
        question, history=None, competitors_context=None, retrieved_context=None
    ):
        captured["ctx"] = retrieved_context
        return "（mock 兜底回答）"

    monkeypatch.setattr(rag_service, "_llm_fallback", _fake_fallback)
    resp = await rag_service.ask("卖家入驻要什么资质")

    assert resp.mode == "fallback"
    assert resp.answer == "（mock 兜底回答）"
    # 临界片段确实注入 prompt（带来源与相似度标注），并作为来源返回
    assert "A-006" in captured["ctx"] and "0.52" in captured["ctx"]
    assert [s.doc_id for s in resp.sources] == ["A-006", "C-001"]


async def test_ask_rejects_without_llm_when_all_chunks_too_weak(monkeypatch):
    """检索相似度连 0.4 都不到 → 直接拒答，不调用 LLM（杜绝无据编造）。"""
    from app.services import rag_service

    low = [{"doc_id": "A-006", "text": "无关", "vector_sim": 0.3}]
    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(low))

    async def _should_not_call(*a, **k):
        raise AssertionError("相似度过低时不应调用 LLM")

    monkeypatch.setattr(rag_service, "_llm_fallback", _should_not_call)
    resp = await rag_service.ask("今天天气怎么样")

    assert resp.mode == "fallback"
    assert "暂无" in resp.answer
    assert resp.sources == []


async def test_ask_empty_results_rejects_without_llm(monkeypatch):
    """检索无结果 → 拒答（知识库确无内容），不调用 LLM。"""
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever([]))

    async def _should_not_call(*a, **k):
        raise AssertionError("无检索结果时不应调用 LLM")

    monkeypatch.setattr(rag_service, "_llm_fallback", _should_not_call)

    resp = await rag_service.ask("完全无关的问题")
    assert resp.mode == "fallback"
    assert "暂无" in resp.answer


async def test_llm_fallback_graceful_on_timeout(monkeypatch):
    """LLM 兜底超时/HTTP 错误时优雅降级，不抛异常。"""
    import httpx

    from app.services import rag_service

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(rag_service.httpx, "AsyncClient", lambda **k: _BoomClient())
    # 成本预检需放行：确保 precheck 返回 allowed
    monkeypatch.setattr(
        rag_service,
        "get_cost_tracker",
        lambda: _PassingTracker(),
    )

    answer = await rag_service._llm_fallback("随便问")
    assert "暂时不可用" in answer


class _PassingTracker:
    """precheck 放行、record 无副作用的 mock tracker。"""

    async def restore_from_db(self):
        return None

    def precheck(self, est_input):
        return {"blocked": False, "allowed": True}

    def record(self, *a, **k):
        return None


async def test_llm_fallback_blocked_returns_hint(monkeypatch):
    """成本熔断 blocked 时直接返回提示，不调用 LLM。"""
    from app.services import rag_service

    class _BlockedTracker:
        async def restore_from_db(self):
            return None

        def precheck(self, est_input):
            return {"blocked": True}

    monkeypatch.setattr(rag_service, "get_cost_tracker", lambda: _BlockedTracker())
    answer = await rag_service._llm_fallback("随便问")
    assert "成本熔断" in answer


# ---------------- 多轮会话记忆 ----------------


async def test_ask_injects_history_to_fallback(monkeypatch):
    """未命中走兜底时，把历史上下文传给 _llm_fallback。"""
    from app.services import rag_service

    gray = [{"doc_id": "A-001", "text": "参考片段", "vector_sim": 0.5}]
    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(gray))
    captured = {}

    async def _fake(q, history=None, competitors_context=None, retrieved_context=None):
        captured["history"] = history
        return "兜底"

    monkeypatch.setattr(rag_service, "_llm_fallback", _fake)

    history = [
        {"role": "user", "content": "闲鱼抽成多少"},
        {"role": "assistant", "content": "闲鱼抽成约 X"},
    ]
    await rag_service.ask("那转转呢", session_id="s1", history=history)
    assert captured["history"] == history


def test_ask_returns_session_id(monkeypatch):
    """带 session_id 时，响应回带 session_id。"""
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "SYNTH_MODE", "off")  # 聚焦直出契约
    monkeypatch.setattr(
        rag_service, "get_retriever", lambda: _ReadyRetriever(_hit_results())
    )
    import asyncio

    resp = asyncio.run(rag_service.ask("卖家入驻需要什么资质", session_id="s-abc"))
    assert resp.session_id == "s-abc"
    assert resp.mode == "hit"


async def test_llm_fallback_history_limits_rounds(monkeypatch):
    """历史超长时按最近 N 轮截断由调用方控制；此处验证兜底能接受任意历史列表。"""
    from app.services import rag_service

    class _T:
        async def restore_from_db(self):
            return None

        def precheck(self, est_input):
            return {"allowed": True, "blocked": False}

        def record(self, *a, **k):
            return None

    monkeypatch.setattr(rag_service, "get_cost_tracker", lambda: _T())

    # mock httpx 返回固定内容
    class _C:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            class _R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "choices": [{"message": {"content": "多轮兜底回答"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    }

            return _R()

    monkeypatch.setattr(
        rag_service.httpx, "AsyncClient", lambda **k: _C(k.get("timeout"))
    )
    answer = await rag_service._llm_fallback(
        "那转转呢",
        history=[
            {"role": "user", "content": "闲鱼抽成"},
            {"role": "assistant", "content": "闲鱼抽成 X"},
        ],
    )
    assert answer == "多轮兜底回答"


# ---------------- 竞品数据联动（B2） ----------------


class _FakeCompetitor:
    """模拟 Competitor ORM 实例。"""

    def __init__(self, **kw):
        self.name = kw.get("name", "")
        self.type = kw.get("type")
        self.positioning = kw.get("positioning")
        self.business_model = kw.get("business_model")
        self.pricing = kw.get("pricing")
        self.strengths = kw.get("strengths")
        self.weaknesses = kw.get("weaknesses")


def test_build_competitors_context():
    from app.services import rag_service

    comps = [
        _FakeCompetitor(
            name="闲鱼",
            type="C2C 闲置交易",
            positioning="个人闲置交易平台",
            business_model="C2C 撮合",
            pricing="交易抽佣 0.6%",
            strengths="用户量大",
            weaknesses="假货风险",
        ),
        _FakeCompetitor(name="转转", type="回收置换", pricing="质检转卖，抽佣更高"),
    ]
    ctx = rag_service.build_competitors_context(comps)
    assert "【闲鱼】" in ctx
    assert "交易抽佣 0.6%" in ctx
    assert "【转转】" in ctx
    # 空字段不输出空值占位
    assert "None" not in ctx


def test_build_competitors_context_empty():
    from app.services import rag_service

    assert rag_service.build_competitors_context([]) == ""


async def test_ask_injects_competitors_context(monkeypatch):
    """未命中兜底时，竞品上下文透传给 _llm_fallback。"""
    from app.services import rag_service

    gray = [{"doc_id": "A-001", "text": "参考片段", "vector_sim": 0.5}]
    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(gray))
    captured = {}

    async def _fake(q, history=None, competitors_context=None, retrieved_context=None):
        captured["ctx"] = competitors_context
        return "兜底"

    monkeypatch.setattr(rag_service, "_llm_fallback", _fake)
    await rag_service.ask(
        "闲鱼和转转哪个抽成高", competitors_context="【闲鱼】抽佣0.6%"
    )
    assert captured["ctx"] == "【闲鱼】抽佣0.6%"


# ---------------- 兜底路径：临界片段提取（P1） ----------------


def test_build_fallback_context_picks_gray_zone_and_dedups_doc():
    """临界片段只取 [0.4, 0.65)：命中区(≥0.65)由 hit gate 负责，同 doc 只留最相似一个。"""
    from app.services.rag_service import build_fallback_context

    results = [
        {"doc_id": "A-001", "text": "命中区（应由 hit gate 处理）", "vector_sim": 0.72},
        {"doc_id": "B-002", "text": "灰区", "vector_sim": 0.55},
        {"doc_id": "B-002", "text": "同文档次优", "vector_sim": 0.50},
        {"doc_id": "C-003", "text": "灰区二", "vector_sim": 0.42},
        {"doc_id": "D-004", "text": "噪声", "vector_sim": 0.10},
    ]
    docs = build_fallback_context(results)
    assert [d.doc_id for d in docs] == ["B-002", "C-003"]


def test_build_fallback_context_none_when_below_min_sim():
    """最高相似度 < 0.4（或空结果）→ 返回 None，调用方据此拒答。"""
    from app.services.rag_service import build_fallback_context

    assert build_fallback_context([]) is None
    assert (
        build_fallback_context([{"doc_id": "X", "text": "t", "vector_sim": 0.39}])
        is None
    )


def test_build_fallback_context_caps_chunks():
    """注入片段数受 FALLBACK_CONTEXT_MAX_CHUNKS 限制，避免 token 膨胀。"""
    from app.rag.config import FALLBACK_CONTEXT_MAX_CHUNKS
    from app.services.rag_service import build_fallback_context

    results = [{"doc_id": f"D-{i}", "text": "t", "vector_sim": 0.5} for i in range(10)]
    docs = build_fallback_context(results)
    assert len(docs) == FALLBACK_CONTEXT_MAX_CHUNKS


def test_format_fallback_context_annotates_doc_and_score():
    """注入文本需带来源与相似度标注，模型与用户可据此判断可信度。"""
    from app.schemas.rag import SourceDoc
    from app.services.rag_service import format_fallback_context

    text = format_fallback_context(
        [SourceDoc(doc_id="A-001", title="t", text="正文", score=0.52)]
    )
    assert "A-001" in text and "0.52" in text and "正文" in text


# ---------------- rerank 精排 ----------------


def test_rerank_by_vsim_pushes_bm25_noise_to_tail():
    """rerank 按向量相似度重排，vsim=0 的纯 BM25 命中沉到末尾。"""
    from app.rag.retriever import rerank_by_vsim

    # RRF 排序结果：pos=5 是 BM25 独有命中（vsim 缺失），却排在第 1 位
    ranked = [(5, 0.033), (1, 0.032), (2, 0.031)]
    v_scores = {1: 0.72, 2: 0.68}  # pos=5 无向量分数
    out = rerank_by_vsim(ranked, v_scores, top_k=3)
    # 重排后：vsim 高的在前，vsim 缺失的沉底
    assert [p for p, _ in out] == [1, 2, 5]


def test_rerank_by_vsim_respects_top_k():
    """rerank 截断到 top_k。"""
    from app.rag.retriever import rerank_by_vsim

    ranked = [(1, 0.033), (2, 0.032), (3, 0.031), (4, 0.030)]
    v_scores = {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6}
    out = rerank_by_vsim(ranked, v_scores, top_k=2)
    assert len(out) == 2
    assert [p for p, _ in out] == [1, 2]


def test_rerank_by_vsim_stable_on_ties():
    """vsim 相同时保持 RRF 原顺序（稳定排序）。"""
    from app.rag.retriever import rerank_by_vsim

    ranked = [(1, 0.033), (2, 0.032)]
    v_scores = {1: 0.5, 2: 0.5}
    out = rerank_by_vsim(ranked, v_scores, top_k=2)
    assert [p for p, _ in out] == [1, 2]


# —— 命中闸门：来源过滤 + 阈值（回归：用户报「回答怎么对不上」）——


def test_build_hit_gate_drops_pure_bm25_noise_sources():
    """BM25 单独命中（vector_sim=0）不得作为来源展示。

    回归背景：线上出现过「相关度 0.0%」的来源条目——那是 BM25 关键词偶然命中、
    向量完全不相干的 chunk，展示给用户属于无意义引用。
    """
    from app.services.rag_service import build_hit_gate

    results = [
        {"doc_id": "FAQ-033", "text": "私下转账风险正文", "vector_sim": 0.72},
        {"doc_id": "B-036", "text": "卖家要求私下交易", "vector_sim": 0.68},
        {"doc_id": "E-051", "text": "极端情绪安抚", "vector_sim": 0.0},  # 纯 BM25 噪声
    ]
    gate = build_hit_gate(results)

    assert gate is not None
    # E-051（相关度 0.0%）被过滤，只剩余两条真实语义命中的来源
    assert [s.doc_id for s in gate["sources"]] == ["FAQ-033", "B-036"]


def test_build_hit_gate_blocks_gray_zone_out_of_domain_question():
    """领域外问题的灰区相似度不应判为命中（应走 LLM 兜底）。

    实测「你觉得转转这个平台怎么样」top1 相似度 0.6235：高于旧阈值 0.55 会直出
    一条与问题无关的 FAQ 原文；现阈值 0.65 应判为未命中。
    """
    from app.services.rag_service import build_hit_gate

    gray = [{"doc_id": "FAQ-033", "text": "私下转账风险正文", "vector_sim": 0.6235}]
    assert build_hit_gate(gray) is None


def test_build_hit_gate_accepts_in_domain_min_score():
    """领域内正确样本的最低实测相似度 0.6660 仍应判为命中（阈值不得误伤）。

    0.65 落在「错误样本最低 0.6483」与「正确样本最低 0.6660」之间的空隙，
    这是 285 问扫描里选它的直接理由。
    """
    from app.services.rag_service import build_hit_gate

    ok = [{"doc_id": "A-001", "text": "注册规定原文", "vector_sim": 0.6660}]
    gate = build_hit_gate(ok)
    assert gate is not None
    assert [s.doc_id for s in gate["sources"]] == ["A-001"]


def test_build_hit_gate_empty_results():
    """检索无结果时视为未命中。"""
    from app.services.rag_service import build_hit_gate

    assert build_hit_gate([]) is None
