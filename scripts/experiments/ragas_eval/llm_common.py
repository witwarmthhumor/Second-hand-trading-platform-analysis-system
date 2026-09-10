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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

from app.rag.embedding import embed_batched, query_embedding
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper

# 生成侧默认输出上限：思考 token（实测单问 600~1500）+ 正文余量
DEFAULT_MAX_TOKENS = 4096

# ---------------------------------------------------------------------------
# 成本核算与预算闸门
# ---------------------------------------------------------------------------
# deepseek-flash 官方定价（美元 / 百万 tokens），此处为**高峰价、缓存未命中价**：
# 输入 $0.30 / 百万，输出 $1.20 / 百万。
# 低谷时段（官方：UTC 周一至周五 01:00-04:00、06:00-10:00 为高峰，其余为低谷）
# 价格减半——按调用发生时刻逐笔计价，避免用高峰期价格虚高估算、误触闸门。
PRICE_IN_USD_PER_M = 0.30
PRICE_OUT_USD_PER_M = 1.20
OFF_PEAK_DISCOUNT = 0.5
USD_TO_CNY = 7.1
DEFAULT_BUDGET_CNY = 2.0

# 高峰时段（UTC 小时区间，仅周一至周五）
_PEAK_HOURS_UTC = ((1, 4), (6, 10))


def is_peak_now() -> bool:
    """当前是否处于 DeepSeek 高峰计费时段（UTC 周一至周五 01-04 与 06-10 点）。"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # 周六/周日全天低谷
        return False
    return any(start <= now.hour < end for start, end in _PEAK_HOURS_UTC)


class BudgetExceeded(RuntimeError):
    """预估花费超过授权预算，主动中止（而非继续烧钱）。"""


class UsageTracker:
    """累计所有 LLM 调用的 token 用量，按 **调用时刻的真实时段价** 折算人民币成本。

    覆盖两条调用路径：本模块 httpx 直调（``chat``）与 langchain 裁判
    （``_UsageCallback`` 回调），二者都汇入全局单例 ``USAGE``。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """清零统计（新开一轮评估前调用）。"""
        self.calls = 0
        self.inp = 0
        self.out = 0
        self._cost_cny = 0.0

    def add(self, inp: int, out: int) -> None:
        """累加一次调用的输入/输出 token，并按当时的时段价折算花费。"""
        inp, out = int(inp or 0), int(out or 0)
        self.calls += 1
        self.inp += inp
        self.out += out
        rate = 1.0 if is_peak_now() else OFF_PEAK_DISCOUNT
        usd = (inp / 1e6 * PRICE_IN_USD_PER_M + out / 1e6 * PRICE_OUT_USD_PER_M) * rate
        self._cost_cny += usd * USD_TO_CNY

    @property
    def cost_cny(self) -> float:
        """累计花费（人民币元，按逐笔时段价）。"""
        return self._cost_cny

    def summary(self) -> str:
        """一行可读的用量/花费摘要。"""
        period = "高峰" if is_peak_now() else "低谷(半价)"
        return (
            f"{self.calls} 次调用 / 入 {self.inp} / 出 {self.out} tokens "
            f"≈ ¥{self.cost_cny:.3f} [{period}]"
        )


USAGE = UsageTracker()


class _UsageCallback(BaseCallbackHandler):
    """把 langchain（RAGAS 裁判）的 token 用量汇入全局 ``USAGE``。"""

    def on_llm_end(self, response, **kwargs) -> None:
        for gen_list in response.generations:
            for gen in gen_list:
                meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if meta:
                    USAGE.add(meta.get("input_tokens", 0), meta.get("output_tokens", 0))

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
    """构造 RAGAS 裁判 LLM（temperature=0，判定可复现），并挂上用量统计回调。"""
    llm = ChatOpenAI(
        model=os.getenv("LLM_MODEL", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_API_BASE", ""),
        temperature=0,
        max_tokens=max_tokens,
        callbacks=[_UsageCallback()],
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
        payload = resp.json()
        usage = payload.get("usage") or {}
        USAGE.add(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return payload["choices"][0]["message"]["content"] or ""

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
