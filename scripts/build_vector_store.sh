#!/usr/bin/env bash
# 建库脚本封装：对 rag_data/ 语料分块 + bge-m3 向量化 + 建 Chroma 库 + BM25 索引。
# 首次约 5 分钟（7619 块 embedding）。在 Git Bash 下运行：`bash scripts/build_vector_store.sh`。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT_DIR/backend/.venv/Scripts/python.exe"

[ -x "$PYTHON" ] || { echo "未找到后端 venv：$PYTHON" >&2; exit 1; }

echo "开始建库（语料 920 MD + 80 FAQ，约 7619 块）..."
"$PYTHON" "$ROOT_DIR/scripts/build_vector_store.py"
echo "建库完成，产物位于 rag_data/vector_store/"
