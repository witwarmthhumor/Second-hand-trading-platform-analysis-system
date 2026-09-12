"""RAG 问答服务：混合检索命中 → 合成/直出 + 来源；未命中 → LLM 兜底生成。

命中后的三态（P1-②）：``synth``（SYNTH_MODE 触发时用深度视图合成，失败/拒答
一律降级）→ ``hit``（直出 top1 原文）→ 未命中 ``fallback``（LLM 兜底，可注入
临界片段/竞品数据/多轮历史）。

检索命中判定：最高「向量相似度」≥ SIMILARITY_THRESHOLD 视为命中，直接返回原文；
否则调用 LLM 兜底（OpenAI 兼容接口，复用 settings 的 LLM 配置与成本熔断记账）。
注意：判据是向量相似度而非 RRF 得分——RRF 量级仅 0.016~0.033，无法做绝对阈值。
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from app.ai.cost import get_cost_tracker
from app.ai.provider import is_llm_configured
from app.config import settings
from app.metrics import get_metrics
from app.rag.cache import SingleFlight, get_search_cache
from app.rag.config import (
    FALLBACK_CONTEXT_MAX_CHUNKS,
    FALLBACK_CONTEXT_MIN_SIM,
    SIMILARITY_THRESHOLD,
    SYNTH_MODE,
    SYNTH_REFUSED_PHRASE,
)
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


def _llm_error_hint(exc: Exception) -> str:
    """把 LLM 调用异常翻译成可读原因。

    区分「欠费/凭证失效/限流/超时/网络」几类，用户与运维据此能立刻判断该充值、
    改配置还是等待，而不是只看到一串异常类名（R1 LLM 降级策略）。
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        if status == 401:
            return "大模型凭证无效（401），请检查 LLM_API_KEY"
        if status in (402, 403):
            return f"大模型账户余额不足或无权访问（{status}）"
        if status == 429:
            return "大模型触发限流（429），请稍后重试"
        if status >= 500:
            return f"大模型服务异常（{status}）"
        return f"大模型调用被拒绝（{status}）"
    if isinstance(exc, httpx.TimeoutException):
        return "大模型响应超时"
    if isinstance(exc, httpx.HTTPError):
        return f"大模型网络异常（{type(exc).__name__}）"
    return f"未知错误（{type(exc).__name__}）"


# 未配置 LLM 时兜底路径的统一提示（避免每次走到 HTTP 调用才失败）
_LLM_NOT_CONFIGURED_MSG = (
    "知识库中未找到直接相关的内容，且当前未配置兜底大模型（LLM_API_KEY 为空）。"
    "请联系管理员配置后重试，或换个更贴近知识库的问题。"
)

# 拒答档：检索相似度连 FALLBACK_CONTEXT_MIN_SIM 都达不到时，直接回这句、不调 LLM。
# 此时模型没有可用的知识库依据，调 LLM 只会得到「凭参数知识编造」的答案。
NO_KB_CONTENT_MSG = (
    "知识库中暂无与该问题相关的内容。为避免给出不准确的信息，本次不作推测性回答；"
    "建议换一个更贴近产品规则/流程的问法，或补充具体场景（如「卖家入驻」「退款」「验货」）。"
)


def build_fallback_context(
    results: list[dict],
    min_sim: float = FALLBACK_CONTEXT_MIN_SIM,
    max_chunks: int = FALLBACK_CONTEXT_MAX_CHUNKS,
) -> list[SourceDoc] | None:
    """从检索结果中挑出「临界片段」，供兜底路径注入 prompt（P1）。

    临界片段 = 相似度落在 `[min_sim, SIMILARITY_THRESHOLD)` 的片段：达不到「命中直出」
    的标准，但又不是全无关系，可作为 LLM 的参考线索（注入时会标注相似度）。

    返回 `None` 表示**连临界标准都达不到**（结果为空，或最高相似度 < `min_sim`）——
    调用方应直接拒答、不调用 LLM：这种输入下模型只能凭参数知识作答。

    来源按 `doc_id` 去重（同文档多 chunk 只留最相似的一个），最多 `max_chunks` 个。
    纯函数，便于单测。
    """
    if not results or results[0]["vector_sim"] < min_sim:
        return None

    seen: set[str] = set()
    picked: list[SourceDoc] = []
    for r in results:
        # 只取灰区 [min_sim, SIMILARITY_THRESHOLD)：≥阈值属「命中直出」，由 build_hit_gate 负责
        if r["vector_sim"] < min_sim or r["vector_sim"] >= SIMILARITY_THRESHOLD:
            continue
        if r["doc_id"] in seen:
            continue
        seen.add(r["doc_id"])
        picked.append(
            SourceDoc(
                doc_id=r["doc_id"],
                title=_title_from_doc_id(r["doc_id"]),
                text=r["text"],
                score=r["vector_sim"],
            )
        )
        if len(picked) >= max_chunks:
            break
    return picked or None


