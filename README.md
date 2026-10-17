# 二手交易平台分析系统

竞品分析 + 领域知识库问答（RAG）系统。

- **竞品情报链路**：输入网址 → 抓取 → LLM 结构化提炼 → 待校对 → 人工确认入库 → 多竞品对比 / 变化追踪
- **知识库问答链路**：920 篇领域文档 + 80 份 FAQ → 混合检索（向量 + BM25 + RRF）→ 直出 / 合成 / 兜底三态回答，引用来源可点验

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · Alembic |
| 存储 | PostgreSQL 16 · Redis 7 |
| 检索 | Chroma 向量库 + BM25（jieba）+ RRF 融合 |
| Embedding | 本地 Ollama `bge-m3`（1024 维） |
| 生成 | OpenAI 兼容协议（可切换模型） |
| 采集 | BeautifulSoup4 · APScheduler |
| 前端 | Vue 3 · Vite · Element Plus |
| 部署 | Docker Compose |

## 快速开始

### 0. 前置

- Python 3.11+
- Node.js 18+
- Docker Desktop
- [Ollama](https://ollama.com/)（本地 embedding，需 `bge-m3`）
- 一个 OpenAI 兼容的大模型 API Key（抓取提炼与兜底问答使用）

### 1. 启动依赖

```bash
docker compose up -d     # PostgreSQL :5433 / Redis :6380
ollama pull bge-m3       # 约 1.2GB
```

### 2. 启动后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -e .
cp .env.example .env          # 按注释填写数据库 / Redis / LLM 配置
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### 3. 首次建向量库

```bash
# 项目根目录执行；对 920 篇语料 + 80 份 FAQ 做切块与 embedding（约 5 分钟）
bash scripts/build_vector_store.sh
```

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

### 5. 运行测试

```bash
cd backend
python -m pytest tests/ --cov=app
```

> 测试默认 mock 检索器 / LLM / 抓取器，不产生外网请求与模型费用。

## 目录结构

```
backend/
  app/
    api/         # 路由层
    services/    # 业务服务
    rag/         # 切块 / 检索 / 重排 / 缓存
    crawler/     # 抓取与站点解析
    models/      # ORM 模型
  alembic/       # 数据库迁移
  tests/         # pytest
frontend/
  src/           # Vue 3 组件与状态
rag_data/
  canonical/     # 920 篇领域语料（Markdown，7 类）
  faq_source/    # 80 份结构化 FAQ
  facts/         # 同义词 / 平台 / 分级事实底座
  eval/          # 285 问黄金评测集
scripts/         # 建库 / 评测 / 模型下载
docs/            # 产品、技术、实施与评测文档
```

## 说明

- 语料为**虚构演示数据**（费率、时限、制度编号均为示例），不指向任何真实平台。
- 检索评测：285 问黄金集，Hit@5 0.9825 / MRR@10 0.9186 / nDCG@10 0.7960，详见 `docs/检索评测报告.md`。
- Reranker（cross-encoder）代码已实现但**默认关闭**：效果达标、延迟不达标，见 `docs/Rerank方案.md`。
