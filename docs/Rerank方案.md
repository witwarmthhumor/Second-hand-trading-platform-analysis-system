# cross-encoder Rerank 接入方案

> 项目：二手交易平台分析系统 · 状态：**已实施（默认关闭，未上线 —— 见 §11 实施结论）** · 撰写日期：2026-09-18
> 关联文档：`docs/RAG优化方案.md`（P0-2 证伪）、`docs/合成答案分流方案.md`（P1-② 双视图）、`docs/RAG技术说明.md` §3.1（rerank 接入点）

## 1. 背景与动机

### 1.1 现状

检索链路的「精排」环节已有实现，但打分器是 **bge-m3 余弦相似度**：

| 位置 | 现状 |
|---|---|
| `retriever.sort_by_vsim()`（原 `rerank_by_vsim`，2026-09-19 改名消歧义） | RRF 融合后按向量相似度二次排序（`RERANK_BY_VSIM=True`，bi-encoder 余弦，非 cross-encoder rerank） |
| `retriever.diversify_with_refill()` | **生产实际走这条**（`DOC_DEDUP_ENABLED=True`），排序键同样是 `v_scores` |

两条路径的排序分数都是 bge-m3 余弦——**加名词没用，该换的是打分器**。

### 1.2 为什么必须换

`docs/RAG优化方案.md` 4.7 记录的实验（P0-2）已证伪「用 bge-m3 余弦做细粒度相关度分级」：

| 观察 | 数值 |
|---|---|
| 「相关但非期望」文档代表块 vsim 比值 中位 | 0.898 |
| 「期望」文档代表块 vsim 比值 中位 | 0.934 |
| 差距 | **仅 0.036**，分布严重重叠 |
| 多档阈值扫描（0.3 / 0.4 / 0.5） | 结果**逐位相同** → 判据根本没触发 |

结论：bge-m3 是 **bi-encoder**，query 与 passage 各自独立编码后才算余弦，无法建模二者交互；同类语料（同域、同题材）的 embedding 被压在极窄区间内，**排序分辨率不足**。

**cross-encoder** 把 (query, passage) 拼成一个序列送进模型，输出单一相关性 logits——直接建模交互，分数分布天然可分。这正是补上 P0-2 失效根因的做法。

### 1.3 预期收益

| 环节 | 预期改善 |
|---|---|
| top1 质量 | 命中后直出的是 rerank 第一名，比「全局余弦最高」更贴近真实相关 |
| 来源列表排序 | sources 按真实相关度排序，用户点验体验提升 |
| 多文档 coverage | 候选池内真相关文档更可能进入 top-k 名额（当前被同域高余弦噪声挤占）——⚠️ **仅限已进候选池的文档**，见下方预期边界 |
| 上限 | 现有 Hit@5 已 1.0000，**分母打满**，收益主要体现在 MRR@10 / nDCG@10 / coverage |

**⚠️ 预期边界（2026-09-18 实测补充，重要）**：rerank **只重排候选池，不扩大召回**。实测候选池平均 `distinct_docs@20` 仅 **4.99**（依据 `scripts/experiments/ragas_eval/results/chunk_prod.json`）——RRF 融合后的前 20 个 chunk 只来自约 5 篇文档，而多文档问题的期望是 7 篇以上。因此：

- 期望文档**已在池内但排序靠后** → rerank 能把它提到前 5，coverage 直接受益 ✅
- 期望文档**根本没进候选池** → rerank 无能为力，必须动召回层 ❌

即 **coverage 的收益上限 = 已进候选池的期望文档比例**。不要把「提高多文档覆盖」当作本次改动的 KPI——真要提高覆盖率，方向是扩召回（`QUERY_N_RESULTS` 或融合策略，另立需求）。

⚠️ 注意：Hit@5 已是 1.0000（满分），不会再涨。**这次改动的主指标是 MRR@10、nDCG@10 与 doc 级 coverage@5，不是 Hit@5**——只看 Hit@5 会把收益误判为「无效」。

## 2. 现状盘点：改动面与耦合点

### 2.1 检索链路

```
_hybrid_candidates(query)
  ├─ embedding（bge-m3）→ Chroma 余弦 Top20     → v_scores
  ├─ BM25（jieba）Top20                        → b_scores
  └─ RRF 融合 k=60                             → rrf
       │
       ▼
  search() / search_dual()
  ├─ DOC_DEDUP_ENABLED=True（生产）
  │    ranked_all = sorted(rrf, key=-v_scores[pos])      ← 【排序键 = 余弦】
  │    diversify_with_refill(ranked_all, v_scores, ...)  ← 【内部 3 处用 v_scores】
  └─ else → sort_by_vsim()                              ← 【排序键 = 余弦】
       │
       ▼
  _assemble_results(ranked, v_scores, v_texts)
       └─ 产出 {chunk_id, doc_id, text, rrf_score, vector_sim}
```