def format_fallback_context(docs: list[SourceDoc]) -> str:
    """把临界片段序列化为注入 system prompt 的文本（带来源与相似度标注）。"""
    return "\n\n".join(
        f"【片段 {i}｜来源 {d.doc_id}｜相似度 {d.score:.2f}】{d.text}"
        for i, d in enumerate(docs, 1)
    )


def _build_fallback_messages(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
    retrieved_context: str | None = None,
) -> list[dict]:
    """组装 LLM 兜底的 messages（system + 历史 + 当前问题），供流式/非流式复用。"""
    system_prompt = _FALLBACK_SYSTEM_PROMPT
    if competitors_context:
        system_prompt += f"\n\n以下是系统中已录入的竞品数据（供对比类问题参考）：\n{competitors_context}"
    if retrieved_context:
        # P1：兜底路径注入临界片段，让模型「有据可依」，而不是凭参数知识作答。
        system_prompt += (
            "\n\n以下是从知识库检索到的、与问题**可能相关**的片段（相似度不高，仅供参考）：\n"
            f"{retrieved_context}\n\n"
            "请**优先基于上述片段**作答；片段未覆盖的信息，请直接说明知识库暂无相关内容，"
            "不要编造费率、时限或制度条款。片段之间若信息冲突，以相似度更高的为准。"
        )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in history or []:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


class LLMCallError(Exception):
    """LLM 调用失败（超时/网络/HTTP 错误），``hint`` 是给用户/运维的可读原因。"""

    def __init__(self, hint: str) -> None:
        super().__init__(hint)
        self.hint = hint


# 成本熔断被预检拦截时给调用方的统一提示（兜底路径直接展示给用户）
_COST_BLOCKED_MSG = (
    "（已触发成本熔断，需在「成本」页确认后继续。当前问题未调用大模型。）"
)


def _precheck_blocked(tracker, messages: list[dict]) -> str | None:
    """成本预检：放行返回 None；被熔断拦截返回统一提示文本（调用方按各自语义处理）。

    估算范围含全部 messages（注入的临界片段/合成上下文），避免低估触发超支。
    """
    est_input = _estimate_tokens("".join(str(m.get("content", "")) for m in messages))
    pre = tracker.precheck(est_input)
    if pre.get("blocked"):
        return _COST_BLOCKED_MSG
    # 放行时把估算值带回给调用方记账（通过 tracker 无法传递，改用闭包属性会隐晦，
    # 故让调用方自行再算一次——极轻量，且保持各函数签名清晰）
    return None


def _record_usage(tracker, usage: dict, est_input: int, output_fallback: int) -> None:
    """统一记账：真实 usage 优先，缺失字段用估算兜底。

    流式路径的 output_fallback 传「累计 delta 的估算值」，与非流式（按完整正文
    估算）语义对齐——此前流式在网关只回部分 usage 时会整单漏记。
    """
    input_tokens = int(usage.get("prompt_tokens") or est_input)
    output_tokens = int(usage.get("completion_tokens") or output_fallback)
    tracker.record(input_tokens, output_tokens)


def _est_input_tokens(messages: list[dict]) -> int:
    """按全部 messages 的字符数粗估输入 token（供预检与记账兜底共用）。"""
    return _estimate_tokens("".join(str(m.get("content", "")) for m in messages))


