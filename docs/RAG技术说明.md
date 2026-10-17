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
│   1.5 cross-encoder 精排（RERANKER_ENABLED， │
│       默认关，见 §3.1；关时不加载模型）       │
│   2. 命中判定（候选池最高向量相似度 ≥ 0.65）  │
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
4. **重排与文档级去重**：RRF 之后先做**精排**（打分器见 §3.1：生产排序分数已从 bge-m3 余弦换成 cross-encoder 分，`RERANKER_ENABLED` 默认关），再做**文档级去重 + 保底补位**（`DOC_DEDUP_ENABLED`，2026-09-11 开启）：每篇文档保底保留 1 个最相关 chunk，名额不足 Top-K 时回填同文档次优 chunk。用于解决「同文档多 chunk 抱团占位、多文档问题召回不全」（详见 `docs/RAG优化方案.md`）。

**BM25 持久化**：分词后的文档 tokens 序列化为 JSON 落到 `rag_data/vector_store/bm25_index.json`（不用 pickle，避免不可信反序列化风险），服务重启后 `load()` 完整恢复，避免退化为纯向量检索。

### 3.1 rerank 接入点（cross-encoder 精排）

动机：bge-m3 是 bi-encoder，query/passage 各自独立编码后算余弦，同域语料被压在极窄
区间内（P0-2 实测「期望 vs 非期望」中位差仅 **0.036**），**排序分辨率不足**。cross-encoder
把 (query, passage) 拼成单序列送模型，直接建模交互。方案与实测见 `docs/Rerank方案.md`。

| 项 | 实现 |
|---|---|
| 模块 | `app/rag/reranker.py`：ONNX Runtime + `tokenizers`（**不引 transformers/torch**），`CrossEncoderReranker.score(query, passages) -> list[float]`（sigmoid 归一到 0~1） |
| 模型 | `onnx-community/bge-reranker-v2-m3-ONNX` 的 `model_int8.onnx`（570MB），落 `backend/models/bge-reranker-v2-m3/`（已 gitignore），由 `scripts/download_reranker.py` 经 hf-mirror 下载 |
| 开关 | `RERANKER_ENABLED`（**默认 false**，关着时行为与改造前**逐位一致**）、`RERANKER_TOP_N`（默认 = `QUERY_N_RESULTS` = 20）、`RERANKER_MAX_LENGTH` / `RERANKER_BATCH_SIZE`，均在 `app/rag/config.py` |
| 取用入口 | `get_active_reranker()` 是**生产唯一入口**（关闭或不可用 → `None`，关闭态连实例都不建）；`get_reranker()` 是纯惰性单例，供离线闸门脚本用、**不受开关影响** |
| 打分窗口 | 只对 **RRF 排序前 `RERANKER_TOP_N`** 个候选打分，**只重排候选池、不扩大召回**（`QUERY_N_RESULTS` 保持 20） |
| 排序键 | `search()` / `search_dual()` 用 `rank_scores = rerank or v_scores` **一处切换**；`diversify_with_refill` / `dual_views` 签名与算法**一行未改**（它们本就接受任意 `pos → 分` 字典）。副作用：rerank 态下**锚定文档**（`ranked_all[0]` 所属文档）会跟着新排序分走 |
| 结果字段 | `_assemble_results` 新增 `rerank_score`（未启用或窗口外为 `None`）；**`vector_sim` 保持原义**（bge-m3 余弦，语义永不随打分器改变） |
| 降级 | 模型文件缺失 / 分词器加载失败 / ONNX 推理异常 → 记一条**含原因**的 warning，返回**空字典**，调用方自然退回余弦排序；**问答接口不返回 5xx**（US-009） |
| 缓存 | `cache._CACHE_VERSION` 升到 **v5**（key 不含打分器维度，不升版本 + 不清 `rag:search:*` 会 TTL 内命中旧排序/旧判定） |