`diversify_with_refill` 内部对 `v_scores` 的依赖有 3 处（保底扫描顺序、补位排序、输出排序），且**不是简单替换**：函数契约要求 `ranked_all` 按「该文档代表块得分」降序传入，其中「代表块」= 该文档得分最高的 chunk。

### 2.2 ⚠️ 命中判定与排序结果耦合（本方案最关键的发现）

`rag_service.py` 有两处**直接用 `results[0]["vector_sim"]` 做判定**，隐含依赖「`results[0]` = 全局余弦最高 chunk」这一不变式：

| 位置 | 代码 | 作用 |
|---|---|---|
| `build_hit_gate()` L782 | `if not results or results[0]["vector_sim"] < SIMILARITY_THRESHOLD` | 命中判定（0.65），不命中走 LLM 兜底 |
| `build_hit_gate()` L788 | `if r["vector_sim"] < SIMILARITY_THRESHOLD: continue` | 来源列表过滤 |
| `_fallback_context()` L110 | `if not results or results[0]["vector_sim"] < min_sim` | 灰区片段挑选 |
| `_fallback_context()` L117 | `if r["vector_sim"] < min_sim or r["vector_sim"] >= SIMILARITY_THRESHOLD` | 灰区区间过滤 |

**rerank 一改排序，这个不变式就断了**，会出现两种坏结果：

- **收益被吃掉**：rerank 把某 cosine 0.60 但语义真相关的 chunk 排到首位 → `vector_sim=0.60 < 0.65` → 判「未命中」→ 走 LLM 兜底，rerank 白做
- **sources 被误过滤**：rerank 提升进 top-k 的低 cosine 真相关 chunk，会被 L788 从来源列表剔除，用户可见来源反而变少

所以本方案必须**同时**决定命中判定口径（见第 5 节决策点 2）。

## 3. 技术选型

### 3.1 运行方式：ONNX（推荐）vs sentence-transformers

| 维度 | ONNX Runtime（推荐） | sentence-transformers |
|---|---|---|
| 新增依赖 | **零**（`onnxruntime` / `tokenizers` / `huggingface_hub` / `numpy` 均已装） | torch（Windows CPU wheel ~200MB）+ transformers + sentence-transformers |
| 代码量 | 约 150 行（手写 pair 编码 + session 推理） | 约 10 行（`CrossEncoder.predict`） |
| 模型体积 | int8 量化版 570MB / 279MB | 需自行量化，默认 fp32 1.1~2.3GB |
| CPU 延迟 | 快（int8 + onnxruntime 图优化） | 略慢（PyTorch eager） |
| 与项目立场一致性 | 高（项目「不引 LangChain」= 倾向轻量自持） | 中（引入 torch 生态） |
| 运维复杂度 | 纯进程内，无新服务 | 同 |

**推荐 ONNX**：本机 `onnxruntime 1.29.0` 已装，走这条路**不需要新增任何重依赖**，也不必为 rerank 单独起服务（与现有 Docker/Ollama 架构不冲突）。

> 已排除的方案：
> - **Ollama**：不支持 cross-encoder 架构（只有 embedding 与 generation 两类），无法承载 reranker
> - **TEI / Infinity 容器**：需拉含 torch 的大镜像，Windows 上配置成本高，且多一个服务要管

### 3.2 模型选型

均在 `hf-mirror.com` 可下载（**HF 直连不通**，实测 `huggingface.co` 超时、`hf-mirror.com` 200 / 0.67s）：

| 模型 | 架构 | int8 体积 | 特点 |
|---|---|---|---|
| **`onnx-community/bge-reranker-v2-m3-ONNX`**（推荐） | XLM-RoBERTa-large（568M） | **570MB** | 多语言，**与现有 bge-m3 同家族**，语义空间一致 |
| `Xenova/bge-reranker-base` | XLM-RoBERTa-base（278M） | 279MB | 中英双语，更快更小，精度略弱 |
| `onnx-community/bge-reranker-v2-m3-ONNX` fp16 | 同上 | 1.1GB | 精度更高，但 **CPU 上 fp16 无硬件加速，反而更慢**，不推荐 |

两个仓库都自带 `tokenizer.json`（17MB）与 `config.json`，可直接用已装的 `tokenizers` 加载，**无需 transformers**。

**推荐 v2-m3 int8**：与 bge-m3 同家族意味着语料风格契合；int8 自包含（不依赖 external data 文件），570MB 在 500G 可用磁盘上无压力。

## 4. 方案设计

### 4.1 新增模块 `backend/app/rag/reranker.py`