async def _post_llm(messages: list[dict]) -> tuple[str, dict]:
    """非流式 LLM 调用公共层（OpenAI 兼容 chat/completions）。

    返回 (正文, usage)。超时/网络/HTTP 错误统一抛 ``LLMCallError``（hint 可读），
    由调用方按各自失败语义处理（兜底返回错误文案，合成返回 None 降级直出）。
    """
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
        return (content or ""), usage
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        raise LLMCallError(_llm_error_hint(exc)) from exc


async def _stream_llm_deltas(messages: list[dict], usage_out: dict):
    """流式 LLM 调用公共层：逐条 yield content 增量；usage 回填进 ``usage_out``。

    与 ``_post_llm`` 共用同一套端点/参数；超时/网络/HTTP 错误抛 ``LLMCallError``。
    调用方在消费增量时自行维护「已产出」状态，以决定失败时的降级形态。
    """
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
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                # 流式 chunk 可能带 usage（部分网关在最后一个 chunk 给）
                if chunk.get("usage"):
                    usage_out.update(chunk["usage"])
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    yield content


async def llm_fallback_stream(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
    retrieved_context: str | None = None,
):
    """未命中时 LLM 兜底流式生成（SSE 数据源）。

    逐 chunk yield 增量文本；结束时统一记账（含 delta 估算兜底）。
    成本熔断 blocked 时 yield 提示文本后结束。LLM 异常时 yield 降级提示后结束：
    已产出部分内容则追加提示（不换稿），否则单独 yield 提示文本。

    `retrieved_context`：P1 注入的临界片段文本（见 `build_fallback_context`）。
    """
    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    messages = _build_fallback_messages(
        question, history, competitors_context, retrieved_context
    )

    # 未配置 LLM：直接给出明确提示，不白跑一次 HTTP 调用
    if not is_llm_configured():
        yield _LLM_NOT_CONFIGURED_MSG
        return

    blocked = _precheck_blocked(tracker, messages)
    if blocked is not None:
        yield blocked
        return

    est_input = _est_input_tokens(messages)
    usage: dict = {}
    produced_parts: list[str] = []
    try:
        async for delta in _stream_llm_deltas(messages, usage):
            produced_parts.append(delta)
            yield delta
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        hint = _llm_error_hint(exc)
        if produced_parts:
            yield f"\n\n（兜底大模型暂时不可用：{hint}，请稍后重试。）"
        else:
            yield f"（兜底大模型暂时不可用：{hint}，请稍后重试。）"
        return
    _record_usage(tracker, usage, est_input, _estimate_tokens("".join(produced_parts)))


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


async def llm_fallback(
    question: str,
    history: list[dict] | None = None,
    competitors_context: str | None = None,
    retrieved_context: str | None = None,
) -> str:
    """未命中时调用 LLM 兜底生成，接入成本熔断；可注入多轮历史、竞品与检索片段。

    `retrieved_context`：P1 注入的临界片段文本（见 `build_fallback_context`）。
    """
    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    # 组装带历史的 messages：system（含竞品/检索片段上下文）+ 历史 + 当前问题
    messages = _build_fallback_messages(
        question, history, competitors_context, retrieved_context
    )

    # 未配置 LLM：直接返回明确提示，不白跑一次 HTTP 调用
    if not is_llm_configured():
        return _LLM_NOT_CONFIGURED_MSG

    blocked = _precheck_blocked(tracker, messages)
    if blocked is not None:
        return blocked

    est_input = _est_input_tokens(messages)
    try:
        content, usage = await _post_llm(messages)
    except LLMCallError as exc:
        # LLM 兜底失败时优雅降级，不让接口抛 500
        return (
            "抱歉，知识库中未找到与您问题直接相关的内容，且兜底大模型暂时不可用"
            f"（{exc.hint}）。请稍后重试，或换个更具体的问题。"
        )
    _record_usage(tracker, usage, est_input, _estimate_tokens(content))
    return content.strip()


# ---------------------------------------------------------------------------
# 合成答案路径（P1-②，docs/合成答案分流方案.md）
# ---------------------------------------------------------------------------
# 命中（build_hit_gate）后的可选增强：用深度视图（DEPTH_QUOTA 名额分配）作上下文
# 调 LLM 合成，来源列表仍用广度视图。任何失败（LLM 不可用/熔断/拒答/剥标为空）
# 一律降级直出 top1 原文——直出永远是最安全的兜底。


