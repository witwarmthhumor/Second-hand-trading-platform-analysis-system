"""``scripts/download_reranker.py`` 的离线测试（US-001）。

脚本本身要联网下 570MB 模型，所以这里**不真下载**：把 ``hf_hub_download`` 换成假实现，
只守住三件容易出错、且出事时很难排查的事：

1. 清单选的是**自包含的 int8 模型**（``onnx/model_int8.onnx``，570MB），
   不是 656KB 的 fp32 外置权重 stub（``onnx/model.onnx``）——拿错要等加载模型时才炸；
2. 已存在且体积达标的文件会被跳过（重复执行不重复下载）；
3. 下载失败 / 拿错文件（体积不足）都以**可读原因 + 非 0 退出码**收场，不是静默或裸栈。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# 脚本不在 backend 包内（scripts/ 是平铺目录），按 test_agent_eval.py 的做法挂载后导入
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "download_reranker.py"
_spec = importlib.util.spec_from_file_location("download_reranker", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
download_reranker = importlib.util.module_from_spec(_spec)
sys.modules["download_reranker"] = download_reranker
_spec.loader.exec_module(download_reranker)

# 行为用例用的小清单：真清单里的 570MB 下限没法在 tmp_path 里造（也不该造）
_FAKE_FILES = (
    ("onnx/model_int8.onnx", "model_int8.onnx", 10),
    ("tokenizer.json", "tokenizer.json", 10),
)


def test_manifest_targets_self_contained_int8_model():
    """清单必须是自包含的 int8 模型，且下限高到能把 656KB 的 stub 挡在门外。"""
    manifest = {repo_path: (local_name, min_bytes) for repo_path, local_name, min_bytes in download_reranker.FILES}

    assert set(manifest) == {
        "onnx/model_int8.onnx",
        "tokenizer.json",
        "config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    }
    # onnx/model.onnx（656KB，fp32 外置权重 stub）一旦进清单，加载模型必炸
    assert "onnx/model.onnx" not in manifest

    _, model_min_bytes = manifest["onnx/model_int8.onnx"]
    assert model_min_bytes >= 500 * 1024 * 1024

    # 落地文件名必须是平铺的：reranker 模块只认一个目录里的这 5 个名字
    assert {local_name for local_name, _ in manifest.values()} == {
        "model_int8.onnx",
        "tokenizer.json",
        "config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    }


def test_endpoint_is_mirror_before_huggingface_hub_import():
    """HF 直连不通，脚本必须在导入 huggingface_hub 之前就把 endpoint 指向镜像。"""
    assert download_reranker.HF_ENDPOINT == "https://hf-mirror.com"
    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_rerun_skips_existing_files(tmp_path, monkeypatch):
    """重复执行是常态（换机/补文件），已就绪的文件不该再走网络。"""
    monkeypatch.setattr(download_reranker, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(download_reranker, "FILES", _FAKE_FILES)
    for _, local_name, _ in _FAKE_FILES:
        (tmp_path / local_name).write_bytes(b"x" * 32)

    def _explode(**kwargs):  # pragma: no cover —— 被调用即用例失败
        raise AssertionError(f"不该联网重下：{kwargs}")

    monkeypatch.setattr(download_reranker, "hf_hub_download", _explode)

    assert download_reranker.main() == 0


def test_download_flattens_onnx_subdir(tmp_path, monkeypatch):
    """仓库里的 onnx/ 层级要拍平成单一目录，空壳子目录顺手清掉。"""
    calls: list[dict] = []

    def _fake_download(**kwargs):
        calls.append(kwargs)
        target = Path(kwargs["local_dir"]) / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"y" * 32)
        return str(target)

    monkeypatch.setattr(download_reranker, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(download_reranker, "FILES", _FAKE_FILES)
    monkeypatch.setattr(download_reranker, "hf_hub_download", _fake_download)

    assert download_reranker.main() == 0

    assert (tmp_path / "model_int8.onnx").exists()
    assert (tmp_path / "tokenizer.json").exists()
    assert not (tmp_path / "onnx").exists()
    assert [call["filename"] for call in calls] == [
        "onnx/model_int8.onnx",
        "tokenizer.json",
    ]
    assert {call["endpoint"] for call in calls} == {"https://hf-mirror.com"}


def test_undersized_file_is_rejected_as_wrong_file(tmp_path, monkeypatch, capsys):
    """体积不足 = 拿错文件（如 656KB 的 stub），必须报出可读原因且退出码非 0。"""

    def _fake_download(**kwargs):
        target = Path(kwargs["local_dir"]) / kwargs["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"stub")  # 5 字节 < 下限 10 字节
        return str(target)

    monkeypatch.setattr(download_reranker, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(download_reranker, "FILES", _FAKE_FILES)
    monkeypatch.setattr(download_reranker, "hf_hub_download", _fake_download)

    assert download_reranker.main() == 1
    err = capsys.readouterr().err
    assert "体积异常" in err
    assert "model_int8.onnx" in err


def test_download_failure_reports_readable_reason(tmp_path, monkeypatch, capsys):
    """网络/镜像异常也要转成可读原因，不能把栈丢给用户。"""

    def _boom(**kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(download_reranker, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(download_reranker, "FILES", _FAKE_FILES)
    monkeypatch.setattr(download_reranker, "hf_hub_download", _boom)

    assert download_reranker.main() == 1
    err = capsys.readouterr().err
    assert "onnx/model_int8.onnx" in err
    assert "connection reset by peer" in err
    assert "hf-mirror.com" in err
