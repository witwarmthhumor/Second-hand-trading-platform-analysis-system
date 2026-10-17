"""``app/rag/reranker.py`` 的离线单测（US-002）。

真模型是 570MB 的 int8 ONNX，加载一次就是秒级——用例一律用**假 session + 假
tokenizer**（``_load_tokenizer`` / ``_create_session`` 是模块级注入点），真模型留给
离线实验脚本。守住四类最容易出错、且出事时很难排查的行为：

1. logits 过 sigmoid 归一化（cross-encoder 输出未归一化，直接当 0~1 分数排序会翻车）；
2. feed 按 ``session.get_inputs()`` 动态构造（本模型没有 ``token_type_ids``，写死必炸）；
3. 模型缺失 / 加载失败 / 推理异常一律降级为「不可用」，**绝不向上抛**；
4. ``get_reranker()`` 是惰性单例，且限制 ``intra_op_num_threads``；
5. ``get_active_reranker()``（US-004 的开关入口）关闭态返回 None 且**不碰模型目录**。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.rag import reranker


class _FakeEncoding:
    """够用的假 Encoding：``_build_feed`` 只读这三个字段。"""

    def __init__(self, ids: list[int], attention_mask: list[int], type_ids: list[int]):
        self.ids = ids
        self.attention_mask = attention_mask
        self.type_ids = type_ids


class _FakeTokenizer:
    """记录 ``enable_*`` 与 ``encode`` 入参的假 tokenizer。

    同时充当 ``Tokenizer.from_file`` 的替身，让 ``_load_tokenizer`` 的**真实**逻辑
    （开 padding/truncation、按 ``<pad>`` 取 id）也能被测到，而不必加载 17MB 的
    ``tokenizer.json``。
    """

    PAD_ID: int | None = 1

    def __init__(self) -> None:
        self.truncation: int | None = None
        self.padding: dict | None = None
        self.encoded: list[tuple[str, str]] = []

    @classmethod
    def from_file(cls, _path: str) -> _FakeTokenizer:
        return cls()

    def token_to_id(self, token: str) -> int | None:
        return type(self).PAD_ID if token == "<pad>" else None

    def enable_truncation(self, max_length: int) -> None:
        self.truncation = max_length

    def enable_padding(self, **kwargs) -> None:
        self.padding = kwargs

    def encode_batch(self, pairs: list[tuple[str, str]]) -> list[_FakeEncoding]:
        """只提供批接口：padding 只在批内对齐，逐条 ``encode()`` 会得到不等长序列。

        假 tokenizer 刻意**不实现** ``encode``——生产代码若退回逐条编码，用例会因
        AttributeError（被降级分支吞掉）以「分数为空」失败。
        """
        self.encoded.extend(pairs)
        return [_FakeEncoding([0, 1, 2], [1, 1, 1], [0, 0, 0]) for _ in pairs]


class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    """假 InferenceSession：按 ``input_names`` 声明输入，按 batch 回一组 logits。"""

    def __init__(
        self,
        input_names: tuple[str, ...] = ("input_ids", "attention_mask"),
        error: Exception | None = None,
    ) -> None:
        self._input_names = list(input_names)
        self._error = error
        self.feeds: list[dict[str, np.ndarray]] = []
        self.batches: list[int] = []

    def get_inputs(self) -> list[_FakeInput]:
        return [_FakeInput(name) for name in self._input_names]

    def run(self, _output_names, feed):
        if self._error is not None:
            raise self._error
        self.feeds.append(feed)
        batch = len(feed["input_ids"])
        self.batches.append(batch)
        # 每个 passage 一个 logit（shape [batch, 1]，与实测真模型一致）
        logits = np.arange(batch, dtype=np.float32).reshape(batch, 1)
        return [logits]


def _explode(*_args, **_kwargs):  # pragma: no cover —— 被调用即用例失败
    raise AssertionError("模型缺失时不该尝试加载")


@pytest.fixture
def model_dir(tmp_path) -> Path:
    """一个「文件齐全」的假模型目录（内容无所谓，加载器已被换成假实现）。"""
    (tmp_path / reranker.MODEL_FILENAME).write_bytes(b"fake-onnx")
    (tmp_path / reranker.TOKENIZER_FILENAME).write_bytes(b"{}")
    return tmp_path


def _make_reranker(monkeypatch, model_dir: Path, session, tokenizer=None, **kwargs):
    tokenizer = tokenizer or _FakeTokenizer()
    monkeypatch.setattr(reranker, "_load_tokenizer", lambda path, max_length: tokenizer)
    monkeypatch.setattr(reranker, "_create_session", lambda path, num_threads: session)
    return reranker.CrossEncoderReranker(model_dir, **kwargs), tokenizer


def test_score_normalizes_logits_with_sigmoid(monkeypatch, model_dir):
    """打分是 sigmoid(logits)：logit 0 → 0.5，越大越接近 1，且严格落在 (0, 1)。"""
    # 假 session 对 batch 内第 i 条返回 logit i，故 3 条 passage 的 logits 是 0 / 1 / 2
    r, tokenizer = _make_reranker(monkeypatch, model_dir, _FakeSession())

    scores = r.score("问题", ["张三", "李四", "王五"])

    assert scores[0] == pytest.approx(0.5, abs=1e-4)
    assert scores[1] == pytest.approx(0.7311, abs=1e-3)
    assert scores[2] == pytest.approx(0.8808, abs=1e-3)
    assert all(0.0 < score < 1.0 for score in scores)
    assert tokenizer.encoded == [("问题", "张三"), ("问题", "李四"), ("问题", "王五")]


@pytest.mark.parametrize(("pad_id", "expected_pad_id"), [(1, 1), (None, 0)])
def test_load_tokenizer_pads_with_model_pad_token(
    monkeypatch, pad_id, expected_pad_id
):
    """pair 编码必须开 padding + truncation（同批长度不一 / 超长段落都会炸推理）。

    pad id 取模型自带的 ``<pad>``（XLM-R 是 1）；查不到时才退回 tokenizers 默认 0。
    """

    class _Tokenizer(_FakeTokenizer):
        PAD_ID = pad_id

    monkeypatch.setattr(reranker, "Tokenizer", _Tokenizer)

    tokenizer = reranker._load_tokenizer(Path("tokenizer.json"), 128)

    assert tokenizer.truncation == 128
    assert tokenizer.padding == {"pad_id": expected_pad_id, "pad_token": "<pad>"}


def test_score_splits_into_batches(monkeypatch, model_dir):
    """batch_size 控制一次 forward 的条数；条目数与入参一一对应。"""
    session = _FakeSession()
    r, _ = _make_reranker(monkeypatch, model_dir, session, batch_size=2)

    scores = r.score("q", ["a", "b", "c"])

    assert session.batches == [2, 1]
    assert len(scores) == 3


def test_feed_has_only_session_declared_inputs(monkeypatch, model_dir):
    """feed 按 get_inputs() 动态构造：本模型无 token_type_ids，就不能塞给它。"""
    session = _FakeSession(input_names=("input_ids", "attention_mask"))
    r, _ = _make_reranker(monkeypatch, model_dir, session)

    r.score("q", ["p"])

    assert set(session.feeds[0]) == {"input_ids", "attention_mask"}
    assert session.feeds[0]["input_ids"].dtype == np.int64
    assert session.feeds[0]["input_ids"].shape == (1, 3)

    with_token_types = _FakeSession(
        input_names=("input_ids", "attention_mask", "token_type_ids")
    )
    r2, _ = _make_reranker(monkeypatch, model_dir, with_token_types)
    r2.score("q", ["p"])
    assert set(with_token_types.feeds[0]) == {
        "input_ids",
        "attention_mask",
        "token_type_ids",
    }


def test_unknown_model_input_is_rejected(monkeypatch, model_dir):
    """模型要一个我们不会构造的输入时，宁可降级也不塞错张量。"""
    session = _FakeSession(input_names=("input_ids", "attention_mask", "pixel_values"))
    r, _ = _make_reranker(monkeypatch, model_dir, session)

    assert r.score("q", ["p"]) == []


def test_available_false_when_model_file_missing(monkeypatch, model_dir):
    """模型文件缺失 → available=False、score 返回空列表（调用方退回余弦排序）。"""
    (model_dir / reranker.MODEL_FILENAME).unlink()
    monkeypatch.setattr(reranker, "_create_session", _explode)
    monkeypatch.setattr(reranker, "_load_tokenizer", _explode)

    r = reranker.CrossEncoderReranker(model_dir)

    assert r.available is False
    assert reranker.MODEL_FILENAME in (r.load_error or "")
    assert r.score("q", ["p"]) == []


def test_available_false_when_tokenizer_file_missing(monkeypatch, model_dir):
    """分词器缺失同样是「不可用」，且不去加载模型（省掉几百 MB 的无用读盘）。"""
    (model_dir / reranker.TOKENIZER_FILENAME).unlink()
    monkeypatch.setattr(reranker, "_create_session", _explode)
    monkeypatch.setattr(reranker, "_load_tokenizer", _explode)

    r = reranker.CrossEncoderReranker(model_dir)

    assert r.available is False
    assert reranker.TOKENIZER_FILENAME in (r.load_error or "")


def test_load_failure_marks_unavailable_without_raising(monkeypatch, model_dir, caplog):
    """加载期异常（模型损坏 / ONNX runtime 缺库）只降级，构造不抛。"""

    def _boom(path, num_threads):
        raise RuntimeError("onnx 模型损坏")

    monkeypatch.setattr(reranker, "_create_session", _boom)
    monkeypatch.setattr(
        reranker, "_load_tokenizer", lambda path, max_length: _FakeTokenizer()
    )

    with caplog.at_level("WARNING"):
        r = reranker.CrossEncoderReranker(model_dir)

    assert r.available is False
    assert "onnx 模型损坏" in (r.load_error or "")
    assert "rerank 加载失败" in caplog.text


def test_inference_error_does_not_leak(monkeypatch, model_dir, caplog):
    """推理期异常不外泄：返回空列表并留一条可观测日志（原因要能看见）。"""
    session = _FakeSession(error=RuntimeError("session 爆炸"))
    r, _ = _make_reranker(monkeypatch, model_dir, session)

    with caplog.at_level("WARNING"):
        scores = r.score("q", ["p", "q"])

    assert scores == []
    assert "session 爆炸" in caplog.text


def test_empty_passages_skips_inference(monkeypatch, model_dir):
    """空候选池不打分、不推理（省掉一次无意义的 forward）。"""
    session = _FakeSession()
    r, _ = _make_reranker(monkeypatch, model_dir, session)

    assert r.score("q", []) == []
    assert session.feeds == []


def test_get_reranker_is_lazy_singleton(monkeypatch, model_dir):
    """首次调用才建实例，之后复用同一份（570MB 模型不能按请求加载）。"""
    monkeypatch.setattr(reranker, "_reranker", None)
    monkeypatch.setattr(reranker, "DEFAULT_MODEL_DIR", model_dir)
    monkeypatch.setattr(
        reranker, "_load_tokenizer", lambda path, max_length: _FakeTokenizer()
    )
    created: list[int] = []

    def _fake_create(path, num_threads):
        created.append(num_threads)
        return _FakeSession()

    monkeypatch.setattr(reranker, "_create_session", _fake_create)

    first = reranker.get_reranker()
    second = reranker.get_reranker()

    assert first is second
    assert created == [reranker.DEFAULT_NUM_THREADS]


def test_get_reranker_passes_thread_limit(monkeypatch, model_dir):
    """线程数要一路传到 session 构造（并发请求下防线程爆炸），不是摆设参数。"""
    monkeypatch.setattr(reranker, "_reranker", None)
    monkeypatch.setattr(
        reranker, "_load_tokenizer", lambda path, max_length: _FakeTokenizer()
    )
    captured: dict = {}

    def _fake_create(path, num_threads):
        captured["num_threads"] = num_threads
        return _FakeSession()

    monkeypatch.setattr(reranker, "_create_session", _fake_create)

    reranker.get_reranker(model_dir, num_threads=2)

    assert captured["num_threads"] == 2


def test_active_reranker_is_none_when_disabled(monkeypatch):
    """开关关闭（默认）→ None，且**连实例都不建**（默认部署零新增开销）。

    ``_load_tokenizer`` / ``_create_session`` 换成「一调用就 raise」的桩：
    关闭态若偷偷去加载 570MB 模型，用例会立刻红，而不是靠断言返回值蒙混过去。
    """
    monkeypatch.setattr(reranker, "RERANKER_ENABLED", False)
    monkeypatch.setattr(reranker, "_reranker", None)
    monkeypatch.setattr(reranker, "_load_tokenizer", _explode)
    monkeypatch.setattr(reranker, "_create_session", _explode)

    assert reranker.get_active_reranker() is None
    assert reranker._reranker is None


def test_active_reranker_is_none_when_model_missing(monkeypatch, tmp_path):
    """开关开着但模型缺失 → None：惰性降级，服务照常起，检索退回余弦排序。"""
    monkeypatch.setattr(reranker, "RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker, "RERANKER_MODEL_DIR", tmp_path)
    monkeypatch.setattr(reranker, "_reranker", None)

    assert reranker.get_active_reranker() is None


def test_active_reranker_passes_config_and_reuses_singleton(monkeypatch, model_dir):
    """开关开着且模型就绪 → 单例；max_length / batch_size 确实取自配置（不是摆设）。"""
    monkeypatch.setattr(reranker, "RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker, "RERANKER_MODEL_DIR", model_dir)
    monkeypatch.setattr(reranker, "RERANKER_MAX_LENGTH", 128)
    monkeypatch.setattr(reranker, "RERANKER_BATCH_SIZE", 3)
    monkeypatch.setattr(reranker, "_reranker", None)
    session = _FakeSession()
    captured: dict = {}

    def _fake_load(path, max_length):
        captured["max_length"] = max_length
        return _FakeTokenizer()

    monkeypatch.setattr(reranker, "_load_tokenizer", _fake_load)
    monkeypatch.setattr(reranker, "_create_session", lambda path, num_threads: session)

    first = reranker.get_active_reranker()
    second = reranker.get_active_reranker()

    assert first is not None
    assert first is second
    assert captured["max_length"] == 128
    first.score("q", ["a", "b", "c", "d"])
    assert session.batches == [3, 1]


def test_create_session_sets_intra_op_threads(monkeypatch, tmp_path):
    """``_create_session`` 必须把线程数写进 SessionOptions（唯一真正生效的地方）。"""
    recorded: dict = {}

    class _SessionOptions:
        intra_op_num_threads = 0

    def _fake_session(path, sess_options=None, providers=None):
        recorded["options"] = sess_options
        recorded["providers"] = providers
        return "session"

    monkeypatch.setattr(reranker.ort, "SessionOptions", _SessionOptions)
    monkeypatch.setattr(reranker.ort, "InferenceSession", _fake_session)

    session = reranker._create_session(tmp_path / "m.onnx", 3)

    assert session == "session"
    assert recorded["options"].intra_op_num_threads == 3
    assert recorded["providers"] == ["CPUExecutionProvider"]
