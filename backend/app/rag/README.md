# backend/app/rag —— RAG 基础设施

知识库问答的检索与生成封装。计划结构（阶段 F2/F3 实现）：

| 文件 | 职责 |
|------|------|
| `embedding.py` | bge-m3 embedding 封装（LangChain OllamaEmbeddings） |
| `chunking.py` | 语料分块（复用 RAGreflow 已验证逻辑：chunk 900 / FAQ 按条 / 注入《标题》） |
| `retriever.py` | 混合检索（LangChain Chroma VectorStore + BM25 + RRF 融合） |
| `store.py` | Chroma 向量库建库与加载 |

> 详见 `docs/融合规划.md` 与 `docs/RAG技术说明.md`（待建）。

## 相关实验

- **父子文档检索实验**（`scripts/experiments/parent_child_retrieval/`）：对比「标题分块 900 /
  递归 900 / 仅子块 400 / ParentDocumentRetriever」四组同源策略的 doc 级 recall@k。
  结论：本语料上父子检索未提升召回（损失全部来自子块粒度，父块回填对召回贡献≈0）。
  详见该目录 `README.md`。