def _observe_synth(outcome: str) -> None:
    """合成路径事件计数（trigger/ok/degraded/refused），/metrics 可观测。"""
    get_metrics().observe_synth(outcome)


def should_synthesize(sources: list[SourceDoc]) -> bool:
    """分流判据（P1-②）：命中后是否走合成。

    - ``off``：不合成（默认，现网逐字节不变）；
    - ``multi_doc``：广度视图 sources ≥ 2 篇（多文档信号，检索结果的被动产物，
      零成本、不依赖相似度阈值——bge-m3 相似度不可做细粒度分级，P0-2 证伪）；
    - ``all``：命中即合成。
    触发时计一次 ``trigger`` 指标（非流式/流式两条路径自动统一）。
    """
    if SYNTH_MODE == "all":
        _observe_synth("trigger")
        return True
    if SYNTH_MODE == "multi_doc" and len(sources) >= 2:
        _observe_synth("trigger")
        return True
    return False


_SYNTH_SYSTEM_PROMPT = (
    "你是二手交易平台（闲鱼、转转等）的问答助手。请严格依据「知识片段」回答用户问题。\n\n"
    "要求：\n"
    "1. 只使用知识片段中的信息作答，不得引入片段之外的任何知识，不得编造数字、比例或规则。\n"
    "2. 若知识片段不足以回答问题，直接回复「知识库中未找到相关信息」，不要勉强作答。\n"
    "3. 若片段来自多个文档，请分点归纳，覆盖每一条与问题相关的信息，不要遗漏。\n"
    "4. 每个要点末尾用【来源:doc_id】标注依据（doc_id 见片段标签），不要编造来源。\n"
    "5. 语言简洁，直接给答案，不要复述问题、不要输出多余前缀。"
)


def _build_synth_messages(question: str, depth_results: list[dict]) -> list[dict]:
    """合成答案的 messages：片段带【doc_id】标签。

    与评测 ``gen_answers.build_synth_messages``（746ebbc 实测 faith 0.9682 的 v1
    形态）同构，避免评测与生产提示词漂移。
    """
    blocks = [f"【{r['doc_id']}】\n{r['text']}" for r in depth_results]
    context = "\n\n".join(blocks)
    return [
        {"role": "system", "content": _SYNTH_SYSTEM_PROMPT},
        {"role": "user", "content": f"知识片段：\n{context}\n\n用户问题：{question}"},
    ]


# 来源标注形如 【A-001】/【来源:D-161】/【来源：FAQ-049】（与评测 CITE_RE 同构）
_CITE_RE = re.compile(r"【(?:来源[:：])?\s*([A-Za-z]+-\d+)\s*】")


def _strip_citations(text: str) -> tuple[str, list[str]]:
    """剥掉正文内联的【来源:x】标注（方案开放问题 4 的裁定：v1 行为 + 正文剥标）。

    剥标只是**后处理**，不改变 LLM 生成行为——提示词仍是 faith 最高的 v1
    （内联标注引导模型对齐来源）；剥掉是为了正文干净（v2 的动机已证明正文
    夹带标记伤可读性）。返回 (剥标正文, 引用 doc_id 去重保序列表)。
    """
    ids: list[str] = []
    for m in _CITE_RE.finditer(text):
        did = m.group(1)
        if did not in ids:
            ids.append(did)
    cleaned = _CITE_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, ids


def _merge_synth_sources(
    hit_sources: list[SourceDoc], depth_results: list[dict], cited: list[str]
) -> list[SourceDoc]:
    """合成回答的来源 = 广度视图 + 正文引用补齐。

    正文引用的 doc_id 不在广度视图时（模型归纳了深度视图里的其他文档），
    从深度视图取该文档的代表块补进来源；若引用的 doc_id 在两个视图中都
    不存在（模型编造来源），该引用无法补齐——此时来源列表不含它，靠
    拒答检测/剥标后的正文核验兜底。
    """
    sources = list(hit_sources)
    have = {s.doc_id for s in sources}
    for did in cited:
        if did in have:
            continue
        for r in depth_results:
            if r.get("doc_id") == did:
                sources.append(
                    SourceDoc(
                        doc_id=did,
                        title=_title_from_doc_id(did),
                        text=r.get("text", ""),
                        score=r.get("vector_sim", 0.0),
                    )
                )
                break
        have.add(did)
    return sources


