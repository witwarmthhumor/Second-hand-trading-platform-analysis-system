"""生成侧实验的 LLM / Embedding 适配层。

- LLM：走 `backend/.env` 的 OpenAI 兼容网关（当前 rightapi gemini 通道）。
  注意 gemini-3.8-flash 是思考型模型：思考 token 会占用输出额度，
  max_tokens 给小了正文会被截断（实测 <1000 会被思考吃光）。
- Embedding：复用项目 bge-m3（Ollama 127.0.0.1:11434），实现 ragas 需要的
  langchain ``Embeddings`` 接口（AnswerRelevancy 需要 embedding 计算「生成问题
  与原始问题的余弦相似度」）。

单独抽出来的原因：run_eval.py（检索侧）只用非 LLM 指标，不依赖任何 LLM；
生成侧评估才需要 LLM，故隔离到本模块，避免检索侧实验被网关可用性牵连。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

from app.rag.embedding import embed_batched, query_embedding
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper

# 生成侧默认输出上限：思考 token（实测单问 600~1500）+ 正文余量
DEFAULT_MAX_TOKENS = 4096

# 这些状态码代表「鉴权/欠费/余额」类问题——重试无意义，应立即中止整轮评估
# 并保留已完成的部分结果（实测 rightapi 余额耗尽时返回 403 {"error":"余额不足"}）。
FATAL_STATUS = {401, 402, 403}

# 文本层面的致命错误特征（网关可能把欠费包装成 429 或普通异常）
_FATAL_HINTS = (
    "余额不足",
    "insufficient_quota",
    "insufficient balance",
    "credited balance",
    "invalid api key",
    "unauthorized",
)


class LLMFatalError(RuntimeError):
    """网关鉴权/欠费类错误：重试无意义，应中止并保存部分结果。"""


def is_fatal_llm_error(exc: BaseException) -> bool:
    """判断异常是否属于「重试也不会好」的致命错误。

    兼容两类来源：本模块 httpx 直调抛出的异常，以及 langchain/openai 客户端
    （RAGAS 裁判走 ChatOpenAI）抛出的 APIStatusError。
    """
    if isinstance(exc, LLMFatalError):
        return True
    for candidate in (exc, getattr(exc, "response", None)):
        if candidate is not None and getattr(candidate, "status_code", None) in FATAL_STATUS:
            return True
    text = str(exc).lower()
    return any(h in text for h in _FATAL_HINTS)


class BgeM3Embeddings(Embeddings):
    """把项目 bge-m3 适配成 langchain Embeddings（供 ragas AnswerRelevancy 用）。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量文本 embedding（走 Ollama，批内 ≤32）。"""
        return [list(v) for v in embed_batched(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        """单条查询 embedding。"""
        return list(query_embedding(text))


def build_judge_llm(max_tokens: int = DEFAULT_MAX_TOKENS) -> LangchainLLMWrapper:
    """构造 RAGAS 裁判 LLM（temperature=0，判定可复现）。"""
    llm = ChatOpenAI(
        model=os.getenv("LLM_MODEL", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_API_BASE", ""),
        temperature=0,
        max_tokens=max_tokens,
    )
    return LangchainLLMWrapper(llm)


def build_judge_embeddings() -> LangchainEmbeddingsWrapper:
    """构造 RAGAS 裁判 embedding（bge-m3）。"""
    return LangchainEmbeddingsWrapper(BgeM3Embeddings())


async def chat(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    timeout: float = 120.0,
    attempts: int = 3,
) -> str:
    """一次 OpenAI 兼容对话调用（非流式），返回正文。

    检索/评测都是同步 httpx 调用，这里用 to_thread 避免阻塞事件循环，
    以便外层用 asyncio 并发压时间。

    抖动类错误（超时/5xx/429）按指数退避重试 ``attempts`` 次；
    鉴权/欠费类（见 ``FATAL_STATUS``）直接抛 ``LLMFatalError`` 不再重试。
    """
    import httpx

    def _call() -> str:
        base = os.getenv("LLM_API_BASE", "").rstrip("/")
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('LLM_API_KEY', '')}",
            },
            json={
                "model": os.getenv("LLM_MODEL", ""),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        if resp.status_code in FATAL_STATUS:
            raise LLMFatalError(f"{resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    last: Exception | None = None
    for i in range(attempts):
        try:
            return await asyncio.to_thread(_call)
        except LLMFatalError:
            raise
        except Exception as exc:  # noqa: BLE001 抖动类错误统一在此退避重试
            last = exc
            if i < attempts - 1:
                await asyncio.sleep(min(2**i, 8))
    raise last  # type: ignore[misc]
