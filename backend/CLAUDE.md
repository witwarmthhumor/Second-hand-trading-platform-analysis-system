# backend（FastAPI + RAG）开发备忘

## LLM 调用层约定（app/services/rag_service.py）

- **唯一 HTTP 出口是 `_post_chat(payload, transport=None)`**：chat/completions 的端点、
  鉴权、60s 超时与错误翻译（`_llm_error_hint`：401/402/429/5xx→中文 hint，抛
  `LLMCallError`）都在这里。新增任何 LLM 入口都要复用它，不要自己 `httpx.post`。
- **`transport` 参数是测试注入口**，生产留 None。测试用
  `httpx.MockTransport(handler)` 注入，`handler` 里记下 `json.loads(request.content)`
  就能断言请求体。
- **每个会花钱的入口都要「预检 + 记账」两步**：进 HTTP 前 `_precheck_tokens`
  （messages 之外的负载要算进估算，否则低估会绕过熔断），成功后
  `_record_usage`（网关没回 usage 时按正文估算兜底，不能记 0）。失败调用不记账。
- **测试里替换记账器要 patch 消费方模块属性** `rag_service.get_cost_tracker`
  （返回带 `restore_from_db` / `precheck` / `record` 的假对象即可，避免碰 DB）。
- **token 估算的唯一口径是 `estimate_tokens`（US-013 提升为公开名）**；估算范围
  要含 messages 之外的负载，否则低估会绕过预算闸门。
- **「一次调用大概多少钱」统一走 `CostTracker.estimate_call_cny`**（输入 → 输出 1/5
  的比例只有这一份，`precheck` 与调用前预检共用）。

## Agent 模块（已撤回）

> 多 Agent 竞品情报流水线已于 commit `e3291d1`（2026-09-20）整体撤回：
> `app/agents/`、`api/agent.py`、`models/agent_run.py` 与相关测试/前端页面均已删除；
> dev 库的 `agent_runs` 表与 alembic 历史迁移**有意保留**（只撤代码不撤数据）。
> 如需恢复，从 git 历史还原 e3291d1 的父提交即可。

## 本地模型资产（cross-encoder reranker，US-001）

- **模型不进版本库**：落点 `backend/models/bge-reranker-v2-m3/`（`.gitignore` 忽略），
  由 `scripts/download_reranker.py` 从 `hf-mirror.com` 下载（HF 直连超时，脚本在
  `import huggingface_hub` **之前**强制 `HF_ENDPOINT`，并显式传 `endpoint=`）。
  换机 / 新克隆后跑一次即可；已存在且体积达标的文件会跳过，中断后重跑可续传。
- **别拿 `onnx/model.onnx`**：它只有 656KB，是 fp32 权重的 external-data **stub**
  （真正的 2.3GB 在 `onnx/model.onnx_data` 里，不在下载清单内）。要的是自包含的
  `onnx/model_int8.onnx`（570,727,094 字节）。拿错要等 `InferenceSession` 加载时才炸，
  报错指向「模型损坏」而不是「拿错文件」——下载脚本按下限校验体积就是为了提前到下载期。
- **落地文件是平铺的**（脚本把仓库里的 `onnx/` 层级拍平）：`model_int8.onnx` /
  `tokenizer.json` / `config.json` / `tokenizer_config.json` / `special_tokens_map.json`。
- 实测该模型的 ONNX 输入恰为 `input_ids` + `attention_mask`（**无** `token_type_ids`）、
  输出 `logits [batch, 1]`。**单测不要加载真模型**（570MB，会把用例拖成分钟级），
  用假 session；真模型留给离线实验脚本。

## cross-encoder 精排模块（app/rag/reranker.py，US-002）

- **两个注入点是模块级属性**：`_load_tokenizer` / `_create_session`（同 `fetch_url` /
  `get_retriever` 的约定）。单测 monkeypatch 它们换成假实现即可覆盖「正常打分 /
  文件缺失 / 加载失败 / 推理异常」四条分支，不必碰 570MB 真模型。
- **打分必须走 `tokenizer.encode_batch([(query, passage), ...])`**：tokenizers 的
  padding 只在**批内**对齐，逐条 `encode()` 得到的是不等长序列，`np.asarray` 会抛
  `inhomogeneous shape`。该异常会被降级分支吞掉、表现为「分数恒为空」的静默失效——
  假 tokenizer 因此**只实现 `encode_batch`**，生产代码退回逐条编码时用例会立刻红。
