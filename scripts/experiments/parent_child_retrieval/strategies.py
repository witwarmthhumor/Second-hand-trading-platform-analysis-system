"""父子文档检索实验 —— 三种策略的构建与检索。

三方共用同一语料（``common.load_corpus_documents``）、同一 golden 测试集、
同一稠密向量检索（Chroma 余弦，bge-m3）与同一 doc 级去重口径：

1. ``heading``      —— 标题分块基线：复用生产 ``chunking.all_chunks``（chunk 900）。
2. ``recursive``    —— 递归分块基线：LangChain ``RecursiveCharacterTextSplitter``（900/10%）。
3. ``parent_child`` —— 父子文档检索：LangChain 官方 ``ParentDocumentRetriever``
   （父块 1600 / 子块 400，Chroma 索引子块，InMemoryStore 存父块）。

检索统一为「先取一个大候选池 → 按 doc_id 去重 → 截断到 top-20」，使三方在
doc 级排名上可直接比较。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from uuid import uuid4

from common import (
    EXPERIMENT_CHROMA_DIR,
    BgeM3Embeddings,
    chunks_to_meta,
    corpus_embeddings,
    load_corpus_documents,
)
from langchain_chroma import Chroma
from langchain_core.vectorstores import VectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 中文友好的递归分隔符（默认分隔符不含中文标点，会退化为按字符切）
CN_SEPARATORS = [
    "\n## ",
    "\n### ",
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "，",
    " ",
    "",
]

# 检索候选池大小（取足够大，保证去重后能凑满 top-20 doc）
CANDIDATE_POOL = 100
DOC_LIMIT = 20

# 生产标题分块的 chunk_size（用于复用其 embedding 缓存）
HEADING_CHUNK_SIZE = 900

# chromadb 单次批量上限 5461，留余量
CHROMA_BATCH = 4096


def _fresh_chroma(collection: str, embeddings: BgeM3Embeddings) -> Chroma:
    """建一个落在实验目录下的全新 Chroma 集合（不碰生产库）。"""
    EXPERIMENT_CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    vs = Chroma(
        collection_name=collection,
        embedding_function=embeddings,
        persist_directory=str(EXPERIMENT_CHROMA_DIR),
        collection_metadata={"hnsw:space": "cosine"},
    )
    # 幂等：同名集合先清空再重建
    with contextlib.suppress(Exception):
        vs.reset_collection()
    return vs


def _add_precomputed(
    vs: Chroma,
    texts: list[str],
    metadatas: list[dict],
    embeddings: list[list[float]],
) -> None:
    """用已算好的向量分批写入（绕过 langchain 的重复 embedding），打印进度。"""
    col = vs._collection  # noqa: SLF001  # chromadb 私有句柄，实验脚本直接用
    ids = [str(uuid4()) for _ in texts]
    total = len(texts)
    for i in range(0, total, CHROMA_BATCH):
        end = min(i + CHROMA_BATCH, total)
        col.upsert(
            ids=ids[i:end],
            embeddings=embeddings[i:end],
            documents=texts[i:end],
            metadatas=metadatas[i:end],
        )
        print(f"    入库 {end}/{total}")


class DenseDocRetriever:
    """稠密向量检索 → doc 级排名（同一 doc 多块去重）。"""

    def __init__(self, vectorstore: VectorStore, name: str) -> None:
        self._vs = vectorstore
        self.name = name

    def doc_pool(self, query: str, limit: int = DOC_LIMIT) -> list[str]:
        hits = self._vs.similarity_search_with_score(query, k=CANDIDATE_POOL)
        return _dedupe_doc_ids((doc.metadata.get("doc_id") for doc, _ in hits), limit)


class ParentChildRetriever:
    """ParentDocumentRetriever 包装：检索子块 → 回填父块 → doc 级排名。"""

    def __init__(self, retriever, name: str) -> None:
        self._retriever = retriever
        self.name = name

    def doc_pool(self, query: str, limit: int = DOC_LIMIT) -> list[str]:
        parents = self._retriever.invoke(query)
        return _dedupe_doc_ids((d.metadata.get("doc_id") for d in parents), limit)


def _dedupe_doc_ids(doc_ids, limit: int) -> list[str]:
    """按出现顺序去重 doc_id 并截断到 limit。"""
    seen: list[str] = []
    for did in doc_ids:
        if did and did not in seen:
            seen.append(did)
            if len(seen) >= limit:
                break
    return seen


def _splitter(chunk_size: int, overlap: int) -> RecursiveCharacterTextSplitter:
    """构建中文友好的递归切分器（按字符计长度）。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=CN_SEPARATORS,
        keep_separator=True,
        length_function=len,
    )


