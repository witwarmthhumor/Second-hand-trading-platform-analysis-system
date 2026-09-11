"""LLM Provider 工厂与提炼能力测试。

覆盖：
- get_provider() 工厂：有 key 走 RemoteLLMProvider，无 key 走 RuleExtractor。
- RemoteLLMProvider._normalize：type 白名单归一、list 字段拼接、tags 规整。
- RuleExtractor.extract：纯规则提炼（不依赖网络）。
"""

import pytest

from app.ai import provider as prov


def test_get_provider_with_key(monkeypatch):
    """有 key → 远程提炼（外层包 FallbackProvider，远程失败可回退规则）。"""
    monkeypatch.setattr(prov.settings, "llm_api_key", "sk-test-123")
    provider = prov.get_provider()
    assert isinstance(provider, prov.FallbackProvider)
    assert isinstance(provider.primary, prov.RemoteLLMProvider)
    assert provider.billable is True


def test_get_provider_without_key(monkeypatch):
    """无 key → 规则兜底。"""
    monkeypatch.setattr(prov.settings, "llm_api_key", "")
    assert isinstance(prov.get_provider(), prov.RuleExtractor)


async def test_fallback_provider_falls_back_on_remote_error(monkeypatch):
    """远程提炼失败时自动回退规则提取，链路不中断（R1 降级策略）。"""

    class _BoomRemote(prov.RemoteLLMProvider):
        async def extract(self, url, html, title="", hints=None):
            raise RuntimeError("402 余额不足")

    provider = prov.FallbackProvider(_BoomRemote(), prov.RuleExtractor())
    html = "<html><title>闲鱼</title><body><p>正文</p></body></html>"
    result = await provider.extract("https://x.com", html)
    assert provider.fell_back is True
    assert result.name == "闲鱼"
    # 回退时不计费：用量归零
    assert provider.last_input_tokens == 0
    assert provider.last_output_tokens == 0


async def test_fallback_provider_uses_remote_when_ok():
    """远程正常时不回退，并透传 token 用量。"""

    class _OkRemote(prov.RemoteLLMProvider):
        async def extract(self, url, html, title="", hints=None):
            self.last_input_tokens = 100
            self.last_output_tokens = 20
            return prov.Extraction(name="闲鱼", type="C2C 闲置交易")

    provider = prov.FallbackProvider(_OkRemote(), prov.RuleExtractor())
    result = await provider.extract("https://x.com", "<html></html>")
    assert provider.fell_back is False
    assert result.name == "闲鱼"
    assert provider.last_input_tokens == 100
    assert provider.last_output_tokens == 20


@pytest.mark.parametrize(
    "raw_type, expected",
    [
        ("C2C 闲置交易", "C2C 闲置交易"),
        ("回收置换", "回收置换"),
        ("质检转卖", "质检转卖"),
        ("乱七八糟的值", "C2C 闲置交易"),  # 白名单外 → 兜底默认
    ],
)
def test_normalize_type_whitelist(raw_type, expected):
    data = {"name": "x", "type": raw_type}
    out = prov.RemoteLLMProvider._normalize(dict(data))
    assert out["type"] == expected


def test_normalize_list_fields_joined():
    data = {
        "name": "x",
        "type": "C2C 闲置交易",
        "features": ["a", "b", "c"],
        "tags": ["t1", "t2"],
    }
    out = prov.RemoteLLMProvider._normalize(data)
    assert out["features"] == "a；b；c"
    assert out["tags"] == ["t1", "t2"]


def test_normalize_tags_string_to_list():
    data = {"name": "x", "type": "C2C 闲置交易", "tags": "单标签"}
    out = prov.RemoteLLMProvider._normalize(data)
    assert out["tags"] == ["单标签"]


def test_parse_json_strips_code_fence():
    content = "```json\n{\"name\": \"闲鱼\"}\n```"
    assert prov.RemoteLLMProvider._parse_json(content) == {"name": "闲鱼"}


async def test_rule_extractor_no_network():
    """规则提炼兜底：从 HTML 抽 title/desc/正文，不依赖网络。"""
    html = (
        "<html><head><title>闲鱼</title>"
        '<meta name="description" content="二手闲置平台"></head>'
        "<body><p>第一段正文内容</p><p>第二段正文内容</p></body></html>"
    )
    r = await prov.RuleExtractor().extract("https://x.com", html)
    assert r.name == "闲鱼"
    assert r.type == "C2C 闲置交易"
    assert r.positioning == "二手闲置平台"
    assert "第一段正文内容" in r.features