- **feed 按 `session.get_inputs()` 动态构造**（本模型恰为 `input_ids` +
  `attention_mask`）；模型要求未知输入时抛错，同样被 `score()` 吞成空列表，
  不冒险塞错张量。
- **`score()` 的契约：返回与入参等长的分数列表；不可用 / 任何异常 → 返回空列表。**
  调用方按「长度不足即视为没打成」退回原排序（US-009 依赖这个契约）。**不要改成
  向上抛异常**，检索链路不能因为 reranker 挂掉而 5xx。
- **`get_reranker()` 的参数只在首次调用生效**（惰性单例 + 锁，防并发首请求加载两份
  模型）；测试要换实例就 monkeypatch 模块级 `_reranker = None` 后再取。
- **两个取用入口分工不能混**（US-004）：`get_reranker()` 是纯惰性单例，给离线实验
  脚本用（`scripts/experiments/ragas_eval/rerank_separability.py` 要真模型，不受开关
  影响）；**生产链路一律用 `get_active_reranker()`** —— 开关关闭（默认）或模型不可用
  都返回 `None`，且关闭态**连实例都不建、不碰模型目录**。调用方（retriever）拿到
  `None` 就按原排序走，不必自己判 `available`。
- **rerank 配置在 `app/rag/config.py`（模块级常量 + `os.getenv`），不是
  `app/config.py` 的 `Settings`**：`RERANKER_ENABLED`（默认 false）/ `RERANKER_MODEL_DIR`
  / `RERANKER_TOP_N`（默认跟随 `QUERY_N_RESULTS`）/ `RERANKER_MAX_LENGTH` /
  `RERANKER_BATCH_SIZE`。reranker.py 的 `DEFAULT_MODEL_DIR` / `DEFAULT_MAX_LENGTH` /
  `DEFAULT_BATCH_SIZE` **引用这些常量**（唯一口径），别另写字面量。测试里改开关/参数就
  monkeypatch `reranker` 模块的这几个同名属性（它们是模块全局，调用时才读）。
- **布尔开关的解析约定**：只有 `1/true/yes/on`（忽略大小写与空白）算开，其余（含拼错
  的值）一律回落 False——把开关拼错的代价必须是「没开」。新增同类开关照抄这个写法。
- 该组值会改变检索排序，而**缓存 key 不含这些维度**：改动后必须清 `rag:search:*`
  （或升 `cache._CACHE_VERSION`，见 US-007）。
- 真机冒烟（2026-09-18，本机）：加载 1.3s、3 条 pair 推理 0.14s；相关片段 0.9935 vs
  无关片段 0.0007 / 0.0052 —— 差距远大于 P0-2 余弦的 0.036，US-003 的可分性闸门可期。
- **`RERANKER_ENABLED` 保持默认 false 是有依据的，不要「顺手打开」**：285 问对拍
  （`--top-k 5`）效果侧 6/6 达标（MRR@10 0.9526→0.9693、nDCG@10 0.7957→0.8146），
  但**延迟增量 11088 ms/问，超 300ms 上限 37 倍** → 整体判「未达到验收标准」。
  结论、实测表与复现命令在 `docs/Rerank方案.md` §11；改默认值前先读它（人工评审后
  再决定是否接受「延迟换效果」）。

## 检索链路接入 rerank（app/rag/retriever.py，US-005）

- **打分步骤是 `HybridRetriever._rerank_scores(query, rrf, v_texts)`**：只对
  **RRF 排序前 `RERANKER_TOP_N`** 个候选打 cross-encoder 分，窗口外按 0（只重排、
  不扩召回）。返回 `pos → 分`，**未启用 / 不可用 / 打分失败一律返回空字典**；
  `search()` / `search_dual()` 用 `rank_scores = rerank or v_scores` 决定排序键
  —— 空字典即「本次没有 rerank 分数」，原样退回余弦。**别用「全 0 分」表达失败**
  （会让所有候选同分、静默改变排序），也别把空字典当真分数用。
- **`diversify_with_refill` / `dual_views` 一行不改**：它们是接受任意 scores 字典的
  纯函数，接入方式是**换传进去的分数**（`rank_scores`），于是「锚定文档」= 当前排序
  分最高所在文档、补位也按该分排序。改这两个函数会同时影响余弦态与 rerank 态。
- **半截分数整体不启用**：reranker 的契约是「等长或空列表」，长度不符说明状态异常，
  `_rerank_scores` 记 warning 后返回空字典（而不是「部分分数 + 部分 0」的半开排序）。
