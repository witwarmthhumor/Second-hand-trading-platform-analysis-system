"""RAG 问答服务：混合检索命中 → 原文直出 + 来源；未命中 → LLM 兜底生成。

检索命中判定：最高 RRF 得分 ≥ SIMILARITY_THRESHOLD 视为命中，直接返回原文；
否则调用 LLM 兜底（OpenAI 兼容接口，复用 settings 的 LLM 配置与成本熔断记账）。
"""

from __future__ import annotations

import httpx

from app.ai.cost import get_cost_tracker
from app.config import settings
from app.rag.config import SIMILARITY_THRESHOLD
from app.rag.retriever import get_retriever
from app.schemas.rag import AskResponse, SourceDoc

_FALLBACK_SYSTEM_PROMPT = (
    "你是二手交易平台知识库的智能助手。请根据用户问题给出简洁、准确、有帮助的回答。"
    "如果知识库中没有相关信息，请据实说明，不要编造具体费率、时限或制度条款。"
    "回答使用中文，控制在 200 字以内。"
)


def _estimate_tokens(text: str) -> int:
    """极简 token 估算（中英文混合按字符数/2 粗估）。"""
    return max(1, len(text) // 2)


async def _llm_fallback(question: str) -> str:
    """未命中时调用 LLM 兜底生成，接入成本熔断。"""
    tracker = get_cost_tracker()

    # 成本预检：预估输入 token，超限且未审批则中断
    est_input = _estimate_tokens(_FALLBACK_SYSTEM_PROMPT + question)
    pre = tracker.precheck(est_input)
    if pre.get("blocked"):
        return "（已触发成本熔断，需在「成本」页确认后继续。当前问题未调用大模型。）"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{settings.llm_api_base.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.llm_api_key}",
            },
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or est_input)
        output_tokens = int(usage.get("completion_tokens") or _estimate_tokens(content))
        tracker.record(input_tokens, output_tokens)

    return content.strip()


async def ask(question: str, top_k: int = 5) -> AskResponse:
    """问答主入口：检索命中直出，未命中 LLM 兜底。"""
    retriever = get_retriever()
    if not retriever.is_ready:
        return AskResponse(
            answer="知识库尚未就绪，请先运行建库脚本（scripts/build_vector_store.py）。",
            mode="fallback",
            sources=[],
            question=question,
        )

    results = retriever.search(question, top_k=top_k)

    # 命中判定：最高向量相似度 ≥ 阈值（cosine 相似度 0~1，语义相关查询约 0.7+，
    # 无关查询约 0.48；RRF 得分量级仅 0.016~0.033，不宜作阈值）
    if results and results[0]["vector_sim"] >= SIMILARITY_THRESHOLD:
        top = results[0]
        # 按 doc_id 去重（同一文档多个 chunk 可能同时进 Top-K）
        seen: set[str] = set()
        sources: list[SourceDoc] = []
        for r in results:
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
        return AskResponse(
            answer=top["text"],
            mode="hit",
            sources=sources,
            question=question,
        )

    # 未命中：LLM 兜底
    answer = await _llm_fallback(question)
    return AskResponse(
        answer=answer,
        mode="fallback",
        sources=[],
        question=question,
    )


def _title_from_doc_id(doc_id: str) -> str:
    """从 doc_id 反查标题（doc_id 即 seeds CSV 的 id，如 A-001）。"""
    from app.rag.chunking import _seed_by_id

    seed = _seed_by_id().get(doc_id)
    return seed.title if seed else doc_id
