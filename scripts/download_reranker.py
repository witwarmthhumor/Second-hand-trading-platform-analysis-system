"""下载 cross-encoder reranker（bge-reranker-v2-m3，ONNX int8）到本地仓库。

为什么要写成脚本，而不是手工下：

1. **HF 直连在本机不通**（实测 huggingface.co 超时、hf-mirror.com 正常），必须显式走镜像。
   `HF_ENDPOINT` 只在 `import huggingface_hub` 之前生效，所以这里在 import 前强制覆盖它，
   同时把 `endpoint=` 显式传给 `hf_hub_download`（环境变量已被别处 import 时靠不住）。
2. **同一个仓库里有容易拿错的文件**：`onnx/model.onnx` 只有 656KB，是 fp32 权重的
   external-data **stub**（真正的 2.3GB 在 `onnx/model.onnx_data` 里，且不在本次下载清单内）。
   拿错要等 `InferenceSession` 加载时才炸，报错指向「模型损坏」而不是「拿错文件」。
   要的是自包含的 `onnx/model_int8.onnx`（570MB）—— 所以脚本按下限校验体积。

模型落点：`backend/models/bge-reranker-v2-m3/`（文件平铺，已在 `.gitignore` 中，不入版本库）。

用法（仓库根目录，用 backend 的 venv）：

    backend/.venv/Scripts/python.exe scripts/download_reranker.py

可重复执行：文件已存在且体积达标即跳过；中途失败重跑由 `hf_hub_download` 断点续传。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 必须在 import huggingface_hub 之前设置（huggingface_hub.constants 在 import 期读这个变量）。
# 这里用赋值而不是 setdefault：本机 HF 直连超时，宁可用户的 HF_ENDPOINT 被覆盖也不要卡死。
HF_ENDPOINT = "https://hf-mirror.com"
os.environ["HF_ENDPOINT"] = HF_ENDPOINT

from huggingface_hub import hf_hub_download  # noqa: E402

REPO_ID = "onnx-community/bge-reranker-v2-m3-ONNX"
MODEL_DIR = Path(__file__).resolve().parents[1] / "backend" / "models" / "bge-reranker-v2-m3"

# (仓库内路径, 落地后的平铺文件名, 体积下限字节)
# 体积下限是防「拿错文件」的闸门，不是装饰：656KB 的 onnx/model.onnx 过不了 500MB 这道线。
FILES: tuple[tuple[str, str, int], ...] = (
    ("onnx/model_int8.onnx", "model_int8.onnx", 500 * 1024 * 1024),
    ("tokenizer.json", "tokenizer.json", 1 * 1024 * 1024),
    ("config.json", "config.json", 100),
    ("tokenizer_config.json", "tokenizer_config.json", 100),
    ("special_tokens_map.json", "special_tokens_map.json", 100),
)


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def _download_one(repo_filename: str, dest: Path, min_bytes: int) -> str | None:
    """把仓库内单个文件下载到 ``dest``（已存在且体积达标则跳过）。

    Returns:
        失败时返回可读原因字符串；成功返回 ``None``。
    """
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  跳过 {dest.name}（已存在，{_mb(dest.stat().st_size)}）")
        return None

    if dest.exists():
        print(
            f"  重新下载 {dest.name}：现有文件只有 {_mb(dest.stat().st_size)}，"
            f"低于下限 {_mb(min_bytes)}"
        )
        dest.unlink()

    try:
        downloaded = hf_hub_download(
            repo_id=REPO_ID,
            filename=repo_filename,
            local_dir=str(MODEL_DIR),
            endpoint=HF_ENDPOINT,
        )
    except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/xet/磁盘各类异常都转成可读原因
        return f"下载 {repo_filename} 失败（endpoint={HF_ENDPOINT}）：{type(exc).__name__}: {exc}"

    src = Path(downloaded)
    try:
        if src != dest:
            # hf_hub_download 会保留仓库内的目录层级（onnx/model_int8.onnx），
            # 这里挪成平铺，让 reranker 模块只认一个目录。
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
    except OSError as exc:
        return f"落地 {dest} 失败：{type(exc).__name__}: {exc}"

    size = dest.stat().st_size
    if size < min_bytes:
        return (
            f"{dest.name} 体积异常：{size} 字节 < 下限 {min_bytes} 字节。"
            f"多半是拿错了文件——onnx/model.onnx 是 656KB 的 fp32 外置权重 stub，"
            f"要的是自包含的 onnx/model_int8.onnx。"
        )
    print(f"  完成 {dest.name}（{_mb(size)}）")
    return None


def main() -> int:
    print(f"[download_reranker] repo: {REPO_ID}")
    print(f"[download_reranker] endpoint: {HF_ENDPOINT}")
    print(f"[download_reranker] 落点: {MODEL_DIR}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    for repo_filename, local_name, min_bytes in FILES:
        error = _download_one(repo_filename, MODEL_DIR / local_name, min_bytes)
        if error is not None:
            print(f"[download_reranker] 失败：{error}", file=sys.stderr)
            print(
                "[download_reranker] 已完成的文件会保留，检查网络/镜像后重跑即可断点续传",
                file=sys.stderr,
            )
            return 1

    # 平铺落地后，onnx/ 会剩一个空目录，顺手清掉
    onnx_dir = MODEL_DIR / "onnx"
    if onnx_dir.is_dir() and not any(onnx_dir.iterdir()):
        onnx_dir.rmdir()

    total = sum((MODEL_DIR / local_name).stat().st_size for _, local_name, _ in FILES)
    print(f"[download_reranker] 全部就绪，共 {_mb(total)}：{MODEL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