- **`_assemble_results` 的 `rerank_score` 字段**：未启用或该 chunk 不在打分窗口内
  为 `None`（不是 0.0）。`vector_sim` **永远是 bge-m3 余弦、语义不随 rerank 改变**
  —— 命中判定读的是它（解耦见 US-006），别把 rerank 分塞进 `vector_sim`。
- **`DOC_MIN_SIM` 闸门自 US-005 起比较「当前排序分数」**：rerank 开启时传入的是
  cross-encoder 分（与余弦不同量纲）。默认 0.0（关闭）所以生产无影响；换打分器后要
  重新启用闸门必须按新分布重标定。
- **测试注入点**：`retriever.get_active_reranker`（模块级属性）+ 实例属性覆盖
  `_hybrid_candidates`（绕开 `__init__` 的 Chroma client）+ `retriever.RERANKER_TOP_N`
  改窗口。见 `tests/test_rag_rerank_flow.py`。

## 命中判定与排序解耦（app/services/rag_service.py，US-006）

- **判定口径只有一份：`_max_vector_sim(results)`**（候选池全局最高 bge-m3 余弦）。
  `build_hit_gate` 与 `build_fallback_context` 的闸门都读它，**不要再用
  `results[0]["vector_sim"]`** —— rerank 接入后排序键是 cross-encoder 分，
  `results[0]` 未必是余弦最高者，用首位会把「rerank 判为最相关、余弦 0.60」误判成
  未命中（收益被吃掉）或把「池里另有临界片段」误判成「连沾边都算不上」。
  阈值本身（`SIMILARITY_THRESHOLD=0.65` / `FALLBACK_CONTEXT_MIN_SIM=0.4`）语义与
  标定依据**不变**，US-006 只换「取哪个 chunk 的分」，不重标定。
- **sources 过滤是「双通道」**：余弦 ≥ 阈值 **或** `rerank_score is not None`
  （被 cross-encoder 打过分的候选）都保留 —— 否则 rerank 提上来的真相关 chunk 会
  被来源列表剔除，用户可见来源反而变少。纯 BM25 噪声（余弦 0 且无 rerank 分）
  仍被过滤，保住「不展示相关度 0.0% 的无意义引用」这条既有回归。
- **直出文本仍是 `results[0]["text"]`**：rerank 态下即「cross-encoder 第一名」，
  这是收益不是回归（设计见 docs/Rerank方案.md 4.4）。改判定时别顺手改成「余弦最高
  那条」，会把 rerank 的排序收益整个抵消。
- 回归守卫在 `tests/test_rag.py` 的「US-006 命中判定与排序解耦」一节（首位低余弦 +
  池内另有达标块 → 命中；全局最高不达标 → 未命中；灰区闸门同口径）。

## 检索缓存版本与清理（app/rag/cache.py，US-007）

- **缓存 key 不含「检索策略 / 打分器 / 判定口径」维度**（key = `rag:search:{版本}:{语料版本}:k{top_k}:{归一化问题}`）。
  因此**任何改变排序或命中判定的改动都必须升 `_CACHE_VERSION` 并清一次 `rag:search:*`**，
  否则 TTL 内会继续命中旧口径写下的结果，表现为「改了策略线上没变」。当前版本 **v5**
  （v4→v5 对应 cross-encoder rerank 接入：排序键换 rerank 分 + 判定/来源过滤口径变化）。
- **升版本之外仍要清一次历史 key**：同版本内做 A/B（如 rerank 开/关两态对拍）会写入两套
  互不可复用的 arm 结果，实验前后各清一次防污染现网。清缓存命令与时机见
  `docs/Rerank方案.md` §6.1 上线执行清单。
- **清缓存用 `purge_all(settings.redis_url)`**（`from app.rag.cache import purge_all`；
  配置在 **`app.config`**，不是 `app.core.config`）。它走一次性连接，不碰进程内单例 client，
  适合后台线程 / 独立事件循环调用；Redis 不可达时返回 0 且不抛异常。
- **`rerank_score` 随缓存值一起落库**：缓存命中时零 rerank 开销（不会重复推理），
  但也意味着开关切换后**必须清缓存**才能看到新排序。
- 版本号由 `tests/test_rag_cache.py::test_cache_key_version_is_v5` 守住。

## rerank 开关两态对拍（US-008）

- **对拍流程**（两组必须同语料、同 `--top-k 5`、同候选池）：
  实验前按 §6.1 清一次缓存 → `RERANKER_ENABLED=false` 与 `true` 各跑一次
  `scripts/experiments/ragas_eval/run_eval.py --top-k 5 --capture-pool` →
  `python scripts/experiments/ragas_eval/rerank_ab.py` 出判定。完整命令见
  `rerank_ab.py` 模块 docstring。
