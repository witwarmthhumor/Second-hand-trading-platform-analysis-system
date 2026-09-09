# 单元测试报告 — 二手交易平台分析系统（后端）

- 时间：2026-09-09 20:06
- 框架 / 命令：pytest 9.1.1 · `python -m pytest --cov=app --cov-report=term-missing`（退出码 0）
- 结果：**93 passed, 0 failed, 0 skipped** · 13.19s（不含覆盖率）/ 63.47s（含覆盖率）

## 结果汇总

| 指标 | 值 |
| --- | --- |
| 通过 | 93 |
| 失败 | 0 |
| 跳过 | 0 |
| 退出码 | 0 |
| 警告 | 3（均为第三方库 jieba 的 `SyntaxWarning: invalid escape sequence`，非本项目代码问题） |

本次为**全量回归执行**（非新增测试）。相比上一轮（69 用例），用例数增长至 93，来源为本会话新增的 8 阶段功能测试：`test_provider.py`（10）、`test_changes.py`、`test_compare.py` 等。

## 测试文件清单（backend/tests/）

| 文件 | 说明 |
| --- | --- |
| `test_changes.py` | 竞品变化检测（diff_fields / monitor 端点） |
| `test_compare.py` | 竞品对比 |
| `test_competitors.py` | 竞品 CRUD + PUT 变化记录 |
| `test_cost.py` | LLM 成本熔断器（record/reset/persist/approve） |
| `test_fetch.py` | 抓取编排 |
| `test_fetcher.py` | 抓取器（含 19 个 SSRF 校验用例） |
| `test_health.py` | 健康检查 |
| `test_provider.py` | LLM Provider 工厂分支（10 用例） |
| `test_rag.py` | RAG 问答（命中/去重/兜底/多轮会话） |
| `test_report.py` | 报告生成 + 路径穿越防护 |
| `test_settings.py` | 应用设置 + 成本状态持久化 |

## 失败详情

无。全量 93 个用例全部通过。

## 覆盖率（`--cov=app`）

总体：**74%**（1339 语句，覆盖 992）

| 模块 | 覆盖率 | 说明 |
| --- | --- | --- |
| `app/api/cost.py` | 100% | 成本审批接口 |
| `app/config.py` / `app/db.py` | 100% | 配置与数据库连接 |
| `app/schemas/*` / `app/models/*` | 100% | 数据模型 |
| `app/services/rag_service.py` | 99% | 命中判定/去重/兜底/多轮会话 |
| `app/services/report_service.py` | 95% | 报告生成与路径穿越防护 |
| `app/services/change_service.py` | 92% | 变化检测 |
| `app/services/fetch_service.py` | 91% | 抓取提炼编排 |
| `app/ai/cost.py` | 88% | 成本熔断器 |
| `app/api/settings.py` | 77% | 设置接口 |
| `app/ai/provider.py` | 71% | LLM 调用 |
| `app/crawler/fetcher.py` | 70% | 抓取器 |
| `app/rag/chunking.py` | 63% | 分块逻辑 |
| `app/api/competitors.py` | 58% | 竞品接口 |
| `app/api/rag.py` | 41% | 问答接口 |
| `app/rag/retriever.py` | 21% | 依赖真实 Chroma/BM25/Ollama |
| `app/rag/embedding.py` | 0% | 依赖真实 Ollama |

**低覆盖模块说明**：`embedding.py`、`retriever.py` 依赖真实 Ollama / Chroma / BM25 端点，属集成测试范畴，单测按约定 mock 隔离外部依赖，强行 mock 反而测不到真实行为，留待集成/端到端验证（已有建库脚本冒烟 + `/api/rag/ask` 端到端验证）。

## 建议 / 后续

1. **`chunking.py` 的 `_split_table_blocks`**（78-93 行）——表格拆块 + 表头复制逻辑未覆盖，是分块质量的关键边界，可补参数化用例。
2. **`retriever.py` 的 RRF 融合**——可用「伪 collection + 预置 embedding」轻量桩测 RRF 排序正确性，不依赖真实 Ollama（`search` 内 `query_embedding` 可 mock）。
3. **`provider.py` 的规则兜底**（无 LLM key 时降级路径，71-114 行）——可 mock 掉 httpx 后测 `extract` 的规则提炼分支。
4. **`api/rag.py`（41%）、`api/competitors.py`（58%）** 的 HTTP 层仍有较多未覆盖行，可通过 ASGITransport（项目已有此模式）补接口级用例。
5. **jieba 的 3 个 SyntaxWarning** 来自第三方库，可在 `pyproject.toml` 加 `filterwarnings` 忽略，保持测试输出干净。

以上均属可选项；当前核心业务逻辑（成本熔断、命中判定、报告、抓取编排、变化检测）已充分覆盖。