```python
class CrossEncoderReranker:
    """ONNX cross-encoder 精排器（惰性单例，模型缺失时优雅降级）。"""

    def __init__(self, model_dir: Path, max_length: int = 512,
                 batch_size: int = 8, num_threads: int = RERANKER_NUM_THREADS): ...
        # US-004 实测选定 8（物理核数）；唯一口径引用 config，勿写字面量

    @property
    def available(self) -> bool: ...
        # 模型文件 / tokenizer 缺失 → False，调用方退回余弦排序

    def score(self, query: str, passages: list[str]) -> list[float]: ...
        # 批量 pair 编码 → onnxruntime 推理 → sigmoid(logits) ∈ (0,1)
        # 分词后用 padding + truncation(max_length)，一次 forward 算完 batch

def get_reranker(...) -> CrossEncoderReranker: ...
    # 惰性单例（离线闸门脚本用，不受开关影响）

def get_active_reranker() -> CrossEncoderReranker | None: ...
    # 生产链路唯一入口；RERANKER_ENABLED=false 或模型不可用 → None（连实例都不建）
```

> 实施修正（US-004）：开关**不塞进 `get_reranker()`**（会同时破坏 US-002 的单例契约
> 与 US-003 闸门脚本对真模型的依赖），另立 `get_active_reranker()` 作生产唯一入口。

要点：

- **tokenizer**：`tokenizers.Tokenizer.from_file(model_dir/"tokenizer.json")`，启用 `enable_padding` / `enable_truncation(max_length)`
- **ONNX 输入**：`input_ids` + `attention_mask`（XLM-R 不需 `token_type_ids`，但 `onnx-community` 导出版本含该输入时按 session 的 `get_inputs()` 动态构造，不硬编码）
- **输出**：`logits` shape `[batch, 1]`，过 sigmoid 归一化到 0~1
- **线程安全**：`InferenceSession` 可并发调用；显式设 `intra_op_num_threads` 防多请求下线程爆炸
- **优雅降级**：任何异常（文件缺失 / 推理失败）→ 返回 `None` 或抛给上层捕获，**检索链路必须退回余弦排序，不得让问答 500**

### 4.2 配置项（`app/rag/config.py` 追加）

| 配置 | 默认 | 说明 |
|---|---|---|
| `RERANKER_ENABLED` | **`false`（默认关）** | 关着时行为与现状**逐位一致** |
| `RERANKER_MODEL_DIR` | `backend/models/bge-reranker-v2-m3` | 模型落点（`.gitignore` 忽略） |
| `RERANKER_TOP_N` | `20` | 只对候选池前 N 个打分（= `QUERY_N_RESULTS`），不扩大召回 |
| `RERANKER_MAX_LENGTH` | `512` | pair 截断长度 |
| `RERANKER_BATCH_SIZE` | `8` | 推理批大小 |
| ~~`RERANKER_HIT_SCORE_MIN`~~ | — | **未采纳**（US-006）：来源过滤改用「余弦达标 **或** `rerank_score is not None`」双通道，不引入新阈值，故不新增配置项 |

### 4.3 检索链路接入（3 处）

**① 打分（`_hybrid_candidates` 之后新增一步 `_rerank_scores`）**

```python
# 候选池 = RRF 融合结果；reranker 可用时对 RRF 前 RERANKER_TOP_N 个打分
rerank = self._rerank_scores(query, rrf, v_texts)   # 未启用/不可用/失败 → {}
rank_scores = rerank or v_scores                    # 兜底：余弦
```

> 实施修正（US-005）：窗口外候选**不是**「按 0 分填满整个池」，而是**整个字典留空**——
> 空字典 = 「本次没有 rerank 分数」，调用方 `or` 自然退回余弦；全 0 分会让所有候选同分、
> 静默把排序变成 RRF 插入序。同理**半截分数**（长度不符）也归到空字典，宁可整体不启用。

**② 排序键替换（`search()` / `search_dual()`）**

把 `sorted(rrf.items(), key=lambda x: -v_scores.get(x[0], 0.0))` 与 `diversify_with_refill(..., v_scores, ...)` 中的排序分数统一换成上一步的 `scores`。

`diversify_with_refill` **签名与算法一行不改**（它接受任意 `scores` 字典，本就是纯函数），只是传入的字典从「余弦」变成「rerank 分数」——这是当前设计的一个红利。

**③ 结果组装（`_assemble_results`）**

新增字段 `rerank_score`（reranker 关闭时为 `None`），**保留 `vector_sim` 原义**（bge-m3 余弦，供命中判定与来源打分继续使用）。

### 4.4 命中判定解耦（与 4.3 配套，否则收益被吃掉）

核心思路：**排序用 rerank 分数，阈值判定仍用余弦，但取「候选池全局最高」而非「首位」**。

```python
# build_hit_gate 前：把全局最高余弦随结果传出（或作为独立参数）
top_sim = max((r["vector_sim"] for r in results), default=0.0)
if top_sim < SIMILARITY_THRESHOLD:
    return None
```

- `SIMILARITY_THRESHOLD = 0.65` 的**语义与标定依据完全不变**（仍是 bge-m3 余弦在 285 问上的分布空隙），无需重新标定
- `sources` 过滤同样改为「该 chunk 余弦 < 阈值 且 非 rerank 高分」——或更简单：**来源过滤改用 rerank 分数排名**，避免误剔除
- 直出 `results[0]["text"]` 语义变为「rerank 第一名」，**这是收益而非回归**

