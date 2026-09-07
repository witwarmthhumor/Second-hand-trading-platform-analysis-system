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

read_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then
    tr -d '\r\n ' < "$file"
  fi
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && tasklist //FI "PID eq $pid" 2>/dev/null | grep -q "$pid"
}

port_pid() {
  local port="$1"
  netstat -ano 2>/dev/null | grep -E "LISTENING.*[ :]$port$" | awk '{print $NF}' | head -1
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
  echo "[1/4] 检查并启动 Docker Desktop（只启动软件，不改项目文件）"
  start_docker || return 1

  echo "[2/4] 启动 Postgres 容器并等待健康检查（不删除具名卷）"
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

  echo "[3/4] 启动 FastAPI 后端（8000；日志写入 .run-app/backend.log）"
  backend_pid="$(read_pid "$BACKEND_PID_FILE")"
  if ! is_running "$backend_pid"; then
    old_pid="$(port_pid 8000)"
    if [[ -n "$old_pid" ]]; then
      echo "ERROR: 8000 已被 PID $old_pid 占用，请确认不是其他程序后再处理。" >&2
      return 1
    fi
    (cd "$ROOT/backend" && nohup .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000 >"$BACKEND_LOG" 2>&1 & echo $! >"$BACKEND_PID_FILE")
  fi
  wait_for_url "http://localhost:8000/health" || { echo "ERROR: 后端未通过健康检查，查看 $BACKEND_LOG" >&2; return 1; }

  echo "[4/4] 启动 Vue/Vite 前端（5173；日志写入 .run-app/frontend.log）"
  frontend_pid="$(read_pid "$FRONTEND_PID_FILE")"
  if ! is_running "$frontend_pid"; then
    old_pid="$(port_pid 5173)"
    if [[ -n "$old_pid" ]]; then
      echo "ERROR: 5173 已被 PID $old_pid 占用，请确认不是其他程序后再处理。" >&2
      return 1
    fi
    (cd "$ROOT/frontend" && nohup npm run dev >"$FRONTEND_LOG" 2>&1 & echo $! >"$FRONTEND_PID_FILE")
  fi
  wait_for_url "http://localhost:5173/" || { echo "ERROR: 前端未通过检查，查看 $FRONTEND_LOG" >&2; return 1; }

  echo "项目已启动"
  echo "网站：http://localhost:5173"
  echo "后端：运行中（http://localhost:8000/health）"
  echo "数据库：已连接（competitor-analysis-db healthy，端口 5433）"
}

status_app() {
  echo "项目目录：$ROOT"
  echo "Docker：$(docker info >/dev/null 2>&1 && echo 运行中 || echo 未运行)"
  echo "数据库：$(cd "$ROOT" && docker compose ps --status running --services 2>/dev/null | grep -qx db && echo 运行中 || echo 未运行)"
  echo "后端：$(wait_for_url "http://localhost:8000/health" 1 && echo 运行中 || echo 未运行)"
  echo "前端：$(wait_for_url "http://localhost:5173/" 1 && echo 运行中 || echo 未运行)"
}

stop_app() {
  echo "停止本技能记录的开发进程（不删除数据库和上传文件）"
  for file in "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"; do
    pid="$(read_pid "$file")"
    if is_running "$pid"; then
      taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
      echo "已停止 PID $pid"
    fi
    rm -f "$file"
  done
  (cd "$ROOT" && docker compose stop db) >/dev/null 2>&1 || true
  echo "已停止 Postgres 容器（数据仍保留在具名卷 pgdata）"
}

case "${1:-start}" in
  start) start_app ;;
  status) status_app ;;
  stop) stop_app ;;
  *) echo "用法：$0 [start|status|stop]" >&2; exit 2 ;;
esac
