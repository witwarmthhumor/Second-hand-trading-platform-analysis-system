---
name: run-app
description: 启动、检查或停止「二手交易平台分析系统（融合RAG）」的本地开发环境。用户说"跑起来项目""启动项目""打开网站""运行应用"、输入 /run-app，或需要启动 Docker Postgres、Ollama(bge-m3)、FastAPI 后端和 Vue/Vite 前端时使用。默认执行 start。
---

# Run App

按项目约定管理本地开发环境：Docker Postgres + 宿主机 Ollama(bge-m3) → FastAPI → Vue/Vite。这个技能只适用于 `E:\AIDevelop\AIProject\Agent`。

## 用法

- `/run-app` 或 `/run-app start`：启动全部服务（Postgres + 后端 8000 + 前端 5173），并检查健康状态
- `/run-app status`：只检查 Docker、Postgres、Ollama、后端 8000、前端 5173
- `/run-app stop`：停止本技能启动的后端和前端进程，并停止 Postgres 容器（不删数据）

## 项目定位（重要）

当前项目是「二手交易平台分析系统（融合RAG）」，由早期「古玩·近代钱币竞品分析系统」改造而来。Docker 容器名、数据库名仍沿用 `competitor-analysis-db` / `competitor`，这是历史命名，不要因此误判项目方向。

## 执行规则

1. 先确认当前工作区是 `E:\AIDevelop\AIProject\Agent`；如果不是，命令中使用这个绝对路径。
2. 执行每条命令前，用一句大白话说明它做什么、会动什么、有没有风险。这是项目 `AGENTS.md` 的铁律，不要省略。
3. `start` 的固定顺序为：
   - 检查/启动 Docker Desktop。Windows 本机安装路径通常是 `C:\Users\ZhuanZ\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`；若该路径不存在，先报告，不要猜测其他应用。
   - 在项目根目录执行 `docker compose up -d db`，等待 `competitor-analysis-db` 健康。
   - 检查向量库 `rag_data/vector_store/` 是否存在；若缺失或为空，提示先执行 `bash scripts/build_vector_store.sh`（否则 RAG 问答不可用），但不要因此中断启动。
   - 后台启动后端 `backend/.venv/Scripts/python -m uvicorn app.main:app --port 8000`。
   - 后台启动前端 `npm run dev`（`frontend/`，若缺 `node_modules` 先 `npm install`）。
   - 检查 `http://localhost:8000/health` 和 `http://localhost:5173/`；成功后返回网站地址和各项状态。
4. 启动前先检查端口 8000 和 5173：
   - 若已有本项目旧的 Python/Node 开发进程，占用端口时可结束旧进程后重启，并说明这是清理残留开发进程。
   - 若占用者不是本项目开发进程，不要强杀；报告端口冲突和占用 PID。
5. Vite 在 Windows 上可能只监听 IPv6 的 `::1`；浏览器仍使用 `http://localhost:5173`，不要因 `127.0.0.1` curl 失败就判定前端没启动。
6. 不要打印、读取或提交 `backend/.env` 里的数据库密码或 API Key。只报告"配置已存在/未配置"。
7. `stop` 只停止本技能记录的开发进程和 `docker compose stop db`，不要删除容器、卷、上传文件或数据库。
8. 如果某一步失败，保留原始错误摘要，说明失败在哪一步和下一条建议命令；不要伪报成功。

## 关键依赖与环境

- **PostgreSQL**：Docker 容器 `competitor-analysis-db`（postgres:16-alpine），映射宿主机 **5433**（5432 被其他项目占用）。后端经 `backend/.env` 的 `DATABASE_URL` 连接，端口已配好。
- **Ollama**：RAG 检索依赖宿主机 Ollama，embedding 模型 `bge-m3` 监听 `127.0.0.1:11434`（代码硬编码，不能用 localhost 避免命中 Docker 内的 ollama 容器）。`status` 时应顺带检查 `http://127.0.0.1:11434/api/tags` 是否返回 bge-m3。
- **向量库**：`rag_data/vector_store/`（已 `.gitignore` 忽略），首次运行前需 `bash scripts/build_vector_store.sh` 建库。

## 推荐实现

优先执行项目自带的统一启动/停止脚本（它们已覆盖上述依赖与检查）：

```bash
cd /e/AIDevelop/AIProject/Agent
bash scripts/start.sh     # 启动 Postgres + 后端 + 前端
bash scripts/stop.sh      # 停止前后端（保留 Postgres 容器）
```

脚本会在 `.run-app/` 保存本地 PID 和日志；该目录是运行时状态，不应进入 Git。若脚本不存在或需要逐步诊断，按上面的固定顺序手动执行。

`status` 脚本可复用本技能自带脚本：

```bash
bash .workbuddy/skills/run-app/scripts/run-app.sh status
```

完成后报告：

```text
项目已启动
网站：http://localhost:5173
后端：运行中（http://localhost:8000/health）
数据库：已连接（competitor-analysis-db healthy，端口 5433）
RAG：向量库就绪，Ollama bge-m3 可用（或提示缺失）
```

不要自动打开浏览器或代替用户进行验收，除非用户明确要求"打开网站"。