> 实施修正（US-006）：最终取「双通道」——`vector_sim >= SIMILARITY_THRESHOLD`
> **或** `rerank_score is not None` 的片段都留在 `sources`（纯 BM25 噪声仍被剔除）；
> 判定闸门（命中 0.65 / 灰区 0.4）统一改读**候选池全局最高余弦**
> `max(r["vector_sim"] for r in results)`，**不再用 `results[0]["vector_sim"]`**。
> 两处闸门共用同一口径（`rag_service._max_vector_sim`），避免「命中过了、灰区却拒答」。

### 4.5 缓存版本（必须做，否则「改了线上没变」）

| 动作 | 说明 |
|---|---|
| `cache._CACHE_VERSION` → `"v5"` | 缓存 key 不含检索策略维度，不升版本则 TTL 内继续命中旧策略结果 |
| `purge_all(settings.redis_url)` | 清理历史 `rag:search:*`；**实验前后都要清**，防实验 arm 污染现网。具体命令与时机见 §6.1 上线执行清单 |

缓存 value 形态不变（仍是 `[breadth, depth]` 双视图），`rerank_score` 字段自然随结果一起缓存，**缓存命中时零 rerank 开销**。

### 4.6 模型获取脚本 `scripts/download_reranker.py`

```python
# HF 直连不通，强制走镜像
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
from huggingface_hub import hf_hub_download
# 下载：onnx/model_int8.onnx + tokenizer.json + config.json + tokenizer_config.json
#       + special_tokens_map.json + quantize_config.json
# 落点：backend/models/bge-reranker-v2-m3/
```

支持断点续传（`hf_hub_download` 自带），并在 `.gitignore` 增加 `backend/models/`。

## 5. 决策点（已拍板，2026-09-18）

5 项均按「建议」列执行；其中 #2 落地时简化为「来源过滤双通道 + 判定改读全局最高余弦」，
**未新增阈值配置项**（见 §4.4 实施修正）。

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| 1 | 落地形态 | ①真实实现（含接入 + 评测）②先只做离线验证 ③只更新材料 | ①，但**分两步走**：先下模型跑离线对拍，数据通过再接入生产链路 |
| 2 | 命中判定口径 | ①解耦（判定用全局最高余弦，推荐）②保持现状（取首位 vector_sim）③改用 rerank 分数阈值 | ①：②会让 rerank 收益被吃掉；③需重新标定阈值，风险与成本都高 |
| 3 | 模型 | v2-m3 int8（570MB）/ base int8（279MB）/ v2-m3 fp16（1.1GB） | v2-m3 int8 |
| 4 | 来源列表排序 | ①按 rerank 分数排 ②保持余弦排 | ①，与排序口径统一；但需确认 `SourceDoc.score` 展示语义变化是否可接受 |
| 5 | 上线开关 | ①默认关，评测通过后手动开（推荐）②直接默认开 | ①，与 `SYNTH_MODE` / `DOC_DEDUP_ENABLED` 的既定上线方式一致 |

## 6. 落地步骤

| 步 | 内容 | 产出 |
|---|---|---|
| 1 | 写 `scripts/download_reranker.py`，下载 int8 模型到 `backend/models/` | 570MB 模型 + tokenizer |
| 2 | 写 `app/rag/reranker.py`：ONNX 推理 + 惰性单例 + 降级 | 模块 + 单测（mock session） |
| 3 | **分布可分性检查**（前置闸门，见 7.1） | 分数分布对比报告 |
| 4 | 加配置项 + 接入检索链路（4.3 三处） | 改动 + 对拍单测 |
| 5 | 命中判定解耦（4.4） | 改动 + 判定不变性单测 |
| 6 | 升缓存版本 + 清缓存（4.5） | `_CACHE_VERSION=v5` |
| 7 | 跑 285 问离线对拍（`--top-k 5`，开/关两组） | 指标对比表 |
| 8 | 数据通过 → 改默认值上线；不通过 → 保持默认关并记录结论 | 提交 + 文档更新 |

> 8 步已全部执行完毕（US-001~US-009）。**第 8 步走的是「不通过」分支**：对拍性能判据未达标，
> 因此**保持 `RERANKER_ENABLED=false` 默认关闭、不修改线上默认值**，结论与数据见 §11。

### 6.1 上线执行清单（缓存清理）

缓存 key **不含打分器/检索策略维度**，所以每次改变排序或判定口径都必须清一次
`rag:search:*`，否则 TTL 内会继续命中旧口径写下的结果（表现为「改了策略线上没变」）。
本需求在 **US-007** 把 `_CACHE_VERSION` 从 `v4` 升到 `v5`，key 结构变化天然淘汰旧 key；
升级版本**之外**仍按下表清一次历史 key，防同版本下实验写入的 arm 结果污染现网。

