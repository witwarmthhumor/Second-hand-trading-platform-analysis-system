"""RAG 问答服务：混合检索命中 → 原文直出 + 来源；未命中 → LLM 兜底生成。

检索命中判定：最高「向量相似度」≥ SIMILARITY_THRESHOLD 视为命中，直接返回原文；
否则调用 LLM 兜底（OpenAI 兼容接口，复用 settings 的 LLM 配置与成本熔断记账）。
注意：判据是向量相似度而非 RRF 得分——RRF 量级仅 0.016~0.033，无法做绝对阈值。
"""

from __future__ import annotations

import asyncio

import httpx

from app.ai.cost import get_cost_tracker
from app.config import settings
from app.rag.cache import SingleFlight, get_search_cache
from app.rag.config import SIMILARITY_THRESHOLD
from app.rag.retriever import HybridRetriever, get_retriever
from app.schemas.rag import AskResponse, SourceDoc

# 同 key 并发合并：热点 key 刚过期时，只放 1 个请求回源，其余复用同一结果
_RETRIEVE_FLIGHT = SingleFlight()

_FALLBACK_SYSTEM_PROMPT = (
    "你是二手交易平台知识库的智能助手。请根据用户问题给出简洁、准确、有帮助的回答。"
    "如果知识库中没有相关信息，请据实说明，不要编造具体费率、时限或制度条款。"
    "回答使用中文，控制在 200 字以内。"
)


def _estimate_tokens(text: str) -> int:
    """极简 token 估算（中英文混合按字符数/2 粗估）。"""
    return max(1, len(text) // 2)


def _build_fallback_messages(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
) -> list[dict]:
    """组装 LLM 兜底的 messages（system + 历史 + 当前问题），供流式/非流式复用。"""
    system_prompt = _FALLBACK_SYSTEM_PROMPT
    if competitors_context:
        system_prompt += f"\n\n以下是系统中已录入的竞品数据（供对比类问题参考）：\n{competitors_context}"
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in history or []:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


async def _llm_fallback_stream(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
):
    """未命中时 LLM 兜底流式生成（SSE 数据源）。

    逐 chunk yield 增量文本；结束时 yield usage（成本记账用）。
    成本熔断 blocked 时 yield 提示文本后结束。LLM 异常时 yield 降级提示后结束。
    """
    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    messages = _build_fallback_messages(question, history, competitors_context)

    est_input = _estimate_tokens(
        _FALLBACK_SYSTEM_PROMPT + "".join(m.get("content", "") for m in messages[1:])
    )
    pre = tracker.precheck(est_input)
    if pre.get("blocked"):
        yield "（已触发成本熔断，需在「成本」页确认后继续。当前问题未调用大模型。）"
        return

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                f"{settings.llm_api_base.rstrip('/')}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.llm_api_key}",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": 0.3,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                usage: dict = {}
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    import json as _json

                    try:
                        chunk = _json.loads(data)
                    except ValueError:
                        continue
                    # 流式 chunk 可能带 usage（部分网关在最后一个 chunk 给）
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content

            # 记账：优先真实 usage，否则估算
            input_tokens = int(usage.get("prompt_tokens") or est_input)
            output_tokens = int(usage.get("completion_tokens") or 0)
            if output_tokens > 0 or not usage:
                tracker.record(input_tokens, output_tokens)
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        yield f"\n\n（兜底大模型暂时不可用：{type(exc).__name__}，请稍后重试。）"


def build_competitors_context(competitors: list) -> str:
    """把已入库竞品数据序列化为文本，供 LLM 兜底回答对比类问题。

    只取对问答有价值的字段（名称/类型/定位/商业模式/定价/优势/不足），
    控制长度避免 token 膨胀。competitors 为 Competitor ORM 实例列表。
    """
    lines: list[str] = []
    for c in competitors:
        fields: list[str] = [f"【{c.name}】"]
        if c.type:
            fields.append(f"类型：{c.type}")
        if c.positioning:
            fields.append(f"定位：{c.positioning}")
        if c.business_model:
            fields.append(f"商业模式：{c.business_model}")
        if c.pricing:
            fields.append(f"定价/抽佣：{c.pricing}")
        if c.strengths:
            fields.append(f"优势：{c.strengths}")
        if c.weaknesses:
            fields.append(f"不足：{c.weaknesses}")
        lines.append("；".join(fields))
    return "\n".join(lines)


async def _llm_fallback(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
) -> str:
    """未命中时调用 LLM 兜底生成，接入成本熔断；可注入多轮历史与竞品数据上下文。"""
    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    # 组装带历史的 messages：system（含竞品上下文）+ 最近若干轮历史 + 当前问题
    messages = _build_fallback_messages(question, history, competitors_context)

    # 成本预检：预估输入 token（system + 历史 + 问题），超限且未审批则中断
    est_input = _estimate_tokens(
        _FALLBACK_SYSTEM_PROMPT + "".join(m.get("content", "") for m in messages[1:])
    )
    pre = tracker.precheck(est_input)
    if pre.get("blocked"):
        return "（已触发成本熔断，需在「成本」页确认后继续。当前问题未调用大模型。）"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.llm_api_base.rstrip('/')}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.llm_api_key}",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            usage = data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or est_input)
            output_tokens = int(
                usage.get("completion_tokens") or _estimate_tokens(content)
            )
            tracker.record(input_tokens, output_tokens)

        return content.strip()
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        # LLM 兜底失败时优雅降级，不让接口抛 500
        return (
            "抱歉，知识库中未找到与您问题直接相关的内容，且兜底大模型暂时不可用"
            f"（{type(exc).__name__}）。请稍后重试，或换个更具体的问题。"
        )


