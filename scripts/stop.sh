#!/usr/bin/env bash
# 统一停止脚本：停止前端 + 后端（不停止 PostgreSQL 容器，保留数据）。
# 在 Git Bash 下运行：`bash scripts/stop.sh`。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run-app"

log() { printf '\033[1;32m[stop]\033[0m %s\n' "$*"; }

# 按端口杀进程（后端 8000 / 前端 5173）
kill_by_port() {
    local port="$1" name="$2"
    local pids
    pids="$(netstat -ano 2>/dev/null | grep ":$port " | grep LISTENING | awk '{print $NF}' | sort -u)"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            log "停止 $name（pid=$pid）..."
            # 用 MSYS_NO_PATHCONV 防止 Git Bash 把 /F /PID 当路径转换
            MSYS_NO_PATHCONV=1 taskkill /F /PID "$pid" >/dev/null 2>&1 || true
        done
    else
        log "$name 未在 $port 端口运行"
    fi
}

kill_by_port 8000 "后端"
kill_by_port 5173 "前端"

# 清理 pid 文件
rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/frontend.pid" 2>/dev/null || true

log "已停止前后端服务（PostgreSQL 容器保留运行，如需停止：docker stop competitor-analysis-db）"