| # | 时机 | 动作 | 命令 / 说明 |
|---|---|---|---|
| 1 | **离线对拍实验前**（US-008 开跑前） | 清一次历史缓存 | `python -c "import asyncio; from app.rag.cache import purge_all; from app.config import settings; print(asyncio.run(purge_all(settings.redis_url)))"`（在 `backend/` 下用 `.venv/Scripts/python.exe`） |
| 2 | **离线对拍实验后**（两组跑完） | 再清一次，丢弃实验期写入的 arm 结果 | 同上；两组实验共用同一候选池，但缓存值里带 `rerank_score`，跨开关两态不可复用 |
| 3 | **正式上线改默认值前** | 升版本 + 清一次 | 确认 `cache._CACHE_VERSION == "v5"` 已生效，再执行步骤 1 的命令 |
| 4 | 回滚（`RERANKER_ENABLED=false`） | 清一次 | 否则 rerank 态写入的结果会在 TTL 内继续被当命中返回 |

> ⚠️ 导入路径是 **`app.config`**（不是 `app.core.config`）；`purge_all` 用一次性连接，
> 不碰进程内单例 client（见 `app/rag/cache.py`）。命令输出即被删除的 key 数，0 表示本来就没有历史缓存。

> 📌 **延迟优化轮（2026-09-19）补记**：US-003 把 `RERANKER_TOP_N` 默认 20→15。因
> `RERANKER_ENABLED` 默认关、TOP_N 在关态不参与排序，本次未升缓存版本、无污染面；
> 但开启态部署改 TOP_N（或任何 rerank 参数）后，仍须按第 3 步清一次 `rag:search:*`。

## 7. 验收标准

### 7.1 前置闸门：分布可分性检查（P0-2 的教训）

**必须先验证 cross-encoder 分数在「期望 vs 非期望」上真的可分**，再谈接不接入：

- 用 285 问 golden set，对每个问题的 top-20 候选打分
- 统计「期望文档 chunk」与「非期望文档 chunk」的分数分布（中位数、p5/p95、重叠区）
- **判据**：两组中位数差距需显著大于 P0-2 时的 0.036；若多档阈值扫描结果**逐位相同**，说明判据没触发，直接否决本方案

> 这一步不能省。P0-2 的失败正是「先实现再验证」，代价是整轮实现白做。

#### 实测结论：**通过**（2026-09-18，脚本 `scripts/experiments/ragas_eval/rerank_separability.py`）

285 问 · RRF 前 20 候选 · 5700 条记录 · 耗时 3289s（≈55 分钟，CPU int8 逐问串行）。

| 分组 | n | 中位数 | p5 | p95 | AUC |
|---|---|---|---|---|---|
| **rerank** · 期望块 | 1825 | **0.8857** | 0.0764 | 0.9963 | **0.8358** |
| **rerank** · 非期望块 | 3875 | **0.1094** | 0.0004 | 0.9476 | |
| **cosine（对照）** · 期望块 | 1825 | 0.7036 | 0.0 | 0.7860 | 0.7561 |
| **cosine（对照）** · 非期望块 | 3875 | 0.6056 | 0.0 | 0.7425 | |

- **中位差：rerank 0.7763 vs cosine 0.0980 —— 相差 7.9 倍**；相对 P0-2 的 0.036 是 **21.6 倍**，远超判定门槛（`p02_median_gap × 3 = 0.108`）。
- **阈值扫描各档互不相同**（Youden J：0.0756 / 0.3218 / **0.5020** / 0.4334 / 0.1167）→ 判据确实触发，非「扫描无差别」的假稳。最佳分离点 **0.345**（期望保留 84.1% / 噪声保留 33.9%）。
- 两组分布仍有重叠（`overlap.overlapped=true`，重叠区 0.076~0.948）——连续分数分布的常态，中位差与 AUC 已足够支撑排序收益。
- 结论落 `scripts/experiments/ragas_eval/results/rerank_separability.json`（含 285 问明细 `per_question`）。

> ⚠️ 注意：**本检查只证明 rerank 分数「可分」，不证明「排序更好」**——后者由 US-008 的 285 问对拍（MRR@10 / nDCG@10 / coverage@5）回答。

### 7.2 指标验收（线上口径 `--top-k 5`）

| 指标 | 要求 |
|---|---|
| MRR@10 | **不劣化，且应有提升**（主指标） |
| nDCG@10 | 不劣化，且应有提升 |
| 多文档 coverage@5 | 不劣化（P0-2 的教训：只看 Hit@5 会误判） |
| 单文档 coverage@5 | 不劣化 |
| chunk 级 coverage@5 | 不劣化（**先截断前 5 个 chunk、再去重**，勿写成「先去重再取前 k」） |
| Hit@5 | 保持 1.0000（已满分，不指望提升） |
| 延迟 | 缓存未命中时增量 ≤ 300ms（原始判据）；延迟优化轮（v1.9.1）放宽为 G1 ≤ 2000ms；缓存命中时零增量 |