async def retrieve(
    question: str,
    top_k: int = 5,
    retriever: HybridRetriever | None = None,
) -> list[dict]:
    """带缓存 + singleflight 的检索：命中缓存直接返回，未命中才真正回源。

    分层防护：
    - 缓存：热点问题命中时免 embedding + 向量/BM25 检索（key 含语料版本与 top_k）
    - singleflight：同一 key 的并发 miss 只放一个回源，其余 await 同一结果（防击穿）
    - 线程池：检索是同步阻塞调用（Ollama HTTP + Chroma），放到线程里避免堵事件循环
    """
    rtv = retriever if retriever is not None else get_retriever()
    cache = get_search_cache()
    # 语料版本：mock/未实现该属性的检索器按空版本处理，不影响缓存可用性
    version = getattr(rtv, "corpus_version", "")
    cached = await cache.get(question, top_k, version)
    if cached is not None:
        return cached

    async def _compute() -> list[dict]:
        # 二次检查：等锁期间可能已被其它请求写入
        again = await cache.get(question, top_k, version)
        if again is not None:
            return again
        results = await asyncio.to_thread(rtv.search, question, top_k)
        await cache.set(question, results, top_k, version)
        return results

    return await _RETRIEVE_FLIGHT.run(cache.key_for(question, top_k, version), _compute)


def build_hit_gate(results: list[dict]) -> dict | None:
    """命中判定 + 来源构造（非流式 ask 与流式端点共用，避免两处逻辑各自漂移）。

    返回 None 表示未命中，调用方应走 LLM 兜底；否则返回 {"answer", "sources"}。

    判定用最高向量相似度（cosine 0~1），不用 RRF 得分——RRF 量级仅 0.016~0.033，
    无法做绝对阈值。来源列表按同一阈值过滤：BM25 单独命中的 chunk 其 vector_sim
    为 0，不过滤就会以「相关度 0.0%」混进来源，属于无意义引用。
    """
    if not results or results[0]["vector_sim"] < SIMILARITY_THRESHOLD:
        return None

    seen: set[str] = set()  # 同一文档多个 chunk 可能同时进 Top-K，按 doc_id 去重
    sources: list[SourceDoc] = []
    for r in results:
        if r["vector_sim"] < SIMILARITY_THRESHOLD:
            continue
        if r["doc_id"] in seen:
            continue
        seen.add(r["doc_id"])
        sources.append(
            SourceDoc(
                doc_id=r["doc_id"],
                title=_title_from_doc_id(r["doc_id"]),
                text=r["text"],
                score=r["vector_sim"],
            )
        )
    # results[0] 已过阈值，故 sources 必非空；此处仅作防御，避免「命中但无来源」的空壳回答。
    if not sources:
        return None
    return {"answer": results[0]["text"], "sources": sources}


async def ask(
    question: str,
    top_k: int = 5,
    session_id: str | None = None,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
) -> AskResponse:
    """问答主入口：检索命中直出，未命中 LLM 兜底（可注入多轮历史与竞品数据）。"""
    retriever = get_retriever()
    if not retriever.is_ready:
        return AskResponse(
            answer="知识库尚未就绪，请先运行建库脚本（scripts/build_vector_store.py）。",
            mode="fallback",
            sources=[],
            question=question,
            session_id=session_id,
        )

    results = await retrieve(question, top_k=top_k, retriever=retriever)

    hit = build_hit_gate(results)
    if hit is not None:
        return AskResponse(
            answer=hit["answer"],
            mode="hit",
            sources=hit["sources"],
            question=question,
            session_id=session_id,
        )

    # 未命中：LLM 兜底（注入历史上下文与竞品数据，支持追问与对比类问题）
    answer = await _llm_fallback(
        question, history=history, competitors_context=competitors_context
    )
    return AskResponse(
        answer=answer,
        mode="fallback",
        sources=[],
        question=question,
        session_id=session_id,
    )


def _title_from_doc_id(doc_id: str) -> str:
    """从 doc_id 反查标题（doc_id 即 seeds CSV 的 id，如 A-001）。"""
    from app.rag.chunking import _seed_by_id

    seed = _seed_by_id().get(doc_id)
    return seed.title if seed else doc_id
