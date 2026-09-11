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
4. **重排与文档级去重**：RRF 之后先按向量相似度**精排**（`RERANK_BY_VSIM`，压掉「BM25 独有命中、语义无关」的噪声），再做**文档级去重 + 保底补位**（`DOC_DEDUP_ENABLED`，2026-09-11 开启）：每篇文档保底保留 1 个最相关 chunk，名额不足 Top-K 时回填同文档次优 chunk。用于解决「同文档多 chunk 抱团占位、多文档问题召回不全」（详见 `docs/RAG优化方案.md`）。

**BM25 持久化**：分词后的文档 tokens 序列化为 JSON 落到 `rag_data/vector_store/bm25_index.json`（不用 pickle，避免不可信反序列化风险），服务重启后 `load()` 完整恢复，避免退化为纯向量检索。

## 4. 命中判定与兜底

命中判定使用**向量相似度**（`vector_sim`，余弦 0~1），阈值 `SIMILARITY_THRESHOLD = 0.65`：

- 相关查询实测 `vector_sim ≈ 0.70~0.74`；285 问实测正确样本 top1 最低 **0.6660**
- 无关查询实测 `vector_sim ≈ 0.48`；领域外闲聊 top1 约 **0.6235**

> **注意**：不能使用 RRF 融合得分做阈值——其量级仅 `0.016~0.033`（`1/61` 起步），与 0~1 的相似度不在同一量级。

未命中后按相似度**三档分流**（P1，见 `docs/RAG优化方案.md` 第五节）：

| 档位 | 条件（`results[0].vector_sim`） | 行为 |
|---|---|---|
| **命中** | `≥ 0.65` | 直出最优 chunk 原文，附来源列表（按 `doc_id` 去重，同一文档多 chunk 不重复展示） |
| **注入兜底** | `[0.4, 0.65)` | 把灰区片段（带「来源 + 相似度」标注，上限 3 条）注入 system prompt，再调 LLM；片段同时作为来源返回供核验 |
| **直接拒答** | `< 0.4` 或结果为空 | 回「知识库暂无相关内容」，**不调用 LLM**（无依据时不猜测，杜绝编造） |

兜底 LLM 走 OpenAI 兼容接口（`LLM_API_BASE` / `LLM_MODEL`），并接入成本熔断记账
（`ai/cost.py`，会话累计消费超阈值即中断征询）。

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
| `/api/rag/ask/stream` | POST | 流式（SSE）：命中直出，未命中逐 token 推 `delta`，结束推 `done` |
| `/api/rag/status` | GET | 返回知识库是否就绪 `{"ready": true}` |

## 7. 检索缓存（L1 进程内 + Redis L2）

检索是问答链路里最重的一段（实测完整检索 ≈150ms/次，其中 bge-m3 embedding 占 62%），
相同问法重复检索纯属浪费，故做两级缓存 + 并发合并。

| 层 | 位置 | 内容 | 说明 |
|----|------|------|------|
| L1 | 进程内 `OrderedDict` LRU（256 条，TTL ≤60s） | 检索结果 | 命中 <0.05ms；同时也是 Redis 故障时的兜底 |
| L2 | Redis（`RAG_CACHE_TTL`，默认 3600s，写时 ±10% 抖动） | 检索结果 JSON | 多进程/多 worker 共享；命中 ≈0.34ms |

**缓存 key**：`rag:search:v2:{语料版本}:k{top_k}:{归一化问题}`

- 归一化：全角→半角、去空白、小写（「忘记密码」与「忘记密码！」同一个 key）
- `top_k` 与**语料版本**（`bm25_index.json` + `chroma.sqlite3` 的 mtime 指纹）都必须进 key，
  否则不同 `top_k` 会串用结果、语料被替换后仍命中旧结果
- 知识库重建：刷新检索器单例（换掉旧 collection 句柄）→ 用**一次性连接**按前缀清空缓存。
  不要复用进程内单例 client：后台线程里的 `asyncio.run` 会建临时事件循环，
  单例连接绑上去后随即随循环关闭，之后主循环全部 `Event loop is closed`，缓存永久失效

**防击穿**：`SingleFlight` 合并同 key 并发 miss，只放 1 个请求回源，其余 await 同一结果。

**降级与熔断**：Redis 不可用时静默跳过缓存，不影响问答主流程；一次失败即进入 30s 熔断窗口，
窗口内不再尝试建连（否则每次操作都要白等 2s 连接超时，比不加缓存更慢）。

**总开关**：`RAG_CACHE_ENABLED`（默认 true），测试与故障排查时置 false 完全关闭。

> 注意两个端点都走 `rag_service.retrieve()`。流式端点若自行调用 `retriever.search`，
> 会绕过缓存 —— 而前端默认走流式，等于缓存收益归零。

## 8. 检索质量（评测）

复用 B 项目的 `golden_queries.yaml`（285 问，含 `expected_ids` 期望命中文档）对 A 项目向量库做检索评测，指标与 B 项目同口径：**Hit@5 / MRR@10 / nDCG@10**。评测脚本见 `scripts/eval_retrieval.py`，结果见 `docs/检索评测报告.md`。

> **同源偏差声明**：golden 问题与语料出自同一生成流程，评测结果天然偏乐观。

## 9. 检索策略对比实验（父子文档检索）

为验证「父块+子块」两层检索能否提升召回，另建了独立实验
`scripts/experiments/parent_child_retrieval/`，对「标题分块 900 / 递归 900 / 仅子块 400 /
ParentDocumentRetriever」四组同源策略做 doc 级 recall@k 对比。

**结论**：本知识库（短文档、结构清晰）上父子检索**未提升 doc 级召回**，反略低于基线；
消融表明损失**全部来自子块粒度**（400 vs 900），**父块回填对召回贡献 ≈ 0**——父块回填的
价值在生成阶段的上下文完整性，而非检索召回。详见该目录 `README.md`。

