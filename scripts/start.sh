#!/usr/bin/env bash
# 统一启动脚本：一键拉起 PostgreSQL + 后端 + 前端。
# 在 Git Bash 下运行：`bash scripts/start.sh`（或 `./scripts/start.sh`）。
#
# 依赖前置：
#   - Docker Desktop（运行 postgres:16-alpine，容器名 competitor-analysis-db）
#   - 宿主机 Ollama（bge-m3，监听 127.0.0.1:11434，供 RAG 检索用）
#   - backend/.env（数据库与 LLM 配置，见 backend/app/config.py）
#   - 向量库 rag_data/vector_store（首次运行需先 `bash scripts/build_vector_store.sh`）
set -euo pipefail

# 项目根目录（脚本在 scripts/ 下，上一级即根目录）
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run-app"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

PYTHON="$BACKEND_DIR/.venv/Scripts/python.exe"
BACKEND_PORT=8000
FRONTEND_PORT=5173

mkdir -p "$RUN_DIR"

log() { printf '\033[1;32m[start]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# 端口是否被监听
port_listening() { netstat -ano 2>/dev/null | grep ":$1 " | grep -q LISTENING; }

# ---------------------------------------------------------------------------
# 1. PostgreSQL（Docker）
# ---------------------------------------------------------------------------
log "检查 PostgreSQL 容器 ..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'competitor-analysis-db'; then
    log "PostgreSQL 容器已运行"
elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'competitor-analysis-db'; then
    log "启动已存在的 PostgreSQL 容器 ..."
    docker start competitor-analysis-db >/dev/null
else
    log "首次启动：docker compose up -d db"
    docker compose -f "$ROOT_DIR/docker-compose.yml" up -d db
fi

# ---------------------------------------------------------------------------
# 2. 向量库存在性检查
# ---------------------------------------------------------------------------
VECTOR_STORE="$ROOT_DIR/rag_data/vector_store"
if [ ! -d "$VECTOR_STORE" ] || [ -z "$(ls -A "$VECTOR_STORE" 2>/dev/null)" ]; then
    warn "未检测到向量库 rag_data/vector_store，RAG 问答将不可用。"
    warn "请先执行：bash scripts/build_vector_store.sh（首次约 5 分钟）"
fi

# ---------------------------------------------------------------------------
# 3. 后端（FastAPI）
# ---------------------------------------------------------------------------
if port_listening "$BACKEND_PORT"; then
    log "后端已在 $BACKEND_PORT 端口运行"
else
    [ -x "$PYTHON" ] || die "未找到后端 venv：$PYTHON（请先在 backend/ 建 venv 并安装依赖）"
    log "启动后端（FastAPI，端口 $BACKEND_PORT）..."
    (cd "$BACKEND_DIR" && "$PYTHON" -m uvicorn app.main:app --port "$BACKEND_PORT" \
        >"$RUN_DIR/backend.log" 2>&1 &
     echo $! >"$RUN_DIR/backend.pid")
    log "后端已启动，pid=$(cat "$RUN_DIR/backend.pid")，日志见 .run-app/backend.log"
fi

# ---------------------------------------------------------------------------
# 4. 前端（Vite）
# ---------------------------------------------------------------------------
if port_listening "$FRONTEND_PORT"; then
    log "前端已在 $FRONTEND_PORT 端口运行"
else
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        warn "未检测到 node_modules，正在 npm install（首次较慢）..."
        (cd "$FRONTEND_DIR" && npm install)
    fi
    log "启动前端（Vite，端口 $FRONTEND_PORT）..."
    (cd "$FRONTEND_DIR" && npm run dev \
        >"$RUN_DIR/frontend.log" 2>&1 &
     echo $! >"$RUN_DIR/frontend.pid")
    log "前端已启动，pid=$(cat "$RUN_DIR/frontend.pid")，日志见 .run-app/frontend.log"
fi

# ---------------------------------------------------------------------------
# 5. 汇总
# ---------------------------------------------------------------------------
log "======================================================"
log "启动完成："
log "  后端  http://127.0.0.1:$BACKEND_PORT  （健康检查 /health）"
log "  前端  http://127.0.0.1:$FRONTEND_PORT"
log "  停止  bash scripts/stop.sh"
log "======================================================"