> **实测结果见 §11.2 / §11.5**：效果 6/6 达标且有提升；延迟两轮判据均未达标
> （原始口径超 37 倍；延迟优化轮 TOP_N=15 + threads=8 后 7095.2 ms，仍超 G1 3.5 倍）
> → 整体「未达到验收标准」。

### 7.3 回归与一致性

- `RERANKER_ENABLED=false` 时，`search()` / `search_dual()` 输出与改造前**逐位一致**（对拍单测守卫）
- 命中判定解耦后，现有 285 问的「命中/未命中」判定结果**不变**（阈值语义保持）
- `ruff check .` 全绿；现有测试全绿（基线 644 passed / 2 failed[Ollama 环境性]）
- 新增测试：reranker 推理（mock session）、降级路径、排序替换、判定解耦、开关对拍

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 模型下载失败（HF 直连不通） | 强制 `HF_ENDPOINT=hf-mirror.com`；支持断点续传；失败则本方案暂缓，不阻塞其它工作 |
| **int8 量化损失精度** | 与 fp16 版本小样本对拍（20 问），差异显著则改用 fp16 并接受体积/延迟 |
| CPU 延迟超预期（>500ms） | 调 `RERANKER_TOP_N`（20→10）；调 batch；复用现有 L1/L2 缓存 —— **缓存命中时零开销**是主要缓解 |
| 内存占用（XLM-R-large int8 约 1GB） | 惰性单例 + `intra_op_num_threads` 限制；启动时不加载，首次检索才载入 |
| 命中判定耦合改动引入回归 | 对拍单测（开关关闭逐位一致）；判定不变性单测 |
| 缓存未升版本 → 「改了没变」 | 明确步骤 6，且实验前后各清一次 |
| rerank 破坏 `diversify_with_refill` 前提 | 传入前校验 `ranked_all` 按新分数降序；对拍测试覆盖 anchor/补位两条分支 |
| 与并行会话冲突 | 本次改 `retriever.py` / `rag_service.py` / `config.py` / `cache.py` —— 均为 RAG 主链路，需确认无并行会话同时改动（**不要在 ralph 循环运行期做本改动**） |

## 9. 成本估算

| 项 | 数值 |
|---|---|
| 磁盘 | 570MB（int8 模型）+ 17MB（tokenizer）+ 约 1MB 配置 |
| 内存 | 约 0.6~1.2GB（常驻，惰性加载） |
| 延迟 | ~~+100~300ms（仅缓存未命中；命中时 0）~~ **已被实测推翻，勿引用** |
| API 费用 | **0**（纯本地推理，不触达 LLM） |
| 评测费用 | 检索侧评测不调 LLM → **0**；若需生成侧复测另计 |
| 开发量 | 约 1~1.5 天（含评测与对拍） |

> ⚠️ **延迟以实测为准（§11.3 / §11.5）**：原估算 100~300ms 被推翻——TOP_N=20、
> 4 线程时 +11088 ms/问；延迟优化轮（TOP_N=15 + 8 线程）后 +6942.8 ms/问，
> 仍远超估算与 G1（≤2s）。瓶颈是 CPU 上对 XLM-R-large int8 的推理本身
> （占 rerank 段 99.8%，US-001 剖析），tokenize 仅 0.2%。

## 10. 不做的事

- **不改** RRF 融合（`RRF_K=60` 保持不变）——它只改顺序、不加成员，是 Hit@5 能到 1.0 的前提
- **不改** 三态分流阈值（`SIMILARITY_THRESHOLD=0.65` 的标定依据不变）
- **不做** MMR（2026-09-17 评估后否决，2026-09-18 补实测：MMR 是块级冗余抑制、不带文档概念，直接替换会牺牲文档覆盖率——多文档 coverage +12.97pp 全靠文档级保底。实测佐证：top-5 的 distinct_docs@5 平均 **2.544 → 4.986**、满分占比 **10.5% → 99.3%**，说明「同文档块抱团」已被 `diversify_with_refill` 消除，**MMR 没有作用对象**）
- **不引** torch / sentence-transformers（ONNX 路线已足够）
- **不扩大**召回池（`QUERY_N_RESULTS` 保持 20），只对现有候选重排
- **不落** 新服务（无 TEI / 无新容器）

## 11. 实施结论（2026-09-18）

### 11.1 落地状态：已实施、默认关闭、**未上线**

