---
name: run-app
description: 启动、检查或停止「古玩·近代钱币竞品分析系统」的本地开发环境。用户说"跑起来项目""启动项目""打开网站""运行应用"、输入 /run-app，或需要启动 Docker Postgres、FastAPI 后端和 Vue/Vite 前端时使用。默认执行 start。
---

# Run App

按项目约定管理本地开发环境：Postgres → FastAPI → Vue/Vite。这个技能只适用于 `E:\AIDevelop\AIProject\Agent`。

## 用法

- `/run-app` 或 `/run-app start`：启动全部服务，并检查健康状态
- `/run-app status`：只检查 Docker、Postgres、后端 8000、前端 5173
- `/run-app stop`：停止本技能启动的后端和前端，并停止 Postgres 容器

## 执行规则

1. 先确认当前工作区是 `E:\AIDevelop\AIProject\Agent`；如果不是，命令中使用这个绝对路径。
2. 执行每条命令前，用一句大白话说明它做什么、会动什么、有没有风险。这是项目 `AGENTS.md` 的铁律，不要省略。
3. `start` 的顺序固定为：
   - 检查/启动 Docker Desktop。Windows 本机的安装路径通常是 `C:\Users\ZhuanZ\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`；若该路径不存在，先报告，不要猜测其他应用。
   - 在项目根目录执行 `docker compose up -d db`，等待 `competitor-analysis-db` 健康。
   - 后台启动 `backend/.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000`。
   - 后台启动 `npm run dev`（`frontend/`）。
   - 检查 `http://localhost:8000/health` 和 `http://localhost:5173/`；成功后返回网站地址和三项状态。
4. 启动前先检查端口 8000 和 5173：
   - 若已有本项目旧的 Python/Node 开发进程，占用端口时可结束旧进程后重启，并说明这是清理残留开发进程。
   - 若占用者不是本项目开发进程，不要强杀；报告端口冲突和占用 PID。
5. Vite 在 Windows 上可能只监听 IPv6 的 `::1`；浏览器仍使用 `http://localhost:5173`，不要因 `127.0.0.1` curl 失败就判定前端没启动。
6. 不要打印、读取或提交 `backend/.env` 里的数据库密码或 API Key。只报告"配置已存在/未配置"。
7. `stop` 只停止本技能记录的开发进程和 `docker compose stop db`，不要删除容器、卷、上传文件或数据库。
8. 如果某一步失败，保留原始错误摘要，说明失败在哪一步和下一条建议命令；不要伪报成功。

## 关于数据库端口

本项目 PostgreSQL 映射到宿主机 **5433**（而非默认 5432，因 5432 被其他项目占用）。后端通过 `backend/.env` 的 `DATABASE_URL` 连接，端口已配好，无需在脚本中额外指定。

## 推荐实现

优先执行捆绑脚本：

```bash
cd /e/AIDevelop/AIProject/Agent
bash .workbuddy/skills/run-app/scripts/run-app.sh start
```

脚本会在 `.run-app/` 保存本地 PID 和日志；该目录是运行时状态，不应进入 Git。若脚本不存在或需要逐步诊断，按上面的固定顺序手动执行。

完成后报告：

```text
项目已启动
网站：http://localhost:5173
后端：运行中（http://localhost:8000/health）
数据库：已连接（competitor-analysis-db healthy，端口 5433）
```

不要自动打开浏览器或代替用户进行验收，除非用户明确要求"打开网站"。
