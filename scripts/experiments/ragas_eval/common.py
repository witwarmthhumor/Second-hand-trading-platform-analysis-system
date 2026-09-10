"""RAGAS 评估公共工具：语料原文反查、golden 加载、检索结果构建。

评估口径说明
------------
本评估用 RAGAS 的**非 LLM 指标**（字符串相似度，不依赖大模型评判）：
- ``NonLLMContextPrecisionWithReference``：衡量检索结果里「相关块是否排在前面」，
  需要 retrieved_contexts + reference_contexts（正确答案所在文档的原文）。
- ``NonLLMContextRecall``：衡量「该命中的文档有没有被漏掉」，同样只需
  retrieved + reference。

选择非 LLM 指标的原因：本项目 LLM 网关（rightapi）当前不可用
（gpt-5.6-luna 报 503 Pricing configuration），无法跑 faithfulness /
answer_relevancy 等需 LLM 打分的指标；context_precision/recall 两个检索侧
指标正好不依赖 LLM，且与本项目「先优化召回」的目标一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

# 允许直接运行或 -m 运行两种方式导入 app 包
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.rag.config import CANONICAL_DIR, FAQ_SOURCE_DIR

GOLDEN_PATH = ROOT / "rag_data" / "eval" / "golden_queries.yaml"


def _load_doc_chunks() -> dict[str, list[str]]:
    """按 doc_id 分组的「分块粒度」文本（与检索器入库粒度一致）。

    非 LLM 指标用字符串相似度（rapidfuzz）判断 retrieved 是否命中 reference，
    两者粒度必须对齐：retrieved 是 chunk，reference 也应是 chunk。
    若 reference 用整篇全文，900 字 chunk 与 3000 字全文相似度会被严重低估，
    导致 context_precision 失真（详见 README 归因）。
    """
    from app.rag.chunking import all_chunks  # 延迟导入，避免循环

    chunks_by_doc: dict[str, list[str]] = {}
    for c in all_chunks():
        chunks_by_doc.setdefault(c.doc_id, []).append(c.text)
    return chunks_by_doc


_DOC_CHUNKS: dict[str, list[str]] | None = None


def doc_chunks() -> dict[str, list[str]]:
    """惰性单例：doc_id → 该文档的分块文本列表。"""
    global _DOC_CHUNKS
    if _DOC_CHUNKS is None:
        _DOC_CHUNKS = _load_doc_chunks()
    return _DOC_CHUNKS


# ---------------------------------------------------------------------------
# doc_id → 原文
# ---------------------------------------------------------------------------


def _load_doc_texts() -> dict[str, str]:
    """把全库文档按 doc_id 索引为纯文本（canonical MD 正文 + FAQ 拼接）。

    doc_id 即文件名 stem 的「-」前缀（A-001 / CASE-001 / FAQ-001 等）。
    返回 {doc_id: 全文}，供 reference_contexts 反查。
    """
    texts: dict[str, str] = {}
    for md_path in sorted(CANONICAL_DIR.rglob("*.md")):
        sid = md_path.stem.split("_")[0]
        raw = md_path.read_text(encoding="utf-8")
        # 去掉 frontmatter（--- 块），只留正文，避免元数据干扰相似度
        body = raw
        if raw.lstrip().startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                body = raw[end + 4 :]
        texts[sid] = body.strip()
    for faq_path in sorted(FAQ_SOURCE_DIR.glob("*.json")):
        sid = faq_path.stem
        payload = json.loads(faq_path.read_text(encoding="utf-8"))
        parts: list[str] = []
        for item in payload.get("items", []):
            variants = "；".join(item.get("q_variants", []))
            parts.append(
                f"问：{item.get('q_main', '')}（{variants}）\n答：{item.get('a', '')}"
            )
        texts[sid] = "\n\n".join(parts).strip()
    return texts


_DOC_TEXTS: dict[str, str] | None = None


def doc_texts() -> dict[str, str]:
    """惰性单例：全库 doc_id → 原文。"""
    global _DOC_TEXTS
    if _DOC_TEXTS is None:
        _DOC_TEXTS = _load_doc_texts()
    return _DOC_TEXTS


# ---------------------------------------------------------------------------
# golden 加载
# ---------------------------------------------------------------------------


def load_golden() -> list[dict]:
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["queries"]


def reference_contexts_for(expected_ids: list[str]) -> list[str]:
    """expected_ids → 参考上下文列表（每条 = 目标文档的一个分块）。

    粒度与 retrieved_contexts 对齐（都是 chunk 级），否则 rapidfuzz
    字符串相似度会因长度差异失真。一个目标文档可能有多个 chunk，
    全部展开为 reference 列表（RAGAS 的 recall 口径是「reference 里
    有多少条被命中」，展开后更贴合「漏检了多少相关内容」）。
    """
    chunks = doc_chunks()
    refs: list[str] = []
    for eid in expected_ids:
        refs.extend(chunks.get(eid, []))
    return refs