| 项 | 结论 |
|---|---|
| 代码接入 | **已完成**：模型本地化 + `reranker.py`（ONNX 惰性单例）+ 排序键替换（US-005）+ 命中判定解耦（US-006）+ 缓存 v5（US-007）+ 降级路径（US-009） |
| 开关默认值 | `RERANKER_ENABLED=false` —— **生产行为与改造前逐位一致**（对拍单测守卫） |
| 前置闸门（§7.1） | **通过**（中位差 0.7763，为 P0-2 的 21.6 倍；阈值扫描非逐位相同） |
| 285 问对拍（§7.2） | **未达到验收标准** —— 效果 6/6 达标且有提升，**延迟超标 37 倍**；延迟优化轮复测仍未达标（§11.5） |
| 是否上线 | **否**。延迟优化轮后仍超 G1（≤2s）3.5 倍，按 FR-8 效果优先保持默认关闭 |
| 降级安全网 | 已就位：模型缺失 / 分词器加载失败 / ONNX 推理异常三类故障均被捕获，**退回余弦排序且问答接口不返回 5xx**；故障发生时记一条含原因的 warning，不向用户暴露堆栈（US-009） |

> **本结论不是「静默搁置」**：代码已合入且可一键开启（`RERANKER_ENABLED=true`），
> 只是**性能判据未达标**，因此按 §6 第 8 步的「不通过」分支保持默认关闭。

### 11.2 实测指标（线上口径 `--top-k 5`，285 问，两组同候选池 285/285 一致）

| 指标 | 关闭 | 开启 | Δ | 要求 | 判定 |
|---|---|---|---|---|---|
| Hit@5 | 1.0000 | 1.0000 | +0.0000 | 保持 1.0000（已打满） | ✅ |
| MRR@10 | 0.9526 | **0.9693** | +0.0167 | 不劣化，应有提升 | ✅ |
| nDCG@10 | 0.7957 | **0.8146** | +0.0189 | 不劣化，应有提升 | ✅ |
| 多文档 coverage@5 | 0.5031 | **0.5164** | +0.0133 | 不劣化 | ✅ |
| 单文档 coverage@5 | 1.0000 | 1.0000 | +0.0000 | 不劣化 | ✅ |
| chunk 级 coverage@5（recall） | 0.1161 | **0.1166** | +0.0005 | 不劣化 | ✅ |

参考项（不参与判定）：chunk 名额内召回@5 0.2779 → 0.2835；chunk precision@5
0.2779 → 0.2835；**chunk hit1（首位是否期望块）0.9158 → 0.9439** —— 直出首段更常
落在期望块上，与「命中后直出 `results[0]`」的收益方向一致。

**结论：效果侧 6/6 全部达标，主指标各有 1.3~1.9pp 提升。**

### 11.3 唯一失败判据：延迟（缓存未命中，逐问配对）

| 指标 | 值 |
|---|---|
| 关闭态平均 | 133.4 ms/问 |
| 开启态平均 | 11221.8 ms/问 |
| 增量（平均） | **11088.4 ms/问** |
| 增量（中位 / p95） | 10904.7 / 16527.6 ms |
| 上限 | 300 ms |
| 判定 | ❌ **超上限 37 倍** |

整组耗时：开启态 285 问 **3198s**（≈53 分钟）vs 关闭态同口径 **38s**。
与 §9 的估算（+100~300ms）差一个数量级——**这是本方案未上线的唯一原因**。

缓解方向的落实情况（详见 §11.5）：`RERANKER_TOP_N` 20→15 **已做**（效果反而全场最优）、
推理线程 4→8 **已做**（-20%）、`max_length` 降档**未做**（25.7% 候选顶满 512 截断，
降档会动效果口径）、换 base int8 模型 / GPU / 专用推理服务**未做**（属新需求，
PRD 明确排除）；**缓存命中时零增量**仍是线上主要缓解手段，但缓存未命中（新问题）
的延迟无法掩盖。

### 11.4 数据与复现

| 文件 | 内容 |
|---|---|
| `scripts/experiments/ragas_eval/results/rerank_ab.json` | 逐项判定的机读结论（US-008 原始轮） |
| `scripts/experiments/ragas_eval/results/rerank_ab_report.md` | 人读版对比报告（末节为延迟优化后验收） |
| `scripts/experiments/ragas_eval/results/rerank_ab_report_us008.md` | US-008 原始轮报告备档 |
| `scripts/experiments/ragas_eval/results/rerank_ab_off.json` / `_on.json` | 原始轮两组逐问明细（含候选池留痕） |
| `scripts/experiments/ragas_eval/results/rerank_lat_off.json` / `_on.json` / `rerank_after_opt.json` | 延迟优化轮两组明细与判定 |
| `scripts/experiments/ragas_eval/results/rerank_profile.json` | US-001 延迟构成剖析（onnx 99.8% / tokenize 0.2%） |
| `scripts/experiments/ragas_eval/results/rerank_topn_scores.json` | US-002 TOP_N=20 全量真跑落盘（池序 + 全部分数） |
| `scripts/experiments/ragas_eval/results/rerank_topn_derived.json` | 10 档离线推导效果（TOP_N=20 对拍逐位一致） |
| `scripts/experiments/ragas_eval/results/rerank_topn_latency.json` | 延迟实测（含 US-004 线程/批矩阵） |
| `scripts/experiments/ragas_eval/results/rerank_topn_sweep.md` | 效果-延迟权衡表（选点依据） |
| `scripts/experiments/ragas_eval/results/rerank_separability.json` | 前置闸门的分组分布与阈值扫描 |