**`sort_by_vsim()` 的定位（已变；原 `rerank_by_vsim`，2026-09-19 改名消歧义）**：它仍是「按 bge-m3 余弦对 RRF 结果二次排序」的纯
函数，但**已不是生产路径**——生产走 `DOC_DEDUP_ENABLED=true` 的 `diversify_with_refill`
分支；`sort_by_vsim` 现在只是 `DOC_DEDUP_ENABLED=false` 时的 **`elif` 兜底分支**，
且其排序键同样已换成 `rank_scores`。即：**换的是「排序分数」，不是「排序算法」**。

> ⚠️ **当前未上线**：285 问对拍（`--top-k 5`）效果侧 6/6 达标（MRR@10 0.9526→0.9693、
> nDCG@10 0.7957→0.8146），但**延迟增量 11088 ms/问，超 300ms 上限 37 倍** → 判定
> 「未达到验收标准」，故保持默认关闭。完整数据与结论见 `docs/Rerank方案.md` §11。

## 4. 命中判定与兜底

命中判定使用**向量相似度**（`vector_sim`，余弦 0~1），阈值 `SIMILARITY_THRESHOLD = 0.65`：

- 相关查询实测 `vector_sim ≈ 0.70~0.74`；285 问实测正确样本 top1 最低 **0.6660**
- 无关查询实测 `vector_sim ≈ 0.48`；领域外闲聊 top1 约 **0.6235**

> **注意**：不能使用 RRF 融合得分做阈值——其量级仅 `0.016~0.033`（`1/61` 起步），与 0~1 的相似度不在同一量级。
>
> **判定取「候选池全局最高余弦」，不取首位**（US-006）：自 §3.1 接入 rerank 起，排序键
> 可能已是 cross-encoder 分，`results[0]` 不再保证是余弦最高的 chunk。若仍读
> `results[0]["vector_sim"]`，会出现「rerank 判最相关、余弦却低于 0.65」→ 误判未命中
> → 走 LLM 兜底，rerank 收益被吃掉。判定与灰区两处闸门共用
> `rag_service._max_vector_sim(results)`（`max(r["vector_sim"] for r in results)`，空池 0.0）。
> 阈值本身的**取值与标定依据不变**（仍是 bge-m3 余弦的分布空隙）。

未命中后按相似度**三档分流**（P1，见 `docs/RAG优化方案.md` 第五节）：

| 档位 | 条件（候选池全局最高 `vector_sim`） | 行为 |
|---|---|---|
| **命中** | `≥ 0.65` | 视 `SYNTH_MODE` 分流（见下节 4.1）：默认 `multi_doc` 档下，多文档信号命中走 LLM 合成，否则直出最优 chunk 原文（rerank 态 = cross-encoder 第一名，**收益而非回归**）；来源列表（按 `doc_id` 去重）不变 |
| **注入兜底** | `[0.4, 0.65)` | 把灰区片段（带「来源 + 相似度」标注，上限 3 条）注入 system prompt，再调 LLM；片段同时作为来源返回供核验 |
| **直接拒答** | `< 0.4` 或结果为空 | 回「知识库暂无相关内容」，**不调用 LLM**（无依据时不猜测，杜绝编造） |

**来源列表过滤（US-006 起为双通道）**：`vector_sim >= SIMILARITY_THRESHOLD`
**或** `rerank_score is not None` 的片段都保留——否则 rerank 提上来的「高相关但余弦低于
阈值」的块会被误剔除，用户可见来源反而变少。纯 BM25 噪声（余弦 0 且无 rerank 分）仍被过滤。

兜底 LLM 走 OpenAI 兼容接口（`LLM_API_BASE` / `LLM_MODEL`），并接入成本熔断记账
（`ai/cost.py`，会话累计消费超阈值即中断征询）。

### 4.1 合成答案分流（P1-②，双视图）

命中后由 `SYNTH_MODE` 决定是否用 LLM 合成（`docs/合成答案分流方案.md`）：

| 档位 | 行为 |
|---|---|
| `multi_doc`（**默认**） | 广度来源 ≥2 篇（多文档信号）→ 合成；否则直出。A/B 实测 relevancy +7.87pp、coverage +10pp、faith -1.33pp（阈值 0.965 内） |
| `off` | 全部直出（P1-② 之前形态，一键回退） |
| `all` | 命中即合成 |

