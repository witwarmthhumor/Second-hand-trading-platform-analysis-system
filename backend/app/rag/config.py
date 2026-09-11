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
