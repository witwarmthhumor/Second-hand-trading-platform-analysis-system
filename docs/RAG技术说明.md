# RAG 技术说明（二手交易知识库问答）

本文档说明「二手交易平台分析系统」中知识库问答（RAG）模块的技术架构、检索原理、建库流程与问答接口。语料与检索链路迁移自 `RAGreflow-second-hand` 项目（其检索实现已验证，详见其 `data/reports/retrieval_eval.md`）。

> 语料为 RAGreflow 演示知识库的**虚构数据**（费率/时限/制度编号均为示例），仅供学习与技术演示，不指向任何真实平台。

## 1. 总体架构

```
用户提问
   │
   ▼
┌─────────────────────────────────────────────┐
│  /api/rag/ask  (rag_service.ask)            │
│   1. HybridRetriever.search 混合检索         │
│   2. 命中判定（向量相似度 ≥ 0.55）           │
│      ├─ 命中 → 直出最优 chunk 原文 + 来源     │
│      └─ 未命中 → LLM 兜底生成（成本熔断记账） │
└─────────────────────────────────────────────┘
   │                        ▲
   ▼                        │
┌──────────────┐   ┌────────────────────┐
│ Chroma 向量库 │   │ 宿主机 Ollama       │
│ + BM25 索引  │   │  bge-m3 (embedding) │
│ + RRF 融合   │   │  LLM (兜底，OpenAI  │
└──────────────┘   │   兼容接口)          │
                   └────────────────────┘
```

**关键环境细节**：Ollama 的 embedding 调用强制使用 `127.0.0.1:11434` 而非 `localhost`。原因是本机同时跑着 Docker 容器 `ai-interview-ollama`（占用 `0.0.0.0:11434`，其中只有 `nomic-embed-text`），`localhost` 会优先命中容器；宿主机 Ollama（装有 `bge-m3`）监听 `127.0.0.1:11434`。代码在 `backend/app/rag/config.py` 中通过 `OLLAMA_BASE_URL` 默认值硬编码 IP 直连宿主机。

## 2. 语料与分块

| 资产 | 路径 | 数量 | 说明 |
|------|------|------|------|
| 制度源稿 | `rag_data/canonical/` | 920 篇 MD | 7 大类（平台规则/买家指南/卖家指南/验货质检/售后仲裁/案例库/公告） |
| 结构化 FAQ | `rag_data/faq_source/` | 80 份 JSON | 每份 15–25 条 QA |
| 事实底座 | `rag_data/facts/` | 3 个 yaml | glossary（同义词，作 BM25 分词词典）/ platform / grading |
| 种子元数据 | `rag_data/topic_seeds_1000.csv` | 1000 行 | 提供 title/category/series 等分块元数据 |

**分块策略**（`app/rag/chunking.py`）：

- MD 按标题递归分块，`CHUNK_SIZE = 900` 字（对齐评测最优值，hybrid MRR@10 0.9559），重叠率 10%；每块注入《标题》与【小节】标题。
- FAQ 按「问 + 答」逐条成块。
- 全库共 **7619 块**，每块生成唯一 `doc_id`（源自文件名，如 `A-006`）。

## 3. 混合检索（向量 + BM25 + RRF）

`app/rag/retriever.py` 实现，迁移自 B 项目已验证的手写链路（未引入 LangChain——B 项目虽装了 langchain 但检索未使用它，属冗余依赖）。

1. **稠密向量检索**：`bge-m3`（1024 维）对 query 编码，Chroma 余弦相似度召回 Top-20。
2. **稀疏 BM25 检索**：jieba 分词（加载 glossary 同义词扩充词典），`rank_bm25` 召回 Top-20。
3. **RRF 融合**：`score(d) = Σ 1/(k + rank)`，`k = 60`，两路排名融合后取 Top-K。

**BM25 持久化**：分词后的文档 tokens pickle 到 `rag_data/vector_store/bm25_index.pkl`，服务重启后 `load()` 完整恢复，避免退化为纯向量检索。

## 4. 命中判定与兜底

命中判定使用**向量相似度**（`vector_sim`，余弦 0~1），阈值 `SIMILARITY_THRESHOLD = 0.55`：

- 相关查询实测 `vector_sim ≈ 0.70~0.74`
- 无关查询实测 `vector_sim ≈ 0.48`

> **注意**：不能使用 RRF 融合得分做阈值——其量级仅 `0.016~0.033`（`1/61` 起步），与 0~1 的相似度不在同一量级。

- **命中**（最高 `vector_sim ≥ 0.55`）：直出最优 chunk 原文，附来源列表（按 `doc_id` 去重，同一文档多 chunk 不重复展示）。
- **未命中**：调用 LLM（OpenAI 兼容接口，`LLM_API_BASE` / `LLM_MODEL`）兜底生成回答，并接入成本熔断记账（`ai/cost.py`，会话累计消费超阈值即中断征询）。

## 5. 建库流程

```bash
# 一键建库（对 7619 块做 bge-m3 embedding，约 5 分钟）
bash scripts/build_vector_store.sh
```

内部执行 `scripts/build_vector_store.py`：分块 → embedding（结果缓存到 `rag_data/emb_cache/`，语料不变时免重付）→ 建 Chroma 库 + BM25 索引，产物写入 `rag_data/vector_store/`（已被 `.gitignore` 忽略，按需重建）。

## 6. 问答接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/rag/ask` | POST | 请求 `{"question": "...", "top_k": 5}`，返回 `answer` / `mode`（`hit` 或 `fallback`）/ `sources` |
| `/api/rag/status` | GET | 返回知识库是否就绪 `{"ready": true}` |

## 7. 检索质量（评测）

复用 B 项目的 `golden_queries.yaml`（285 问，含 `expected_ids` 期望命中文档）对 A 项目向量库做检索评测，指标与 B 项目同口径：**Hit@5 / MRR@10 / nDCG@10**。评测脚本见 `scripts/eval_retrieval.py`，结果见 `docs/检索评测报告.md`。

> **同源偏差声明**：golden 问题与语料出自同一生成流程，评测结果天然偏乐观。
