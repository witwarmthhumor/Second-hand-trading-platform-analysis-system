"""语料分块：MD 按标题结构递归分块，FAQ 以「条」为块。

迁移自 RAGreflow-second-hand 的 ``retrieval_eval/chunking.py``，去掉对其
``kb_builder.common`` 包的依赖，改为自包含实现，路径走 ``rag.config``。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from functools import lru_cache

from app.rag.config import CANONICAL_DIR, CHUNK_SIZE, FAQ_SOURCE_DIR, SEEDS_CSV


@dataclass
class Chunk:
    """一个可分块检索的最小单元。"""

    doc_id: str
    title: str
    category: str
    series: str
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class SeedRow:
    """种子元数据（对应 topic_seeds CSV 一行）。"""

    id: str
    l1_category: str
    series: str
    title: str
    template_type: str


@lru_cache(maxsize=1)
def load_seed_rows() -> tuple[SeedRow, ...]:
    """读取 topic_seeds CSV 全量种子（含 BOM，用 utf-8-sig 解码）。"""
    rows: list[SeedRow] = []
    with open(SEEDS_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                SeedRow(
                    id=r["id"],
                    l1_category=r["l1_category"],
                    series=r["series"],
                    title=r["title"],
                    template_type=r.get("template_type", ""),
                )
            )
    return tuple(rows)


@lru_cache(maxsize=1)
def _seed_by_id() -> dict[str, SeedRow]:
    return {r.id: r for r in load_seed_rows()}


def _split_headings(body: str) -> list[tuple[str, str]]:
    """按二级标题切段：[(小节标题, 小节文本)]；无小节的归入「（导语）」。"""
    sections: list[tuple[str, list[str]]] = [("（导语）", [])]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.startswith("# "):
            continue  # h1 标题单独存 title
        else:
            sections[-1][1].append(line)
    return [(t, "\n".join(ls).strip()) for t, ls in sections if "\n".join(ls).strip()]


def _split_table_blocks(section: str) -> list[str]:
    """大表按行组切块：表格行连续块超过 chunk_size 时按行拆分，复制表头。"""
    lines = section.splitlines()
    if not any(ln.strip().startswith("|") for ln in lines):
        return [section] if section.strip() else []
    out: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if ln.strip().startswith("|"):
            buf.append(ln)
        else:
            if buf:
                out.append("\n".join(buf))
                buf = []
            out.append(ln)
    if buf:
        out.append("\n".join(buf))
    return out


def _strip_fm(md_text: str) -> tuple[dict, str]:
    """返回 (frontmatter dict, 正文)。"""
    m = md_text.lstrip()
    if not m.startswith("---"):
        return {}, m
    end = m.find("\n---", 3)
    if end == -1:
        return {}, m
    meta: dict = {}
    for line in m[3:end].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, m[end + 4 :]


def chunk_markdown(seed: SeedRow, md_text: str) -> list[Chunk]:
    """单篇 MD → 分块列表（frontmatter 元数据进 meta，正文注入《标题》）。"""
    meta_fm, body = _strip_fm(md_text)
    chunks: list[Chunk] = []

    def _emit(text: str, section: str) -> None:
        text = text.strip()
        if not text:
            return
        chunks.append(
            Chunk(
                seed.id,
                seed.title,
                seed.l1_category,
                seed.series,
                text,
                {"section": section, **meta_fm},
            )
        )

    for sec_title, sec_text in _split_headings(body):
        full = (
            f"《{seed.title}》【{sec_title}】\n{sec_text}"
            if sec_title != "（导语）"
            else f"《{seed.title}》\n{sec_text}"
        )
        if len(full) <= CHUNK_SIZE:
            _emit(full, sec_title)
            continue
        parts = _split_table_blocks(sec_text)
        buf = ""
        for part in parts:
            candidate = (buf + "\n" + part).strip() if buf else part
            if len(candidate) > CHUNK_SIZE and buf:
                _emit(f"《{seed.title}》【{sec_title}】\n{buf}", sec_title)
                buf = part
            else:
                buf = candidate
        if buf.strip():
            _emit(f"《{seed.title}》【{sec_title}】\n{buf}", sec_title)
    return chunks


def chunk_faq(seed: SeedRow, payload: dict) -> list[Chunk]:
    """FAQ 以「条」为块：每条 QA 一个块，q_main+变体+答案拼为正文。"""
    chunks: list[Chunk] = []
    for i, item in enumerate(payload.get("items", []), 1):
        variants = "；".join(item.get("q_variants", []))
        text = f"【{seed.title}】问：{item['q_main']}（{variants}）\n答：{item['a']}"
        chunks.append(
            Chunk(
                seed.id,
                seed.title,
                seed.l1_category,
                seed.series,
                text,
                {"faq_index": i, "role": item.get("role", "buyer")},
            )
        )
    return chunks


def all_chunks() -> list[Chunk]:
    """全库分块：canonical MD + faq_source JSON（与 B 项目评测入库范围一致）。"""
    seeds = _seed_by_id()
    chunks: list[Chunk] = []
    for md_path in sorted(CANONICAL_DIR.rglob("*.md")):
        sid = md_path.stem.split("_")[0]
        seed = seeds.get(sid)
        if seed is None:
            continue
        chunks.extend(chunk_markdown(seed, md_path.read_text(encoding="utf-8")))
    for faq_path in sorted(FAQ_SOURCE_DIR.glob("*.json")):
        sid = faq_path.stem
        seed = seeds.get(sid)
        if seed is None:
            continue
        payload = json.loads(faq_path.read_text(encoding="utf-8"))
        chunks.extend(chunk_faq(seed, payload))
    return chunks