- **双视图**：检索一次产出两个视图（`retriever.search_dual`，同一候选池按
  `BREADTH_QUOTA`/`DEPTH_QUOTA` 各排一次名额，零额外 embedding 成本）——
  **来源列表用广度视图**（点验需要覆盖面），**合成上下文用深度视图**（top1
  文档多占名额，消除 20% 无谓拒答，见 116b933）。
- **合成实现**：v1 提示词（746ebbc 实测 faith 最高）+ 深度视图 5 块作上下文；
  输出剥掉正文内联【来源:x】标注、把引用 doc_id 并入来源列表（正文引用全部可点验）。
- **降级安全网**：LLM 未配置 / 成本熔断 / 超时 / 输出为拒答语或剥标后为空 →
  一律回退直出 top1 原文（`mode="hit"`）；合成成功 `mode="synth"`。
- **观测**：`/metrics` 的 `synth.trigger/ok/degraded/refused`；反馈管理面板按
  mode 区分 👍/👎 分布。
- **成本**：命中 +1 次 LLM 调用（DeepSeek ≈¥0.01/问）+ 1~3s 延迟（流式缓解）；
  `llm` 档限流与成本熔断沿用。

## 5. 建库流程

```bash
# 一键建库（对 7619 块做 bge-m3 embedding，约 5 分钟）
bash scripts/build_vector_store.sh
```

内部执行 `scripts/build_vector_store.py`：分块 → embedding（结果缓存到 `rag_data/emb_cache/`，语料不变时免重付）→ 建 Chroma 库 + BM25 索引，产物写入 `rag_data/vector_store/`（已被 `.gitignore` 忽略，按需重建）。

## 6. 问答接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/rag/ask` | POST | 请求 `{"question": "...", "top_k": 5}`，返回 `answer` / `mode`（`hit`=直出原文 / `synth`=LLM 合成 / `fallback`=兜底生成）/ `sources` |
| `/api/rag/ask/stream` | POST | 流式（SSE）：命中（直出或合成）逐 token 推 `delta`，结束推 `done`；未命中走兜底 |
| `/api/rag/status` | GET | 返回知识库是否就绪 `{"ready": true}` |

## 7. 检索缓存（L1 进程内 + Redis L2）

检索是问答链路里最重的一段（实测完整检索 ≈150ms/次，其中 bge-m3 embedding 占 62%），
相同问法重复检索纯属浪费，故做两级缓存 + 并发合并。

| 层 | 位置 | 内容 | 说明 |
|----|------|------|------|
| L1 | 进程内 `OrderedDict` LRU（256 条，TTL ≤60s） | 检索结果 | 命中 <0.05ms；同时也是 Redis 故障时的兜底 |
| L2 | Redis（`RAG_CACHE_TTL`，默认 3600s，写时 ±10% 抖动） | 检索结果 JSON | 多进程/多 worker 共享；命中 ≈0.34ms |

**缓存 key**：`rag:search:v5:{语料版本}:k{top_k}:{归一化问题}`

- 归一化：全角→半角、去空白、小写（「忘记密码」与「忘记密码！」同一个 key）
- `top_k` 与**语料版本**（`bm25_index.json` + `chroma.sqlite3` 的 mtime 指纹）都必须进 key，
  否则不同 `top_k` 会串用结果、语料被替换后仍命中旧结果
- **key 里不含检索策略/打分器维度**，所以每次改变排序或判定口径都必须
  **升 `_CACHE_VERSION` + 清一次 `rag:search:*`**，否则 TTL 内继续命中旧口径结果
  （表现为「改了策略线上没变」）。当前版本 **v5**：cross-encoder rerank 接入
  （排序键换 rerank 分 + 命中判定/来源过滤口径变化，见 §3.1）。清缓存命令与时机见
  `docs/Rerank方案.md` §6.1 上线执行清单
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

