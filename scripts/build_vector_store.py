"""建库脚本：把 rag_data/ 语料分块 + bge-m3 向量化 + 建 Chroma 库 + BM25 索引。

产出：
- rag_data/vector_store/           Chroma 向量库（.gitignore 忽略）
- rag_data/vector_store/bm25_index.pkl   BM25 分词索引（供重启恢复）
- rag_data/emb_cache/*.npy         语料 embedding 缓存（语料不变时免重付）

用法（在 backend/ 目录下，用 backend 的 venv）：
    ../scripts/build_vector_store.py 直接运行
    （或 cd backend && .venv/Scripts/python -m scripts.build_vector_store）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 本脚本位于 Agent/scripts/，app 包在 Agent/backend/app，把 backend/ 加入 sys.path
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.rag.chunking import all_chunks
from app.rag.config import CHUNK_SIZE, EMBED_MODEL, OLLAMA_BASE_URL
from app.rag.embedding import corpus_embeddings
from app.rag.retriever import HybridRetriever


def main() -> int:
    print(f"[build_vector_store] embedding 模型: {EMBED_MODEL} @ {OLLAMA_BASE_URL}")
    print(f"[build_vector_store] chunk_size: {CHUNK_SIZE}")

    # 1. 分块
    t0 = time.time()
    chunks = all_chunks()
    print(f"[build_vector_store] 分块完成: {len(chunks)} 块，耗时 {time.time() - t0:.1f}s")
    if not chunks:
        print("[build_vector_store] 错误：未分到任何语料，请检查 rag_data/canonical 与 faq_source")
        return 1

    # 2. 向量化（含缓存，语料不变则命中缓存）
    texts = [c.text for c in chunks]
    print(f"[build_vector_store] 开始 embedding（{len(texts)} 块，首次约数十分钟）...")
    t1 = time.time()
    embeddings = corpus_embeddings(CHUNK_SIZE, texts)
    print(f"[build_vector_store] embedding 完成，耗时 {time.time() - t1:.1f}s")

    # 3. 建库
    t2 = time.time()
    retriever = HybridRetriever()
    n = retriever.build(chunks, embeddings)
    print(f"[build_vector_store] 建库完成: {n} 块，耗时 {time.time() - t2:.1f}s")

    # 4. 冒烟验证
    smoke = retriever.search("忘记密码怎么办", top_k=3)
    print("[build_vector_store] 冒烟查询「忘记密码怎么办」Top-3:")
    for r in smoke:
        print(f"  - [{r['doc_id']}] score={r['rrf_score']} {r['text'][:60]}...")
    print("[build_vector_store] ✅ 建库成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
