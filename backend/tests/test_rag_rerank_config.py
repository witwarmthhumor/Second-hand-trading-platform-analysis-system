"""``app/rag/config.py`` 的 rerank 配置项单测（US-004）。

守住两件事：

1. **默认值**：开关默认关（关着时链路一行都不多跑）、窗口默认 15（≥ TOP_K 硬下限）、
   长度/批大小/线程数锚定实测值；
2. **环境变量覆盖**：``_ENV_NAMES`` 里的值全部可覆盖，且布尔解析对拼错的值**回落安全默认（关）**。

为什么不直接 import 后断言常量：``app/rag/config.py`` 的取值在 **import 期**就完成了
（``os.getenv`` 直接读），已经 import 进来的模块值早定死了，测不出解析分支。这里用
``spec_from_file_location`` 把同一个文件在**隔离命名空间**里按当前 env 重新执行一遍
（与 ``test_download_reranker.py`` / ``test_rerank_separability.py`` 挂载脚本同套路）：
既跑的是真实代码，又不会像 ``importlib.reload`` 那样改到进程内共享的模块对象
（reload 之后 env 一还原还得再 reload 一次，漏一次就会把覆盖值留给后续用例）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.rag import config as rag_config

_CONFIG_PATH = Path(rag_config.__file__).resolve()
_ENV_NAMES = (
    "RERANKER_ENABLED",
    "RERANKER_MODEL_DIR",
    "RERANKER_TOP_N",
    "RERANKER_MAX_LENGTH",
    "RERANKER_BATCH_SIZE",
    "RERANKER_NUM_THREADS",
)


def _load_config():
    """按当前 ``os.environ`` 重新执行一遍 config.py，返回隔离命名空间里的模块。"""
    spec = importlib.util.spec_from_file_location("_rerank_config_probe", _CONFIG_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_reranker_env(monkeypatch):
    """所有用例都从「env 里没有 _ENV_NAMES 中任何变量」出发，要覆盖的自己 setenv。"""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_off_and_anchored_to_measured_values():
    module = _load_config()

    # 默认关：延迟优化扫描（rerank_topn_sweep.md）后仍超 G1 上限（≤2s），保持默认关。
    assert module.RERANKER_ENABLED is False
    # 窗口默认 15（US-003 选点：效果不劣化前提下延迟最低的完全合规档）；
    # 与 QUERY_N_RESULTS=20 已解耦——窗口小于候选池正是「降 TOP_N 提速」的本意。
    assert module.QUERY_N_RESULTS == 20
    assert module.RERANKER_TOP_N == 15
    # 长度/批大小/线程数是实测选定的平衡点（US-004：threads 8 比 4 快 20%，16 反而退化）。
    assert module.RERANKER_MAX_LENGTH == 512
    assert module.RERANKER_BATCH_SIZE == 8
    assert module.RERANKER_NUM_THREADS == 8
    assert module.RERANKER_MODEL_DIR == (
        module.PROJECT_ROOT / "backend" / "models" / "bge-reranker-v2-m3"
    )
    assert isinstance(module.RERANKER_MODEL_DIR, Path)


def test_top_n_default_respects_hard_floor():
    """硬下限守卫（US-003）：默认窗口必须 ≥ TOP_K，否则 rerank 连 top-5 都产不出。

    窗口 < top_k 时窗口外排序键全为 0，top-5 名单会混入「没被打过分」的候选。
    这测的是**默认值本身**；env 误配（如 RERANKER_TOP_N=3）由检索侧运行时守卫
    兜底——``_rerank_scores`` 会把窗口放大到 top_k 并记 warning
    （见 test_rag_rerank_flow.py::test_窗口低于top_k时自动放大并告警）。
    """
    module = _load_config()

    assert module.RERANKER_TOP_N >= module.TOP_K


def test_reranker_module_defaults_come_from_config():
    """reranker.py 的 DEFAULT_* 必须引用 config（唯一口径），不是各写一份字面量。"""
    from app.rag import reranker

    assert reranker.DEFAULT_MODEL_DIR == rag_config.RERANKER_MODEL_DIR
    assert reranker.DEFAULT_MAX_LENGTH == rag_config.RERANKER_MAX_LENGTH
    assert reranker.DEFAULT_BATCH_SIZE == rag_config.RERANKER_BATCH_SIZE
    assert reranker.DEFAULT_NUM_THREADS == rag_config.RERANKER_NUM_THREADS


def test_env_overrides_every_value(monkeypatch, tmp_path):
    model_dir = tmp_path / "custom-reranker"
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    monkeypatch.setenv("RERANKER_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("RERANKER_TOP_N", "7")
    monkeypatch.setenv("RERANKER_MAX_LENGTH", "128")
    monkeypatch.setenv("RERANKER_BATCH_SIZE", "3")
    monkeypatch.setenv("RERANKER_NUM_THREADS", "2")

    module = _load_config()

    assert module.RERANKER_ENABLED is True
    assert module.RERANKER_MODEL_DIR == model_dir
    assert isinstance(module.RERANKER_MODEL_DIR, Path)
    assert module.RERANKER_TOP_N == 7
    assert module.RERANKER_MAX_LENGTH == 128
    assert module.RERANKER_BATCH_SIZE == 3
    assert module.RERANKER_NUM_THREADS == 2


def test_top_n_override_does_not_touch_recall(monkeypatch):
    """打分窗口是独立旋钮：调它不改变召回量（候选池仍由 QUERY_N_RESULTS 决定）。"""
    monkeypatch.setenv("RERANKER_TOP_N", "10")

    module = _load_config()

    assert module.RERANKER_TOP_N == 10
    assert module.QUERY_N_RESULTS == 20


@pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "yes", "on", " true "])
def test_enabled_truthy_values(monkeypatch, raw):
    monkeypatch.setenv("RERANKER_ENABLED", raw)

    assert _load_config().RERANKER_ENABLED is True


@pytest.mark.parametrize(
    "raw", ["false", "FALSE", "0", "no", "off", "", "treu", "enabled", "2"]
)
def test_enabled_falsy_or_invalid_values_fall_back_to_off(monkeypatch, raw):
    """非法值回落安全默认（关）：把开关拼错的代价必须是「没开」，不是「悄悄开了」。"""
    monkeypatch.setenv("RERANKER_ENABLED", raw)

    assert _load_config().RERANKER_ENABLED is False
