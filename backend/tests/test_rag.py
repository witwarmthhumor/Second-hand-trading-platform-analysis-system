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
            {"q_main": "密码错误怎么办", "q_variants": ["密码不对"], "a": "用忘记密码重置", "role": "buyer"},
            {"q_main": "收不到验证码", "q_variants": [], "a": "检查拦截", "role": "buyer"},
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

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(_hit_results()))
    resp = await rag_service.ask("卖家入驻需要什么资质")

    assert resp.mode == "hit"
    assert resp.answer == "企业卖家需营业执照"
    # 去重后 A-006 只保留一次，共 2 个来源
    assert [s.doc_id for s in resp.sources] == ["A-006", "C-001"]
    assert resp.question == "卖家入驻需要什么资质"


async def test_ask_hit_below_threshold_falls_back(monkeypatch):
    """未命中（最高相似度 < 阈值）时走 LLM 兜底，sources 为空。"""
    from app.services import rag_service

    low = [{"doc_id": "A-006", "text": "无关", "vector_sim": 0.3}]
    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(low))

    async def _fake_fallback(question, history=None):
        return "（mock 兜底回答）"

    monkeypatch.setattr(rag_service, "_llm_fallback", _fake_fallback)
    resp = await rag_service.ask("今天天气怎么样")

    assert resp.mode == "fallback"
    assert resp.answer == "（mock 兜底回答）"
    assert resp.sources == []


async def test_ask_empty_results_falls_back(monkeypatch):
    """检索无结果时走 LLM 兜底。"""
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever([]))

    async def _fake(q, history=None):
        return "兜底"

    monkeypatch.setattr(rag_service, "_llm_fallback", _fake)

    resp = await rag_service.ask("完全无关的问题")
    assert resp.mode == "fallback"


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

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever([]))
    captured = {}

    async def _fake(q, history=None):
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

    monkeypatch.setattr(rag_service, "get_retriever", lambda: _ReadyRetriever(_hit_results()))
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

    monkeypatch.setattr(rag_service.httpx, "AsyncClient", lambda **k: _C(k.get("timeout")))
    answer = await rag_service._llm_fallback(
        "那转转呢",
        history=[
            {"role": "user", "content": "闲鱼抽成"},
            {"role": "assistant", "content": "闲鱼抽成 X"},
        ],
    )
    assert answer == "多轮兜底回答"