复现（完整命令见 `scripts/experiments/ragas_eval/rerank_ab.py` 模块 docstring）：

```bash
# 1) 实验前清缓存（§6.1 步骤 1）；跑完两组后再清一次（步骤 2）
cd backend && .venv/Scripts/python.exe -c "import asyncio; from app.rag.cache import purge_all; from app.config import settings; print(asyncio.run(purge_all(settings.redis_url)))"
cd ..
# 2) 两组各跑一次（约 53 分钟/组，CPU 上跑批期间不要并行跑全量 pytest）
RERANKER_ENABLED=false backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --strategy rerank_off --top-k 5 --capture-pool --out rerank_ab_off.json
RERANKER_ENABLED=true  backend/.venv/Scripts/python scripts/experiments/ragas_eval/run_eval.py \
    --strategy rerank_on  --top-k 5 --capture-pool --out rerank_ab_on.json
# 3) 出对比表与结论（判据写死在脚本常量里，无 CLI 开关）
backend/.venv/Scripts/python scripts/experiments/ragas_eval/rerank_ab.py
```

### 11.5 延迟优化轮（2026-09-19，PRD：`tasks/prd-rerank-latency.md`）

**结论先行：延迟 11221.8 → 7095.2 ms/问（-37%），但仍未达 G1（≤2s），按 FR-8 效果优先保持默认关闭。**

做了什么（6 story 全部完成）：

| 步骤 | 结果 |
|---|---|
| US-001 延迟剖析 | rerank 段中 **ONNX 推理占 99.8%、tokenize 仅 0.2%**（30 问，`rerank_profile.json`）——tokenize 缓存按 <5% 阈值直接否决 |
| US-002 TOP_N 扫描 | 一次真跑落盘 + 离线切档（推导 vs 真跑逐位对拍一致）；**FR-2 合规下限 TOP_N=12**（11 起多文档 coverage 跌破关闭态）、加辅助指标（单文档 c@5=1.0）后完全合规仅 **{15, 20}** → **选点 TOP_N=15**（延迟最低且三项主指标全场最优） |
| US-003 改默认值 | `RERANKER_TOP_N` 20→15 + 防御单测（默认值 ≥ TOP_K）；⚠️ 改 TOP_N 必须清 `rag:search:*`（缓存 key 不含它） |
| US-004 零依赖调优 | **threads 4→8 采纳（-20%）**；threads=16 反而退化（超线程争抢）、batch 16/32 无收益或更差，均否决；新增 `RERANKER_NUM_THREADS` 配置 |
| US-005 复测验收 | 285 问两态对拍（池一致 285/285）：**效果 6/6 达标**（MRR +0.0173 / nDCG +0.0167 / 多文档 c@5 +0.0117），**延迟 7095.2 ms 未达 ≤2s**（均值≈中位，分布健康） |

**为什么 ≤2s 不可达（数学边界）**：`RERANKER_TOP_N` 硬下限是 5（= TOP_K），加速上限
4×（11221.8 → ≈2800 ms；实测 TOP_N=5 为 3292 ms）——在不换模型、不引 GPU 的约束下，
纯降 TOP_N 触底也够不到 2s。US-004 的线程收益（-20%）叠加后最优组合 ≈7.1s，
仍差 3.5×。**要真正达标必须换 base 模型（预计再 ~2×，需重跑闸门与对拍）或引入
GPU/专用推理服务——均属新需求另立 PRD。**

技术档案（后续排查必读）：

- **int8 动态量化的批组成效应**：DynamicQuantizeLinear 的 scale 取自输入张量
  min/max，批组成不同（8+8+4 vs 8+7）分数有 ~0.1 扰动；实测在 15/10/5 三档
  30 问样本上**指标差全部为 0**（只影响问内近邻名次）。
- **Ollama 重启后 bge-m3 embedding 微漂**：约 1/10 问的候选池边界成员翻转；
  同一 Ollama 生命周期内完全确定（池守卫 285×6 档零漂移）。跨生命周期的池比对
  会误报，评测的两臂必须在同一次 Ollama 会话内跑完。
- **评测主机休眠会污染逐问墙钟**（均值被拉爆、中位不受影响）：长评测须连续轮询
  防休眠；遇均值 ≫ 中位先查总耗时是否 ≫ CPU 时间。
- **`RERANKER_ENABLED` 不能写进 `backend/.env`**：`app.config.Settings` 为
  extra=forbid，会炸掉整个应用导入；临时开启用进程级环境变量。

复现命令见 `scripts/experiments/ragas_eval/rerank_topn_sweep.py` 与
`rerank_ab.py` 的模块 docstring。
