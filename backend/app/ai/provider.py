"""可插拔 LLM Provider 抽象层。

设计目标：
- 统一提炼接口，调用方不关心底层是远程 API 还是本地规则。
- RemoteLLMProvider：OpenAI 兼容接口（本项目接 gpt-5.6-luna）。
- RuleExtractor：纯规则兜底，无密钥也能跑通链路。
- FallbackProvider：远程优先、失败自动回退规则，保证 LLM 不可用时抓取链路不中断。
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

import httpx
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)


class Extraction(BaseModel):
    """从网页提炼出的竞品结构化字段（供后续人工校对）。"""

    name: str
    type: str
    positioning: str | None = None
    features: str | None = None
    target_users: str | None = None
    business_model: str | None = None
    pricing: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    tags: list[str] | None = None


class LLMProvider(Protocol):
    """提炼能力统一接口。"""

    async def extract(self, url: str, html: str, title: str = "") -> Extraction:
        """从网页内容提炼竞品结构化字段。"""
        ...


def is_llm_configured() -> bool:
    """是否配置了 LLM 密钥（决定走远程提炼还是规则兜底）。"""
    return bool(settings.llm_api_key)


# 允许的 type 值（与 PlatformType 对齐，避免 LLM 乱写）
_TYPE_WHITELIST = {"C2C 闲置交易", "回收置换", "质检转卖"}

_SYSTEM_PROMPT = (
    "你是竞品分析助手。请从给定的二手交易平台网页内容中，"
    "提炼该平台的竞品信息，并严格按 JSON 输出，不要输出多余文字。\n"
    "JSON 字段：name(平台名)、type(平台类型，只能取「C2C 闲置交易/回收置换/质检转卖」之一)、"
    "positioning(一句话定位)、features(核心功能，可多条)、target_users(目标用户)、"
    "business_model(业务/盈利模式)、pricing(定价/抽佣，未知则 null)、"
    "strengths(优势)、weaknesses(不足)、tags(标签数组)。\n"
    "信息不足的字段填 null，不要编造。"
)


def _estimate_tokens(text: str) -> int:
    """极简 token 估算：中英文混合按字符数/2 粗估（用于无 usage 时的兜底记账）。"""
    return max(1, len(text) // 2)


class RemoteLLMProvider:
    """OpenAI 兼容接口的 LLM 提炼实现。"""

    # 标记为计费 provider：调用方据此决定是否做成本预检与 token 记账
    billable = True

    def __init__(
        self,
        api_base: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.api_base = (api_base or settings.llm_api_base).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        # 最近一次真实调用的 token 用量（供成本记账；单请求串行使用场景足够）
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0

    async def extract(self, url: str, html: str, title: str = "") -> Extraction:
        # 先用 BS4 抽取纯文本（去标签、压缩空白），比直接发 HTML 更省 token
        text = self._html_to_text(html, title)
        snippet = text[:4000]
        user_prompt = f"网页 URL: {url}\n标题: {title}\n正文内容:\n{snippet}"

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            # 记录真实 token 用量（供成本熔断记账），缺失时按输入字符粗估
            usage = data.get("usage") or {}
            self.last_input_tokens = int(
                usage.get("prompt_tokens") or _estimate_tokens(user_prompt)
            )
            self.last_output_tokens = int(
                usage.get("completion_tokens") or _estimate_tokens(content)
            )

        parsed = self._parse_json(content)
        return Extraction(**self._normalize(parsed))

    @staticmethod
    def _parse_json(content: str) -> dict:
        """容错解析 LLM 返回的 JSON（可能带 ```json 包裹）。"""
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)

    @staticmethod
    def _html_to_text(html: str, title: str) -> str:
        """HTML → 纯文本，提取标题/描述/正文/链接，供 LLM 提炼。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        lines = []
        if title:
            lines.append(f"标题: {title}")
        for name in ("description", "keywords"):
            meta = soup.find("meta", attrs={"name": name})
            if meta and meta.get("content"):
                lines.append(f"{name}: {meta['content'].strip()}")
        for p in soup.find_all(["p", "h1", "h2", "h3"]):
            t = p.get_text(" ", strip=True)
            if t:
                lines.append(t)
        return "\n".join(lines)

    @staticmethod
    def _normalize(data: dict) -> dict:
        """规整 LLM 输出：type 归一到白名单，list 字段 join 成字符串，清理空值。"""
        t = data.get("type")
        if t not in _TYPE_WHITELIST:
            data["type"] = "C2C 闲置交易"  # 兜底默认
        # 文本字段若被 LLM 返回为 list，则拼接成字符串
        _text_fields = (
            "positioning",
            "features",
            "target_users",
            "business_model",
            "pricing",
            "strengths",
            "weaknesses",
        )
        for key in _text_fields:
            val = data.get(key)
            if isinstance(val, list):
                data[key] = "；".join(str(x) for x in val if x)
        # tags 必须是 list
        tags = data.get("tags")
        if isinstance(tags, str):
            data["tags"] = [tags]
        for key in list(data.keys()):
            if data[key] == "" or data[key] is None:
                if key != "tags":
                    data[key] = None
        return data


class RuleExtractor:
    """纯规则提炼兜底：从 HTML 中抽取 title / meta description / 正文。"""

    # 纯本地规则，不产生费用
    billable = False

    async def extract(self, url: str, html: str, title: str = "") -> Extraction:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if not title and soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not title:
            title = url

        desc = ""
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            desc = meta["content"].strip()

        # 正文：取 h2/段落文本，截取前几段
        parts = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        body = " ".join(parts)[:2000]

        return Extraction(
            name=title,
            type="C2C 闲置交易",
            positioning=desc or None,
            features=body or None,
        )


class FallbackProvider:
    """远程优先、失败回退规则。

    背景（R1 LLM 降级策略）：远程网关可能因欠费（402/403）、凭证失效（401）、
    限流（429）、超时或 5xx 而不可用。若不兜底，竞品抓取链路会直接失败，用户
    拿不到任何草稿。此包装保证「远程能用就用远程，用不了就退回规则提取」，链路不中断。
    """

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        # 与 primary 保持一致：是否计费由远程侧决定
        self.billable = getattr(primary, "billable", False)
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        # 本次是否实际发生了回退（供调用方/测试观察）
        self.fell_back = False

    async def extract(self, url: str, html: str, title: str = "") -> Extraction:
        self.fell_back = False
        try:
            result = await self.primary.extract(url=url, html=html, title=title)
        except Exception as exc:
            self.fell_back = True
            self.last_input_tokens = 0
            self.last_output_tokens = 0
            logger.warning(
                "远程 LLM 提炼失败，回退规则提取: %s: %s", type(exc).__name__, exc
            )
            return await self.secondary.extract(url=url, html=html, title=title)

        self.last_input_tokens = getattr(self.primary, "last_input_tokens", 0)
        self.last_output_tokens = getattr(self.primary, "last_output_tokens", 0)
        return result


def get_provider() -> LLMProvider:
    """工厂：配置了 LLM key 则用远程提炼（失败自动回退规则），否则纯规则兜底。"""
    if is_llm_configured():
        return FallbackProvider(RemoteLLMProvider(), RuleExtractor())
    return RuleExtractor()