def _finalize_text(raw_answer: str) -> tuple[str, list[str]] | None:
    """合成文本后处理的共用规则：拒答检测 + 剥标。

    返回 None 表示输出不可用（空/拒答/剥标后为空），调用方降级直出；
    否则返回 (剥标正文, 引用 doc_id 列表)。非流式与流式共用，保证规则一致。
    """
    raw = (raw_answer or "").strip()
    if not raw or SYNTH_REFUSED_PHRASE in raw:
        return None
    cleaned, cited = _strip_citations(raw)
    if not cleaned:
        return None
    return cleaned, cited


def observe_synth(outcome: str) -> None:
    """供 api 层记录合成事件（ok/refused）；与内部 ``_observe_synth`` 同一指标。"""
    _observe_synth(outcome)


def finalize_stream_synthesis(
    raw_answer: str, hit_sources: list[SourceDoc], depth_results: list[dict]
) -> tuple[str, list[SourceDoc]] | None:
    """流式合成的收尾处理（基于 ``_finalize_text`` + 来源合并）。

    返回 None 表示合成输出不可用（拒答/剥标后为空），调用方降级直出。
    """
    out = _finalize_text(raw_answer)
    if out is None:
        return None
    cleaned, cited = out
    return cleaned, _merge_synth_sources(hit_sources, depth_results, cited)


async def llm_synthesize(
    question: str, depth_results: list[dict]
) -> tuple[str, list[str]] | None:
    """合成命中答案（非流式）。失败/拒答返回 None → 调用方降级直出。

    成本预检/记账/错误翻译与 ``llm_fallback`` 走同一公共层（``_post_llm``）；
    区别仅在提示词与上下文来源（深度视图），以及失败语义：兜底失败返回
    「错误提示文本」，合成失败返回 None（降级直出永远可用，不需要文本兜底）。
    """
    if not depth_results or not is_llm_configured():
        return None
    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    messages = _build_synth_messages(question, depth_results)
    blocked = _precheck_blocked(tracker, messages)
    if blocked is not None:
        return None

    est_input = _est_input_tokens(messages)
    try:
        content, usage = await _post_llm(messages)
    except LLMCallError:
        _observe_synth("degraded")
        return None
    _record_usage(tracker, usage, est_input, _estimate_tokens(content))

    out = _finalize_text(content.strip())
    if out is None:
        _observe_synth("refused")
        return None
    return out


async def llm_synthesize_stream(question: str, depth_results: list[dict]):
    """合成答案的流式生成器：yield ``(kind, text)``。

    - ``("delta", text)``：增量正文（含内联来源标注；调用方在 done 事件给出
      剥标后的干净全文，前端用它替换显示）；
    - ``("failed", reason)``：合成失败——**保证只会在产出任何 delta 之前出现**，
      调用方据此干净降级直出；已产出 delta 后的中途异常以 delta 形式追加错误
      提示（用户已看到部分内容，不再换稿）。
    """
    if not depth_results or not is_llm_configured():
        yield ("failed", "llm 未配置或片段为空")
        return

    tracker = get_cost_tracker()
    await tracker.restore_from_db()

    messages = _build_synth_messages(question, depth_results)
    blocked = _precheck_blocked(tracker, messages)
    if blocked is not None:
        yield ("failed", "成本熔断")
        return

    est_input = _est_input_tokens(messages)
    usage: dict = {}
    produced_parts: list[str] = []
    try:
        async for delta in _stream_llm_deltas(messages, usage):
            produced_parts.append(delta)
            yield ("delta", delta)
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        hint = _llm_error_hint(exc)
        if produced_parts:
            yield ("delta", f"\n\n（合成中断：{hint}）")
        else:
            yield ("failed", hint)
        return
    _record_usage(tracker, usage, est_input, _estimate_tokens("".join(produced_parts)))