# ---------------------------------------------------------------------------
# 策略 1：标题分块基线（生产既有逻辑）
# ---------------------------------------------------------------------------


def build_heading() -> DenseDocRetriever:
    """标题分块基线：md 按二级标题、FAQ 按条，chunk 900。"""
    from app.rag.chunking import all_chunks

    chunks = all_chunks()
    texts = [c.text for c in chunks]
    metas = chunks_to_meta(chunks)
    print(f"    [heading] 分块 {len(texts)} 块，复用生产 embedding 缓存 ...")
    vs = _fresh_chroma("exp_heading", BgeM3Embeddings(HEADING_CHUNK_SIZE))
    embs = corpus_embeddings(HEADING_CHUNK_SIZE, texts)
    _add_precomputed(vs, texts, metas, embs)
    return DenseDocRetriever(vs, "heading")


# ---------------------------------------------------------------------------
# 策略 2：递归分块基线（朴素单级）
# ---------------------------------------------------------------------------


def build_recursive(
    chunk_size: int = 900, overlap: int = 90, name: str = "recursive"
) -> DenseDocRetriever:
    """递归分块基线：对「每源文件一个 Document」做单级递归切分。"""
    docs = load_corpus_documents()
    children = _splitter(chunk_size, overlap).split_documents(docs)
    texts = [d.page_content for d in children]
    metas = [d.metadata for d in children]
    print(f"    [{name}] 源文档 {len(docs)} → 子块 {len(texts)} 块，开始 embedding ...")
    vs = _fresh_chroma(f"exp_{name}", BgeM3Embeddings(chunk_size))
    embs = corpus_embeddings(chunk_size, texts)
    _add_precomputed(vs, texts, metas, embs)
    return DenseDocRetriever(vs, name)


def build_child400() -> DenseDocRetriever:
    """消融对照：仅子块（400）入库、无父块回填。

    与 ``parent_child`` 使用完全相同的切分器（400/40），唯一差别是不做父块回填，
    用于把「子块粒度效应」与「父块回填效应」分离。
    """
    return build_recursive(chunk_size=400, overlap=40, name="child400")


# ---------------------------------------------------------------------------
# 策略 3：父子文档检索
# ---------------------------------------------------------------------------


def build_parent_child(
    parent_size: int = 1600,
    parent_overlap: int = 160,
    child_size: int = 400,
    child_overlap: int = 40,
) -> ParentChildRetriever:
    """父子文档检索：子块入向量库，父块入 docstore，检索命中子块后回填父块。"""
    from langchain.retrievers import ParentDocumentRetriever
    from langchain.storage import InMemoryStore

    docs = load_corpus_documents()
    vs = _fresh_chroma("exp_parent_child", BgeM3Embeddings(child_size))
    store: InMemoryStore = InMemoryStore()
    retriever = ParentDocumentRetriever(
        vectorstore=vs,
        docstore=store,
        child_splitter=_splitter(child_size, child_overlap),
        parent_splitter=_splitter(parent_size, parent_overlap),
        search_kwargs={"k": CANDIDATE_POOL},
        # 关键：默认 id_key="doc_id" 会覆盖语料的 doc_id 元数据，改用独立键
        id_key="parent_id",
    )
    # 分批 add：PDR 一次性 add 全部子块会超 chromadb 批量上限
    step = 30
    for i in range(0, len(docs), step):
        retriever.add_documents(docs[i : i + step])
        print(f"    [parent_child] 已索引源文档 {min(i + step, len(docs))}/{len(docs)}")
    print(f"    [parent_child] 子块入库 {vs._collection.count()} 块")  # noqa: SLF001
    return ParentChildRetriever(retriever, "parent_child")


BUILDERS: dict[str, Callable[[], object]] = {
    "heading": build_heading,
    "recursive": build_recursive,
    "child400": build_child400,
    "parent_child": build_parent_child,
}
