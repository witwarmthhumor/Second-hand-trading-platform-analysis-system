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
SIMILARITY_THRESHOLD = 0.55  # 命中阈值：最高向量相似度(cosine 0~1)低于此视为未命中，走 LLM 兜底。
# rerank 精排：RRF 融合后按向量相似度二次排序（压掉 BM25 纯关键词误命中噪声）。
# RAGAS 实验（scripts/experiments/ragas_eval）验证：开启后 Hit@5 0.9825→1.0000、
# MRR@10 0.9186→0.9506、nDCG@10 0.7960→0.8280。代价是 chunk 级 context_precision
# 在多文档问题上略降（0.7414→0.7370），但 doc 级指标全面大幅提升，净收益明确。
RERANK_BY_VSIM = True
# 实测：相关查询约 0.70~0.74，无关查询约 0.48。注意不能用 RRF 得分(量级仅 0.016~0.033)。

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
