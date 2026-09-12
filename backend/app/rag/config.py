"""RAG 配置常量：集中管理 embedding 模型、检索参数与路径约定。

关键环境细节：Ollama 必须用 ``127.0.0.1`` 而非 ``localhost``——
本机同时跑着 Docker 容器 ``ai-interview-ollama``（占用 0.0.0.0:11434，
里面只有 nomic-embed-text），``localhost`` 会优先命中容器；宿主机 Ollama
（装有 bge-m3）监听 ``127.0.0.1:11434``，因此这里强制用 IP 直连宿主机。
"""

import os
from pathlib import Path

# 项目根目录（backend/ 的上一级，即 Agent/）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# ---- 语料资产路径（rag_data/）----
RAG_DATA_DIR = PROJECT_ROOT / "rag_data"
CANONICAL_DIR = RAG_DATA_DIR / "canonical"
FAQ_SOURCE_DIR = RAG_DATA_DIR / "faq_source"
FACTS_DIR = RAG_DATA_DIR / "facts"
SEEDS_CSV = RAG_DATA_DIR / "topic_seeds_1000.csv"
VECTOR_STORE_DIR = RAG_DATA_DIR / "vector_store"

# ---- Ollama embedding（宿主机，用 IP 直连避免命中 Docker 容器）----
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024  # bge-m3 输出维度

# ---- 分块参数（对齐 B 项目评测最优 chunk_size=900，hybrid MRR@10 0.9559 / nDCG 0.8389）----
CHUNK_SIZE = 900  # 字
CHUNK_OVERLAP_RATIO = 0.10

# ---- 检索参数 ----
RRF_K = 60
TOP_K = 5  # 问答返回的候选文档数
QUERY_N_RESULTS = 20  # 向量/BM25 各自召回数（供 RRF 融合）
SIMILARITY_THRESHOLD = 0.65  # 命中阈值：最高向量相似度(cosine 0~1)低于此视为未命中，走 LLM 兜底。
# 取值依据（rag_data/eval/golden_queries.yaml 285 问实测 top1 相似度分布）：
#   正确 top1（261 条）最低 0.6660 / p5 0.6919；错误 top1（24 条）最低 0.6483 / 最高 0.8116。
#   0.65 恰好落在 [0.6483, 0.6660] 这段空隙里：误伤正确样本 0 条。
#   领域外闲聊实测仅 0.6235（「你觉得转转这个平台怎么样」）——旧值 0.55 挡不住，
#   会把领域外问题误判为「命中」直出无关原文（用户报障）；0.65 可挡住。
#   继续上调有明确代价：0.68 漏判 6 条正确、0.70 漏判 23 条、0.75 漏判 104 条。
# 注意：本阈值只决定「直出原文 vs LLM 兜底」，不参与检索召回，故不影响 RAGAS 指标。
# rerank 精排：RRF 融合后按向量相似度二次排序（压掉 BM25 纯关键词误命中噪声）。
# RAGAS 实验（scripts/experiments/ragas_eval）验证：开启后 Hit@5 0.9825→1.0000、
# MRR@10 0.9186→0.9506、nDCG@10 0.7960→0.8280。代价是 chunk 级 context_precision
# 在多文档问题上略降（0.7414→0.7370），但 doc 级指标全面大幅提升，净收益明确。
RERANK_BY_VSIM = True
# 实测：相关查询约 0.70~0.74，无关查询约 0.48。注意不能用 RRF 得分(量级仅 0.016~0.033)。

# 多文档召回：文档级去重 + 保底补位（见 retriever.diversify_with_refill）。
# 动机：top_k=5 是固定预算，同文档 chunk 抱团占位导致多文档问题期望文档覆盖率
# 仅 0.3755（distinct_docs@5 平均 2.84）。开启后每篇文档保底 1 个 chunk、
# 名额不足再回填同文档次优 chunk。
# 实测（285 问，top_k=5，docs/RAG优化方案.md）：多文档 coverage 0.3755→0.5052、
# 单文档 0.9755→1.0000、Hit(任意一篇) 0.9825→1.0000，三项同时变好。
# 已开启（2026-09-11 评审通过）。开启前按第三节验收标准复测 285 问（生产 top_k=5）：
#   多文档 coverage 0.3755→0.5052（+12.97pp）、单文档 0.9755→1.0000、Hit 0.9825→1.0000
#   —— 三项均不劣于 baseline，单文档与 Hit 打满，属帕累托改进（docs/RAG优化方案.md）。
# 注意：上线同时需把检索缓存 key 版本升级（cache.py 的 _CACHE_VERSION → v3），
# 否则旧策略下写入的缓存在 TTL 内仍会命中，导致「改了配置但线上没变」。
DOC_DEDUP_ENABLED = True

