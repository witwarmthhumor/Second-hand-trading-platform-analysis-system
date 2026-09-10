"""生成侧两种答案策略：生产现状（直出原文） vs 优化（LLM 合成）。

- ``answer_direct``：完全复刻生产 `rag_service.ask()` 命中路径——把 top1 chunk
  原文当答案返回（无 LLM 参与）。
- ``answer_synth``：生成侧优化——把 top-k 片段喂给 LLM 合成答案，要求只依据片段、
  多文档分点、逐条标注来源、无依据则不编造。

两者共用同一份检索结果，唯一变量是「答案怎么生成」，保证对比公平。
"""

from __future__ import annotations

import re
import time

from llm_common import chat

# 来源标注形如 【A-001】/【来源:D-161】/【来源：FAQ-049】
CITE_RE = re.compile(r"【(?:来源[:：])?\s*([A-Za-z]+-\d+)\s*】")

SYNTH_SYSTEM_PROMPT = """你是二手交易平台（闲鱼、转转等）的问答助手。请严格依据「知识片段」回答用户问题。

要求：
1. 只使用知识片段中的信息作答，不得引入片段之外的任何知识，不得编造数字、比例或规则。
2. 若知识片段不足以回答问题，直接回复「知识库中未找到相关信息」，不要勉强作答。
3. 若片段来自多个文档，请分点归纳，覆盖每一条与问题相关的信息，不要遗漏。
4. 每个要点末尾用【来源:doc_id】标注依据（doc_id 见片段标签），不要编造来源。
5. 语言简洁，直接给答案，不要复述问题、不要输出多余前缀。"""

# v2：来源标注从正文里挪到末尾统一一行。动机：正文夹带【来源:x】标记会打断句子，
# 既影响可读性，也会让 answer_relevancy（由答案反推问题）的语义向量被标记稀释。
SYNTH_SYSTEM_PROMPT_V2 = """你是二手交易平台（闲鱼、转转等）的问答助手。请严格依据「知识片段」回答用户问题。

要求：
1. 只使用知识片段中的信息作答，不得引入片段之外的任何知识，不得编造数字、比例或规则。
2. 若知识片段不足以回答问题，直接回复「知识库中未找到相关信息」。
3. 若片段来自多个文档，请分点归纳，覆盖每一条与问题相关的信息，不要遗漏。
4. 正文保持干净流畅，不要在句子中间插入来源标记。
5. 全部作答结束后，另起一行输出「依据来源：doc_id1、doc_id2」（doc_id 取片段标签）。
6. 语言简洁，不要复述问题、不要输出多余前缀。"""

PROMPT_STYLES = {"v1": SYNTH_SYSTEM_PROMPT, "v2": SYNTH_SYSTEM_PROMPT_V2}


def answer_direct(results: list[dict]) -> str:
    """生产现状：命中直出 top1 chunk 原文。"""
    return results[0]["text"] if results else ""


def build_synth_messages(
    question: str, results: list[dict], style: str = "v2"
) -> list[dict]:
    """拼装合成答案的 messages：片段带【doc_id】标签，便于模型标注来源。"""
    blocks = [f"【{r['doc_id']}】\n{r['text']}" for r in results]
    context = "\n\n".join(blocks)
    return [
        {"role": "system", "content": PROMPT_STYLES.get(style, SYNTH_SYSTEM_PROMPT_V2)},
        {
            "role": "user",
            "content": f"知识片段：\n{context}\n\n用户问题：{question}",
        },
    ]


async def answer_synth(
    question: str, results: list[dict], style: str = "v2"
) -> tuple[str, float]:
    """LLM 合成答案，返回 (答案正文, 耗时秒)。"""
    if not results:
        return "", 0.0
    t0 = time.time()
    text = await chat(build_synth_messages(question, results, style))
    return text.strip(), time.time() - t0


def parse_citations(text: str) -> list[str]:
    """从答案中解析来源 doc_id，兼容两种标注风格。

    - v1（正文内联）：【来源:A-001】/【A-001】
    - v2（末尾一行）：依据来源：A-001、B-002
    """
    seen: list[str] = []
    for m in CITE_RE.finditer(text):
        did = m.group(1).upper()
        if did not in seen:
            seen.append(did)
    if seen:
        return seen
    # v2 回退：只在含「来源」的那一行/那几行里抽取 doc_id，避免正文里的数字被误捕
    for line in text.splitlines():
        if "来源" not in line:
            continue
        for token in re.findall(r"[A-Za-z]+-\d+", line):
            did = token.upper()
            if did not in seen:
                seen.append(did)
    return seen
