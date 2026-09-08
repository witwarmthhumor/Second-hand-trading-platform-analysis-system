# rag_data —— RAG 语料资产（拷自 RAGreflow-second-hand）

存放二手交易知识库问答所需的语料资产。来源项目：`E:\AIDevelop\AIProject\RAGreflow-second-hand`。

| 子目录/文件 | 内容 | 是否入库 git |
|--------|------|-------------|
| `canonical/` | 920 篇二手交易制度 MD 源稿（A~G 七类） | ✅ |
| `faq_source/` | 80 份 FAQ 结构化 JSON | ✅ |
| `facts/` | 事实底座（platform/grading/glossary 三个 yaml） | ✅ |
| `topic_seeds_1000.csv` | 种子元数据（id/类目/系列/标题） | ✅ |
| `vector_store/` | Chroma 向量库 + BM25 索引（`scripts/build_vector_store.py` 生成） | ❌ 不入库 |
| `emb_cache/` | 语料 embedding 缓存（`*.npy`，语料不变时免重付） | ❌ 不入库 |

> 语料为 RAGreflow 演示知识库的**虚构数据**（费率/时限/制度编号均为示例），仅供学习与技术演示，不指向任何真实平台。

## 建库

```bash
cd backend
.venv/Scripts/python -m scripts.build_vector_store
```

产物：`rag_data/vector_store/`（Chroma 库 + `bm25_index.pkl`）、`rag_data/emb_cache/*.npy`。
问答服务启动后自动加载；未建库时 `/api/rag/status` 返回 `ready: false`。

