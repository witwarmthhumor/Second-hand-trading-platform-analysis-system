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