# 保底阶段的相关性闸门（P0-2 原方案）：文档代表 chunk 的 vsim 低于本阈值则
# 整篇跳过、不占名额。**实测证伪、默认关闭**——在 bge-m3 的相似度标尺上，
# 「相关但非期望」文档与「期望」文档的代表块 vsim 比值分布严重重叠
# （单文档问题的噪声比值中位 0.898 vs 多文档问题的期望比值中位 0.934），
# 绝对阈值几乎无区分度：0.3~0.5 完全不触发（候选池内各文档代表块 vsim 普遍
# ≥0.55），0.6 才微微起效（单文档 chunk precision 0.2000→0.2613）却伴随
# 多文档 coverage 下降 0.5031→0.4954，不是帕累托改进。保留实现以备更换
# embedding 模型后复用。详见 docs/RAG优化方案.md 4.7。
DOC_MIN_SIM = 0.0

# 锚定文档名额（P0-2 修正方案，**替换**阈值判据）：允许 top1 所在文档
# （= 全局 vsim 最高 chunk 的文档，hit1 0.9158 印证它大概率就是主文档）
# 在保底阶段多占名额，其余文档仍各 1 个。
#   = 1（默认，维持现网）：最大化文档广度——**今天必须保持**，因为生产 `ask()`
#     命中即直出 `results[0]` 原文（无 LLM 合成），top-5 的唯一消费者是**来源列表**，
#     它需要广度；换成 4 会让用户可见来源覆盖 0.7873→0.7416（-4.57pp，仍高于
#     baseline 0.7187），却拿不到任何答案收益。
#   ↑：偏向深度（单文档问题的上下文更完整）；**是合成路径（P1-②）的硬前置**——
#     生成侧实测，配额 1→4 使合成 faithfulness 0.7907→0.9662、relevancy
#     0.7398→0.9078（单文档问题 +27.32pp / +30.66pp），并回到 P0 前水准
#     （0.9682 / 0.8986，P0 上线前无去重）。故**合成上线时必须与深度上下文配套**
#     （直接把本值调到 4，或实现「双视图」：来源列表用广度、合成上下文用深度）。
# 动机：P0 上线后单文档问题的 top-5 里「期望文档」占位均值从 3.56 掉到 1.00，
# 深度全丢（chunk 级 precision 0.7104→0.2000），而 vsim 无法区分问题类型，
# 故把名额分配变成显式旋钮。检索侧实测曲线（285 问，top_k=5）：
#   配额 1          多文档 cov 0.5031 / chunk 名额内 0.2779 / hit1 0.9158
#   配额 2          多文档 cov 0.4622 / chunk 名额内 0.4246 / hit1 0.9158
#   配额 3          多文档 cov 0.4372 / chunk 名额内 0.5551 / hit1 0.9158
#   配额 4          多文档 cov 0.4046 / chunk 名额内 0.6505 / hit1 0.9158
#   （baseline 无去重：0.3755 / 0.7053 / 0.8702）
# 生成侧依据（40 问 v1 合成 A/B，DeepSeek，实花 ¥1.28，见 RAGAS 评估实验.md 第十三节）。
# 支持环境变量覆盖（`ANCHOR_QUOTA=4` 即可试用深度形态，无需改代码）。
# ⚠️ 检索缓存 key 不含该策略维度：改配额后**必须清一次 `rag:search:*` 缓存**
# （或升级 cache._CACHE_VERSION），否则 TTL 内仍命中旧策略的结果。
ANCHOR_QUOTA = int(os.getenv("ANCHOR_QUOTA", "1"))

# ---- 双视图配额（P1-② 合成答案分流，见 docs/合成答案分流方案.md）----
# 一次检索产出两个 top-k 视图（同一候选池，diversify_with_refill 纯函数各排一次，
# 零额外 embedding/向量检索成本）：
#   广度视图（BREADTH_QUOTA，默认跟随 ANCHOR_QUOTA=1）→ 来源列表（用户点验入口，需广度）；
#   深度视图（DEPTH_QUOTA，默认 4）→ 合成上下文（top1 文档多占名额，消除无谓拒答）。
# 依据：116b933 生成侧 A/B——配额 4 使合成 faithfulness 0.7907→0.9662（拒答率
# 20%→0），但全局切 4 会让用户可见来源覆盖 0.7873→0.7416，故来源与上下文
# 各用各的视图，互不牺牲。
# ⚠️ 检索缓存 key 不含配额维度：改动这两个值后必须清 `rag:search:*`
# （或升级 cache._CACHE_VERSION），否则 TTL 内仍命中旧配额的结果。
BREADTH_QUOTA = int(os.getenv("BREADTH_QUOTA", str(ANCHOR_QUOTA)))
DEPTH_QUOTA = int(os.getenv("DEPTH_QUOTA", "4"))