async def retrieve(
    question: str,
    top_k: int = 5,
    retriever: HybridRetriever | None = None,
    *,
    with_depth: bool = False,
):
    """带缓存 + singleflight 的检索（双视图，P1-②）。

    返回：
    - ``with_depth=False``（默认）：广度视图 ``list[dict]``——兼容旧契约，
      与现网 ``search()`` 输出逐位一致；
    - ``with_depth=True``：``(广度, 深度)`` 元组——ask 的合成路径用深度视图
      作上下文，来源列表仍用广度。

    缓存 value 统一为 ``[breadth, depth]``（``cache._CACHE_VERSION`` v4）；检索
    走 ``search_dual``（同一候选池两次名额分配，零额外 embedding 成本）。fake/旧
    检索器没有 ``search_dual`` 时退化为两份相同的 ``search()`` 结果——合成上下文
    退化为广度，行为安全。
    """
    rtv = retriever if retriever is not None else get_retriever()
    cache = get_search_cache()
    # 语料版本：mock/未实现该属性的检索器按空版本处理，不影响缓存可用性
    version = getattr(rtv, "corpus_version", "")
    cached = await cache.get_pair(question, top_k, version)
    if cached is not None:
        return cached if with_depth else cached[0]

    async def _compute() -> tuple[list[dict], list[dict]]:
        # 二次检查：等锁期间可能已被其它请求写入
        again = await cache.get_pair(question, top_k, version)
        if again is not None:
            return again
        dual = getattr(rtv, "search_dual", None)
        if dual is not None:
            breadth, depth = await asyncio.to_thread(dual, question, top_k)
        else:
            breadth = await asyncio.to_thread(rtv.search, question, top_k)
            # 退化形态也要拷贝：depth 与 breadth 共用同一对象会让调用方的
            # 原地修改跨视图串染（缓存写入的是两份引用）
            depth = [dict(r) for r in breadth]
        await cache.set_pair(question, breadth, depth, top_k, version)
        return breadth, depth

    breadth, depth = await _RETRIEVE_FLIGHT.run(
        cache.key_for(question, top_k, version), _compute
    )
    return (breadth, depth) if with_depth else breadth


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

    breadth, depth = await retrieve(
        question, top_k=top_k, retriever=retriever, with_depth=True
    )

    hit = build_hit_gate(breadth)
    if hit is not None:
        # P1-② 合成分流：触发条件不满足 / 合成失败 / 拒答 → 一律降级直出 top1 原文
        if should_synthesize(hit["sources"]):
            synth = await llm_synthesize(question, depth)
            if synth is not None:
                cleaned, cited = synth
                _observe_synth("ok")
                return AskResponse(
                    answer=cleaned,
                    mode="synth",
                    sources=_merge_synth_sources(hit["sources"], depth, cited),
                    question=question,
                    session_id=session_id,
                )
        return AskResponse(
            answer=hit["answer"],
            mode="hit",
            sources=hit["sources"],
            question=question,
            session_id=session_id,
        )

    # 未命中：先取「临界片段」。连临界标准都达不到 → 直接拒答，不调 LLM
    #（没有知识库依据时调用 LLM 只会得到凭参数知识编造的答案，见 docs/RAG优化方案.md 五）。
    fallback_docs = build_fallback_context(breadth)
    if fallback_docs is None:
        return AskResponse(
            answer=NO_KB_CONTENT_MSG,
            mode="fallback",
            sources=[],
            question=question,
            session_id=session_id,
        )

    # 未命中但存在临界片段：注入 prompt 让模型有据可依，并把片段作为来源返回供核验
    answer = await llm_fallback(
        question,
        history=history,
        competitors_context=competitors_context,
        retrieved_context=format_fallback_context(fallback_docs),
    )
    return AskResponse(
        answer=answer,
        mode="fallback",
        sources=fallback_docs,
        question=question,
        session_id=session_id,
    )


def _title_from_doc_id(doc_id: str) -> str:
    """从 doc_id 反查标题（doc_id 即 seeds CSV 的 id，如 A-001）。

    函数内 import 是刻意为之：``_seed_by_id`` 首次调用会加载整份 seeds CSV
    （千行级），放到模块顶部会让所有 import 本模块的路径（含测试收集）都
    承担这笔一次性开销；函数内 import 把成本推迟到首次真实问答。
    """
    from app.rag.chunking import _seed_by_id

    seed = _seed_by_id().get(doc_id)
    return seed.title if seed else doc_id
