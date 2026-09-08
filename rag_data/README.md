# rag_data —— RAG 语料资产（拷自 RAGreflow-second-hand）

存放二手交易知识库问答所需的语料资产。来源项目：`E:\AIDevelop\AIProject\RAGreflow-second-hand`。

| 子目录 | 内容 | 是否入库 git |
|--------|------|-------------|
| `canonical/` | 920 篇二手交易制度 MD 源稿 | ✅ |
| `faq_source/` | 80 份 FAQ 结构化 JSON | ✅ |
| `facts/` | 事实底座（platform/grading/glossary 三个 yaml） | ✅ |
| `vector_store/` | Chroma 向量库（`scripts/build_vector_store.py` 生成） | ❌ 不入库 |
| `prompts/`（待拷） | 生成用 prompt 模板（问答无需，可归档） | 可选 |

> 语料为 RAGreflow 演示知识库的**虚构数据**（费率/时限/制度编号均为示例），仅供学习与技术演示，不指向任何真实平台。