# ---- 合成答案分流（P1-②，见 docs/合成答案分流方案.md）----
# 命中（build_hit_gate）后的路径选择：
#   multi_doc **默认（2026-09-12 生成侧 A/B 裁定）**：广度视图 sources ≥ 2 篇
#     （多文档信号）才合成，单文档留直出；合成失败/拒答一律降级直出。
#   off       全部直出原文（P1-② 之前的现网形态，可随时回退）。
#   all       命中即合成。
# 裁定依据（compare_synth_mode.py，40 问单/多各半 seed=42，实花 ≈¥3.2，RAGAS 裁判
# + DeepSeek 生成）：multi_doc 对直出——relevancy 0.8209→0.8996（+7.87pp）、
# coverage 0.6025→0.7025（+10pp）、faithfulness 1.0→0.9867（-1.33pp，预登记阈值
# 0.965 通过）；合成 40/40 全部 ok（零降级零拒答）；6 项判定全过 → 判定
# multi_doc_pass=True。all 与 multi_doc 几乎重合（40 问中 38 问 sources≥2 触发
# 分流），multi_doc 保留「单文档直出」逃生舱语义且更保守，故取 multi_doc。
# 判据刻意用「sources 篇数」而非相似度阈值——bge-m3 相似度不可做细粒度分级
#（P0-2 证伪）；注意本语料同域相似度普遍 ≥0.65，sources 几乎总是 4~5 篇，
# 即分流判据当前近似「恒合成」，保留判据以备语料异质化后的成本控制。
# 合成上下文用深度视图（DEPTH_QUOTA），来源列表仍用广度视图。
_SYNTH_MODE_RAW = os.getenv("SYNTH_MODE", "multi_doc")
SYNTH_MODE = _SYNTH_MODE_RAW if _SYNTH_MODE_RAW in ("off", "multi_doc", "all") else "off"

# v1 提示词的拒答语（要求 2）。合成输出包含该短语 → 视为「片段不足以回答」，
# 降级直出 top1 原文（top1 是全局最相似 chunk，比拒答更稳妥）。116b933 实测
# 深度视图下拒答率 0/40，此为低概率安全网。
SYNTH_REFUSED_PHRASE = "知识库中未找到相关信息"

# 兜底路径（未命中时）：把相似度落在 [FALLBACK_CONTEXT_MIN_SIM, SIMILARITY_THRESHOLD)
# 的 top-k 片段作为「临界片段」注入 prompt，让模型有据可依，而不是凭参数知识作答。
# 依据：`_llm_fallback` 原先完全不携带检索结果，是全局最容易编造费率/时限/制度条款的
# 地方；且阈值上调到 0.65 后，更多问题会走到这条路径，风险被放大。
# 最高相似度连 FALLBACK_CONTEXT_MIN_SIM 都达不到 → 直接回「知识库暂无相关内容」，
# 不调用 LLM（既省成本，也杜绝无据编造）。
# 取值 0.4：远低于领域外闲聊实测 top1（0.6235）与正确样本最低 top1（0.6483），
# 只用来圈出「连沾边都算不上」的检索噪声，不做相关性判断（相关性由阈值 0.65 负责）。
FALLBACK_CONTEXT_MIN_SIM = 0.4
# 注入片段数上限：控制 token 膨胀（每片段约 200~900 字，3 个已足够提供线索）。
FALLBACK_CONTEXT_MAX_CHUNKS = 3

# ---- Chroma 配置 ----
CHROMA_COLLECTION = "second_hand_kb"
CHROMA_ADD_BATCH = 4096  # Chroma 单次 add 上限余量

# ---- canonical 子目录 → 一级类目（对齐 B 项目 CANONICAL_SUBDIRS）----
CANONICAL_SUBDIRS: dict[str, str] = {
    "平台规则制度": "A_rule",
    "买家指南": "B_guide",
    "卖家指南": "C_guide",
    "验货质检手册": "D_inspection",
    "售后仲裁SOP": "E_sop",
    "真实场景案例库": "F_case",
    "公告与课程": "G_news",
}
