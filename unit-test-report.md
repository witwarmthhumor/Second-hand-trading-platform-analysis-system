# 单元测试报告 — 二手交易平台分析系统（后端）

- 时间：2026-09-08 22:41
- 框架 / 命令：pytest 9.1.1 · `python -m pytest --cov=app`（退出码 0）
- 结果：**69 passed, 0 failed, 0 skipped** · 60.2s

## 本次新增 / 修改

针对上一轮安全修复后暴露的测试缺口，补充了 `rag_service` 问答服务的命中与兜底分支用例：

| 文件 | 变更 | 用例数 |
| --- | --- | --- |
| `backend/tests/test_rag.py` | 新增 `_ReadyRetriever` mock 类 + 5 个用例（命中直出、命中去重、未命中兜底、空结果兜底、LLM 超时降级、成本熔断提示） | +5 |

原有测试 `test_fetcher.py`（19 个 SSRF 校验用例，上一轮新增）与 `test_cost.py`（approve 行为）均保持绿色。

## 失败详情

无。全量 69 个用例全部通过。

## 覆盖率（`--cov=app`）

总体：**70%**（1107 语句，覆盖 771）

| 模块 | 覆盖率 | 说明 |
| --- | --- | --- |
| `app/ai/cost.py` | 93% | 成本熔断器，核心已覆盖 |
| `app/services/report_service.py` | 95% | 报告生成与路径穿越防护 |
| `app/services/fetch_service.py` | 91% | 抓取提炼编排 |
| `app/services/rag_service.py` | 84% | 命中判定/去重/兜底（本次补齐） |
| `app/api/cost.py` | 100% | 审批接口 |
| `app/schemas/*` / `app/models/*` | 100% | 数据模型 |
| `app/rag/chunking.py` | 63% | 分块逻辑（表格拆块等边界未覆盖） |
| `app/rag/retriever.py` | 21% | 依赖真实 Chroma/BM25/Ollama |
| `app/rag/embedding.py` | 0% | 依赖真实 Ollama |
| `app/ai/provider.py` | 32% | LLM 调用 |

**低覆盖模块说明**：`embedding.py`、`retriever.py`、`provider.py` 依赖真实 Ollama / Chroma / LLM 端点，属集成测试范畴。单测按约定应 mock 隔离外部依赖，强行 mock 掉这些反而测不到真实行为，故未在单测层覆盖，留待集成/端到端验证（已有建库脚本冒烟 + `/api/rag/ask` 端到端验证）。

## 建议 / 后续

1. **`chunking.py` 的 `_split_table_blocks`**（约 78-93 行）——表格拆块 + 表头复制逻辑未覆盖，是分块质量的关键边界，可补参数化用例。
2. **`retriever.py` 的 RRF 融合**——可用「伪 collection + 预置 embedding」的轻量桩测 RRF 排序正确性，不依赖真实 Ollama（`search` 内 `query_embedding` 可 mock）。
3. **`provider.py` 的规则兜底**（无 LLM key 时的降级路径，约 71-114 行）——可 mock 掉 httpx 后测 `extract` 的规则提炼分支。
4. **`api/rag.py` 与 `api/compare.py`** 的 HTTP 层仍有少量未覆盖行，可通过 ASGITransport（项目已有此模式）补接口级用例。

以上均属可选项；当前核心业务逻辑（成本熔断、命中判定、报告、抓取编排）已充分覆盖。
