#!/usr/bin/env bash
set -u

# ROOT：本脚本位于 .workbuddy/skills/run-app/scripts/，向上 4 级即项目根
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
STATE_DIR="$ROOT/.run-app"
mkdir -p "$STATE_DIR"

BACKEND_PID_FILE="$STATE_DIR/backend.pid"
FRONTEND_PID_FILE="$STATE_DIR/frontend.pid"
BACKEND_LOG="$STATE_DIR/backend.log"
FRONTEND_LOG="$STATE_DIR/frontend.log"

port_pid() {
  local port="$1"
  # netstat 行形如 "TCP 127.0.0.1:8000 ... LISTENING 2580"，端口后是空格+地址/PID，
  # 故匹配 ":端口 空格" 而非行尾
  netstat -ano 2>/dev/null | grep -E ":$port " | grep LISTENING | awk '{print $NF}' | head -1
}

wait_for_url() {
  local url="$1"
  local attempts="${2:-30}"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

start_docker() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi

  local docker_desktop="/c/Users/ZhuanZ/AppData/Local/Programs/DockerDesktop/Docker Desktop.exe"
  if [[ ! -f "$docker_desktop" ]]; then
    echo "ERROR: Docker 守护进程未运行，且未找到默认安装路径：$docker_desktop" >&2
    return 1
  fi

  powershell.exe -NoProfile -Command "Start-Process -FilePath 'C:\Users\ZhuanZ\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe'" >/dev/null 2>&1
  for _ in $(seq 1 36); do
    sleep 5
    if docker info >/dev/null 2>&1; then
      return 0
    fi
  done
  echo "ERROR: Docker Desktop 启动超时。" >&2
  return 1
}

start_app() {
  echo "[1/5] 检查并启动 Docker Desktop（只启动软件，不改项目文件）"
  start_docker || return 1

  echo "[2/5] 启动 Postgres 容器并等待健康检查（不删除具名卷）"
  (cd "$ROOT" && docker compose up -d db) || return 1
  local status=""
  for _ in $(seq 1 24); do
    status="$(cd "$ROOT" && docker compose ps -q db | xargs -r docker inspect --format '{{.State.Health.Status}}' 2>/dev/null)"
    if [[ "$status" == "healthy" ]]; then
      break
    fi
    sleep 3
  done
  [[ "$status" == "healthy" ]] || { echo "ERROR: Postgres 未达到 healthy。" >&2; return 1; }

  echo "[3/5] 检查向量库与 Ollama(bge-m3)（缺失仅提示，不中断启动）"
  local vector_store="$ROOT/rag_data/vector_store"
  if [[ ! -d "$vector_store" ]] || [[ -z "$(ls -A "$vector_store" 2>/dev/null)" ]]; then
    echo "WARN: 未检测到向量库 rag_data/vector_store，RAG 问答将不可用。"
    echo "      请先执行：bash scripts/build_vector_store.sh（首次约 5 分钟）"
  fi
  if ! curl -fsS "http://127.0.0.1:11434/api/tags" 2>/dev/null | grep -q "bge-m3"; then
    echo "WARN: Ollama 未就绪或缺少 bge-m3 模型，RAG 检索将不可用。"
    echo "      请确认宿主机 Ollama 运行，并已 ollama pull bge-m3。"
  fi

  echo "[4/5] 启动 FastAPI 后端（8000；日志写入 .run-app/backend.log）"
  old_pid="$(port_pid 8000)"
  if [[ -n "$old_pid" ]]; then
    echo "后端已在 8000 端口运行（pid=$old_pid），跳过启动"
  else
    (cd "$ROOT/backend" && nohup .venv/Scripts/python -m uvicorn app.main:app --port 8000 >"$BACKEND_LOG" 2>&1 & echo $! >"$BACKEND_PID_FILE")
  fi
  wait_for_url "http://localhost:8000/health" || { echo "ERROR: 后端未通过健康检查，查看 $BACKEND_LOG" >&2; return 1; }

  echo "[5/5] 启动 Vue/Vite 前端（5173；日志写入 .run-app/frontend.log）"
  old_pid="$(port_pid 5173)"
  if [[ -n "$old_pid" ]]; then
    echo "前端已在 5173 端口运行（pid=$old_pid），跳过启动"
  else
    if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
      echo "未检测到 node_modules，先执行 npm install（首次较慢）..."
      (cd "$ROOT/frontend" && npm install) || return 1
    fi
    (cd "$ROOT/frontend" && nohup npm run dev >"$FRONTEND_LOG" 2>&1 & echo $! >"$FRONTEND_PID_FILE")
  fi
  wait_for_url "http://localhost:5173/" || { echo "ERROR: 前端未通过检查，查看 $FRONTEND_LOG" >&2; return 1; }

  echo "项目已启动"
  echo "网站：http://localhost:5173"
  echo "后端：运行中（http://localhost:8000/health）"
  echo "数据库：已连接（competitor-analysis-db healthy，端口 5433）"
  echo "RAG：$(curl -fsS "http://127.0.0.1:11434/api/tags" 2>/dev/null | grep -q bge-m3 && echo '向量库/模型就绪' || echo '请检查向量库与 bge-m3')"
}

status_app() {
  echo "项目目录：$ROOT"
  echo "Docker：$(docker info >/dev/null 2>&1 && echo 运行中 || echo 未运行)"
  echo "数据库：$(cd "$ROOT" && docker compose ps --status running --services 2>/dev/null | grep -qx db && echo 运行中 || echo 未运行)"
  echo "后端：$(wait_for_url "http://localhost:8000/health" 1 && echo 运行中 || echo 未运行)"
  echo "前端：$(wait_for_url "http://localhost:5173/" 1 && echo 运行中 || echo 未运行)"
  echo "Ollama：$(curl -fsS "http://127.0.0.1:11434/api/tags" 2>/dev/null | grep -q bge-m3 && echo 'bge-m3 可用' || echo '未就绪或缺少 bge-m3')"
  local vector_store="$ROOT/rag_data/vector_store"
  echo "向量库：$([[ -d "$vector_store" ]] && [[ -n "$(ls -A "$vector_store" 2>/dev/null)" ]] && echo 已就绪 || echo 缺失/为空)"
}

kill_by_port() {
  # 按端口找监听 PID 再杀（Git Bash 下 nohup 记录的 $! 是 bash 子进程 pid，
  # 与 Windows 实际监听端口的 PID 不一致，按端口定位才可靠）
  local port="$1" name="$2"
  local pids
  pids="$(netstat -ano 2>/dev/null | grep -E ":$port " | grep LISTENING | awk '{print $NF}' | sort -u)"
  if [[ -n "$pids" ]]; then
    for pid in $pids; do
      echo "停止 $name（pid=$pid）..."
      # MSYS_NO_PATHCONV 防止 Git Bash 把 /F /PID 当路径转换
      MSYS_NO_PATHCONV=1 taskkill /F /PID "$pid" >/dev/null 2>&1 || true
    done
  else
    echo "$name 未在 $port 端口运行"
  fi
}

stop_app() {
  echo "停止本技能记录的开发进程（不删除数据库和上传文件）"
  kill_by_port 8000 "后端"
  kill_by_port 5173 "前端"
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"
  (cd "$ROOT" && docker compose stop db) >/dev/null 2>&1 || true
  echo "已停止 Postgres 容器（数据仍保留在具名卷 pgdata）"
}

case "${1:-start}" in
  start) start_app ;;
  status) status_app ;;
  stop) stop_app ;;
  *) echo "用法：$0 [start|status|stop]" >&2; exit 2 ;;
esac
