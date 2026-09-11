"""站点解析器测试（F1）。

覆盖：解析器路由（按域名命中）、通用线索抽取（og/JSON-LD/正文）、站点专属线索、
以及「解析失败不拖垮主链路」的韧性。
"""

import pytest

from app.crawler.parsers import (
    GenericParser,
    available_sites,
    find_parser,
    format_hints,
    parse_page,
)

_HTML = """
<html><head>
  <title>闲鱼 - 让闲置游起来</title>
  <meta property="og:site_name" content="闲鱼">
  <meta property="og:description" content="阿里巴巴旗下二手闲置交易平台">
  <meta name="keywords" content="二手, 闲置, 交易">
  <script type="application/ld+json">
    {"@type": "WebSite", "name": "闲鱼", "description": "闲置交易社区"}
  </script>
</head>
<body><p>闲鱼是一个 C2C 二手闲置交易平台，支持信用交易。</p>
<p>用户可以发布闲置物品并在线沟通。</p></body></html>
"""


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.goofish.com/app", "xianyu"),
        ("https://2.taobao.com/item/1", "xianyu"),
        ("https://m.zhuanzhuan.com/", "zhuanzhuan"),
        ("https://www.aihuishou.com/recycle", "aihuishou"),
        ("https://example.com/whatever", "generic"),
        ("not-a-url", "generic"),
    ],
)
def test_find_parser_routes_by_domain(url, expected):
    """按域名路由到对应站点解析器，未知站点回落通用解析器。"""
    assert find_parser(url).key == expected


def test_generic_parser_extracts_og_and_jsonld():
    """通用解析器抽取 og:site_name / description / keywords，并回退到 JSON-LD。"""
    page = GenericParser().parse(_HTML, url="https://example.com/x")
    assert page.title == "闲鱼 - 让闲置游起来"
    assert page.hints["平台名"] == "闲鱼"
    assert "二手闲置交易平台" in page.hints["定位/简介"]
    assert "二手" in page.hints["关键词"]
    assert "C2C 二手闲置交易平台" in page.text


def test_xianyu_parser_adds_platform_type_hint():
    """站点解析器补充专属线索（平台类型），并保留通用线索。"""
    page = parse_page("https://www.goofish.com/app", _HTML, title="闲鱼")
    assert page.site == "xianyu"
    assert page.hints["平台类型"] == "C2C 闲置交易"
    assert page.hints["平台名"] == "闲鱼"


def test_aihuishou_parser_marks_recycle_type():
    """爱回收 → 回收置换类型。"""
    html = "<html><head><title>爱回收</title></head><body><div class='price'>iPhone 回收价 ¥3200</div></body></html>"
    page = parse_page("https://www.aihuishou.com/x", html)
    assert page.site == "aihuishou"
    assert page.hints["平台类型"] == "回收置换"
    assert "3200" in page.hints.get("定价/报价", "")


def test_parse_page_is_resilient_to_garbage_html():
    """异常 HTML 不应抛错：解析器内部失败自动降级为通用解析。"""
    page = parse_page("https://www.goofish.com/x", "<<<>>>not html at all", title="")
    assert page.url == "https://www.goofish.com/x"
    assert isinstance(page.hints, dict)


def test_parse_page_handles_empty_html():
    """空 HTML 也能返回最小可用结果。"""
    page = parse_page("https://example.com", "")
    assert page.site == "generic"
    assert page.title == ""


def test_format_hints_skips_empty_values():
    """线索格式化：跳过空值，无线索时返回空串。"""
    page = parse_page("https://www.goofish.com/a", _HTML)
    text = format_hints(page)
    assert "平台名: 闲鱼" in text
    assert "None" not in text


def test_available_sites_lists_registered_parsers():
    """已注册站点清单不包含通用兜底。"""
    sites = available_sites()
    assert set(sites) == {"xianyu", "zhuanzhuan", "aihuishou"}
    assert "generic" not in sites