- **`--capture-pool` 是「同候选池」的唯一证据来源**：它把每题 `_hybrid_candidates`
  的返回**留痕**（`candidate_ids`）与返回块号（`result_chunk_ids`）写进 per_q。
  留痕复用 `search()` 内部那一次候选池计算，**不额外调 embedding**，所以不增加耗时；
  用完会还原实例方法（同进程连跑两组不会串味）。判定逻辑在
  `rerank_ab.pool_check`：池子不一致或返回块落在池外都判不达标。
- **判定口径写死在 `rerank_ab.py` 的模块常量里，不做 CLI 开关**：效果指标一律
  「不劣化」、Hit@5 必须保持 1.0、延迟增量 ≤300ms、候选池逐问一致。主入口还会
  拒绝误用（两份同态 / 非 `top_k=5` / 问数不同）——避免把无效对比当成上线依据。
- **rerank 组很慢，别在跑批时并行跑全量 pytest**：285 问 × 20 候选实测
  ≈17s/问（XLM-R-large int8、4 线程、batch 8），整轮约 80 分钟；关闭态同口径
  只要 38s。CPU 争抢会让两边都变慢一倍。
- **延迟增量按「逐问配对相减」算，不用「总耗时 ÷ n」**：后者会把首问的模型加载
  （约 1.3s）摊到每一问上，压低稳态增量。`latency_stats` 同时给平均/中位/p95。

## rerank 降级路径与开关一致性（US-009）

- **三类故障各自的降级点，改动时别搞混**：
  ① 模型文件缺失 → `reranker._load()` 的**文件存在性前置检查**直接判不可用，
  **连加载都不尝试**（`_load_tokenizer` / `_create_session` 一次都不会被调到）；
  ② 分词器 / 模型加载失败 → `_load()` 的 `try` 捕获，`available=False`；
  ③ 推理抛异常 → `score()` 捕获后返回**空列表**。
  三者最终都经 `get_active_reranker()` 翻成 None / 空字典，检索退回余弦排序。
- **降级日志分两处、各管一段**：`reranker` 侧记「为什么失败」（含异常原因）；
  `retriever._rerank_scores` 侧记「本次检索已退回余弦排序」。**整体打不出分**
  （空列表）与**半截分数**（长度不符）是两条不同的 warning —— 别把前者写成
  「长度不符（0 != N）」，那会把已知降级误报成状态异常。
- **惰性降级 = 启动零风险**：`get_active_reranker()` 只在**检索时**被调用，
  进程启动（建 `HybridRetriever` / 导入 `app.main`）不碰模型目录。失败实例会被
  惰性单例缓存住，所以坏模型是「每次检索都退回余弦」而**不是**「每次检索都重试加载」。
- **测试注入点**：走真实降级链路要 monkeypatch `reranker` 模块的
  `RERANKER_ENABLED` / `RERANKER_MODEL_DIR` / `_reranker=None` + 两个加载器，
  再把 `retriever.get_active_reranker` 包一层「可切关闭态」的 lambda 取基线
  （`tests/test_rag_rerank_degradation.py`）。**不要**在这里用替身精排器 ——
  那是 `test_rag_rerank_flow.py` 的活（只验排序键替换）。
- 「问答接口不返回 5xx」的服务层等价物是 `rag_service.ask` 照常命中直出
  （同一个测试文件里有一条 async 用例守着）。

## 其它

- 检索结果的字段口径（`doc_id` / `vector_sim` / `rrf_score`）与 AskResponse 契约见
  `app/schemas/rag.py`；`title_from_doc_id` 是 doc_id→标题 的唯一公开入口。
## Rerank 术语口径（2026-09-19 质量审计后约定）

- 本项目里 **"rerank" 专指 cross-encoder 精排**：`retriever._rerank_scores` /
  结果字段的 `rerank_score` / `app/rag/reranker.py`。
- **`retriever.sort_by_vsim`**（原 `rerank_by_vsim`，2026-09-19 改名）是 bi-encoder
  余弦二次排序，`DOC_DEDUP_ENABLED=false` 时的兜底分支，与 cross-encoder 无关——
  别在注释或新代码里把它叫做「rerank」。
- `RERANKER_NUM_THREADS` 是**每次 run 的算子内线程数**：并发 N 个请求的峰值线程
  = N×该值（详见 `reranker.py` 的 DEFAULT_NUM_THREADS 注释）。rerank 上线前必须
  处理并发叠乘（信号量限并发或按部署核数压低线程）。
