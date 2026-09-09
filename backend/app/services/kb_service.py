"""知识库管理服务：文档列表 / 删除 / 上传 / 重建向量库。

知识库语料由两类文件构成：
- canonical/**/*.md    主知识库（文件名约定 `<id>_<标题>.md`，id 对应 seeds CSV）
- faq_source/*.json    FAQ 语料

删除/上传只改动语料文件；改动后需调用 rebuild 重新分块 + embedding + 建库，
新语料才会进入检索。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.rag.config import CANONICAL_DIR, FAQ_SOURCE_DIR, PROJECT_ROOT

# 文件名约定：<id>_<标题>.md，id 形如 A-001（字母-数字）
_ID_RE = re.compile(r"^[A-Z][a-z]?-\d+$")

# 上传/删除允许的根目录（防路径穿越）
_ALLOWED_DIRS = (CANONICAL_DIR, FAQ_SOURCE_DIR)


@dataclass
class KbDocument:
    """一条知识库文档的元信息。"""

    path: str  # 相对项目根的路径（如 rag_data/canonical/A_rule/A-001_xxx.md）
    filename: str
    kind: str  # "md" | "faq"
    category: str  # 分类目录名（canonical 子目录或 "faq"）
    size: int
    modified_at: str


def _rel(p: Path) -> str:
    """转成相对项目根的 POSIX 路径。"""
    return p.relative_to(PROJECT_ROOT).as_posix()


def _category_of(path: Path) -> str:
    """返回分类名：canonical 子目录名，或 faq_source 固定为 'faq'。"""
    try:
        rel = path.relative_to(CANONICAL_DIR)
        return rel.parts[0] if len(rel.parts) > 1 else "（根）"
    except ValueError:
        return "faq"


def list_documents() -> list[KbDocument]:
    """列出全部知识库文档，按路径排序。"""
    docs: list[KbDocument] = []
    for p in sorted(CANONICAL_DIR.rglob("*.md")):
        docs.append(
            KbDocument(
                path=_rel(p),
                filename=p.name,
                kind="md",
                category=_category_of(p),
                size=p.stat().st_size,
                modified_at=datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            )
        )
    for p in sorted(FAQ_SOURCE_DIR.glob("*.json")):
        docs.append(
            KbDocument(
                path=_rel(p),
                filename=p.name,
                kind="faq",
                category="faq",
                size=p.stat().st_size,
                modified_at=datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            )
        )
    return docs


def _safe_resolve(relative_path: str) -> Path:
    """把用户给的相对路径解析到允许目录内，防路径穿越。"""
    candidate = (PROJECT_ROOT / relative_path).resolve()
    for allowed in _ALLOWED_DIRS:
        try:
            candidate.relative_to(allowed.resolve())
            return candidate
        except ValueError:
            continue
    raise ValueError("非法路径")


def delete_document(relative_path: str) -> bool:
    """删除指定语料文件；返回是否真的删除了。"""
    path = _safe_resolve(relative_path)
    if not path.exists():
        return False
    path.unlink()
    return True


def upload_document(
    filename: str, content: bytes, category: str | None = None
) -> KbDocument:
    """上传一个 markdown 文档到 canonical（分类目录可选）。

    文件名必须符合 `<id>_<标题>.md` 约定（id 形如 A-001）。内容为 UTF-8 文本。
    """
    if not filename.endswith(".md"):
        raise ValueError("仅支持上传 .md 文档")

    stem = Path(filename).stem  # 去掉 .md
    # 校验 id 前缀：<id>_<标题>
    if "_" not in stem:
        raise ValueError("文件名需符合 `<id>_<标题>.md`，如 A-100_新规则.md")
    doc_id = stem.split("_", 1)[0]
    if not _ID_RE.match(doc_id):
        raise ValueError("文档 id 需形如 A-001（字母-数字），且需在 seeds 中登记")

    # 分类目录：默认放到 canonical 根下的第一个子目录，或用户指定
    target_dir = CANONICAL_DIR
    if category:
        sub = (CANONICAL_DIR / category).resolve()
        try:
            sub.relative_to(CANONICAL_DIR.resolve())
        except ValueError:
            raise ValueError("非法分类目录") from None
        sub.mkdir(parents=True, exist_ok=True)
        target_dir = sub

    target = target_dir / filename
    # 防覆盖已有文件
    if target.exists():
        raise ValueError("同名文档已存在")

    target.write_text(content.decode("utf-8"), encoding="utf-8")
    return KbDocument(
        path=_rel(target),
        filename=target.name,
        kind="md",
        category=_category_of(target),
        size=target.stat().st_size,
        modified_at=datetime.fromtimestamp(target.stat().st_mtime).isoformat(),
    )


def rebuild_vector_store() -> dict:
    """重建向量库：分块 → embedding → 建库，与 build_vector_store.py 一致。

    返回 {chunk_count, seconds}。首次全量 embedding 可能数十分钟（有缓存时快）。
    """
    import time

    from app.rag.chunking import all_chunks
    from app.rag.config import CHUNK_SIZE
    from app.rag.embedding import corpus_embeddings
    from app.rag.retriever import HybridRetriever

    t0 = time.time()
    chunks = all_chunks()
    if not chunks:
        raise RuntimeError("未分到任何语料，请检查 canonical 与 faq_source")

    texts = [c.text for c in chunks]
    embeddings = corpus_embeddings(CHUNK_SIZE, texts)
    retriever = HybridRetriever()
    n = retriever.build(chunks, embeddings)
    # 清理空分类目录（删除文档后可能残留）
    _cleanup_empty_dirs()
    return {"chunk_count": n, "seconds": round(time.time() - t0, 1)}


def _cleanup_empty_dirs() -> None:
    """删除 canonical 下空的分类目录（删除文档后残留）。"""
    for d in CANONICAL_DIR.iterdir():
        if d.is_dir() and not any(d.rglob("*")):
            shutil.rmtree(d, ignore_errors=True)
