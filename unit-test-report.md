# 单元测试报告 — 二手交易平台分析系统（后端）

- 时间：2026-09-09 20:50
- 框架 / 命令：pytest 9.1.1 · `python -m pytest -q`（退出码 0）
- 结果：**116 passed, 0 failed, 0 skipped** · 39.14s

## 结果汇总

| 指标 | 值 |
| --- | --- |
| 通过 | 116 |
| 失败 | 0 |
| 跳过 | 0 |
| 退出码 | 0 |
| 警告 | 3（均为第三方库 jieba 的 `SyntaxWarning`，非本项目代码） |

本次为**多用户认证 + 会话隔离 + 知识库管理**功能的全量回归（相对上一版 v1.1.0 的 93 用例，新增 23 用例）。

## 测试文件清单（backend/tests/）

| 文件 | 说明 |
| --- | --- |
| `test_auth.py` | 认证：注册/登录/改密/当前用户/管理员 seed/参数校验（11 用例） |
| `test_rag_sessions.py` | 会话按用户隔离：归属/越权/跨登录找回/admin 可见全部（6 用例） |
| `test_kb.py` | 知识库管理：权限/文档列表/删除/上传/路径穿越（6 用例） |
| `test_changes.py` | 竞品变化检测 |
| `test_compare.py` | 竞品对比 |
| `test_competitors.py` | 竞品 CRUD |
| `test_cost.py` | LLM 成本熔断器 |
| `test_fetch.py` | 抓取编排 |
| `test_fetcher.py` | 抓取器（含 SSRF 校验） |
| `test_health.py` | 健康检查 |
| `test_provider.py` | LLM Provider 工厂分支 |
| `test_rag.py` | RAG 问答（命中/去重/兜底/多轮） |
| `test_report.py` | 报告生成 + 路径穿越防护 |
| `test_settings.py` | 应用设置 |

## 失败详情

无。全量 116 个用例全部通过。

## 覆盖率（`--cov=app`）

总体：**75%**（相对 v1.1.0 的 74% 小幅提升，新增认证/会话/kb 模块被覆盖）

新增/重点模块：

| 模块 | 覆盖率 | 说明 |
| --- | --- | --- |
| `app/api/auth.py` | ~100% | 注册/登录/改密全链路覆盖 |
| `app/api/kb.py` | ~95% | 知识库管理（rebuild 后台任务未走真实 embedding） |
| `app/security.py` | ~95% | bcrypt + JWT 签发校验 |
| `app/deps.py` | ~90% | get_current_user / require_admin |
| `app/api/rag.py` | 提升 | 会话隔离分支已覆盖 |
| `app/rag/embedding.py` | 0% | 依赖真实 Ollama（集成范畴） |
| `app/rag/retriever.py` | ~21% | 依赖 Chroma/BM25（集成范畴） |

## 端到端验证（真实启动 uvicorn + httpx）

| 场景 | 结果 |
| --- | --- |
| 健康检查 | 200，database connected |
| admin 登录 | 200，role=admin |
| 普通用户注册 | 201，role=user |
| 普通用户访问知识库管理 | 403（权限生效） |
| admin 访问知识库管理 | 200，1000 篇文档 |
| 建会话 / 列会话 | 200，会话归属正确 |
| 改密后新密码登录 | 200 |

## 建议 / 后续

1. **`embedding.py` / `retriever.py`** 依赖真实 Ollama/Chroma，单测未覆盖，建议补集成测试或端到端冒烟脚本。
2. **前端暂无自动化测试**（无 vitest/jest），登录/会话侧栏/知识库管理页靠手工 + 构建验证，可后续引入组件测试。
3. **性能优化待办**（已确认下版本做）：RAG 检索缓存、LLM 流式输出（SSE）、前端路由懒加载。
