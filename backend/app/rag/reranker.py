"""ONNX cross-encoder 精排器：给 (query, passage) 对打分，用于重排候选池。

为什么需要（详见 ``docs/Rerank方案.md`` §1）：bge-m3 是 **bi-encoder**，query 与
passage 各自独立编码后才算余弦，同域语料被压在极窄区间内——P0-2 实测「期望 / 非
期望」文档代表块的余弦比值中位只差 **0.036**，多档阈值扫描结果**逐位相同**，即余弦
做不了细粒度分级。cross-encoder 把 (query, passage) 拼成一条序列送进模型、直接建模
二者的交互，输出单一相关性 logits，分布天然可分。

实现取舍：

- **ONNX 而非 sentence-transformers**：不引入 torch/transformers。``onnxruntime``
  与 ``tokenizers`` 随 chromadb 已是本仓库既有依赖，走这条路零新增重依赖。
- **文件缺失 / 加载失败 / 推理异常一律降级为「不可用」，绝不向上抛**：检索链路据此
  退回余弦排序，问答接口不会因为 reranker 挂掉而返回 5xx（US-009）。
- **只重排、不扩大召回**：本模块不认识候选池大小，``RERANKER_TOP_N`` 由调用方
  （retriever）决定传哪几条进来。
- **单测不加载真模型**（570MB，一次加载就是秒级）：``_load_tokenizer`` /
  ``_create_session`` 是模块级注入点，测试换假实现即可覆盖推理与降级分支。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Encoding, Tokenizer

from app.rag.config import (
    RERANKER_BATCH_SIZE,
    RERANKER_ENABLED,
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_DIR,
    RERANKER_NUM_THREADS,
)

logger = logging.getLogger(__name__)

# 模型落点：backend/models/bge-reranker-v2-m3/（``scripts/download_reranker.py`` 的
# 默认落点，已在 .gitignore 中，模型文件不入版本库）。
# 三个默认值一律**引用 config 的 RERANKER_\***（US-004）——配置只有一份口径，
# 别在这里另写字面量，否则改环境变量时会出现「config 改了、这里没改」的静默分叉。
DEFAULT_MODEL_DIR = RERANKER_MODEL_DIR
MODEL_FILENAME = "model_int8.onnx"
TOKENIZER_FILENAME = "tokenizer.json"

DEFAULT_MAX_LENGTH = RERANKER_MAX_LENGTH
DEFAULT_BATCH_SIZE = RERANKER_BATCH_SIZE
# 显式限制算子内并行线程数：XLM-R-large 在 CPU 上默认吃满所有核，多请求并发时
# 会各开一份、叠成线程爆炸。InferenceSession 本身可并发调用，限制线程即可。
# 默认 8 = 物理核数（US-004 实测 -20% 延迟；16 线程因超线程争抢反而退化）。
#
# ⚠️ 并发叠乘（质量审计 P3-N，上线前必读）：8 是**每次 run** 的算子内线程数，
# 单例 session 被 N 个并发请求同时 run 时峰值线程 = N×8——8C/16T 机器上 2 个
# 并发即饱和、4 个并发开始争抢互拖。当前 RERANKER_ENABLED=false 不触发；上线时
# 须二选一：① 在 score() 外加信号量把并发 rerank 限到 1~2；② 按部署核数调低
# RERANKER_NUM_THREADS（如 4C 机器设 2~4，宁可单请求慢一点也不要吞吐塌方）。
DEFAULT_NUM_THREADS = RERANKER_NUM_THREADS

_PAD_TOKEN = "<pad>"


def _load_tokenizer(tokenizer_path: Path, max_length: int) -> Tokenizer:
    """加载 tokenizer 并开启 padding + truncation（pair 编码的两项必备处理）。

    padding 只在**批内**对齐（``encode_batch`` 一次编码一批），所以打分时不能逐条
    ``encode()``。pad 用模型自带的 ``<pad>``（XLM-R 里 id 是 1，不是 tokenizers 的
    默认 0）：padding 位置已被 attention_mask 屏蔽，但写对不必依赖这一点。

    测试注入点：真 ``tokenizer.json`` 有 17MB。
    """
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    pad_id = tokenizer.token_to_id(_PAD_TOKEN)
    tokenizer.enable_truncation(max_length=max_length)
    tokenizer.enable_padding(
        pad_id=pad_id if pad_id is not None else 0, pad_token=_PAD_TOKEN
    )
    return tokenizer


def _create_session(model_path: Path, num_threads: int) -> ort.InferenceSession:
    """建 InferenceSession（测试注入点，同 ``_load_tokenizer``）。"""
    options = ort.SessionOptions()
    options.intra_op_num_threads = num_threads
    return ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _sigmoid(logits: np.ndarray) -> np.ndarray:
    """logits → (0, 1)：cross-encoder 输出的是未归一化分数，过 sigmoid 后才能与别的分数同量纲。"""
    return 1.0 / (1.0 + np.exp(-logits))


def _build_feed(
    session: ort.InferenceSession, encodings: list[Encoding]
) -> dict[str, np.ndarray]:
    """按 session **声明的**输入名构造 feed。

    不硬编码 ``token_type_ids``：本模型的导出里没有它（实测输入恰为 ``input_ids`` +
    ``attention_mask``），但同架构的其它导出可能有，写死任何一边都会在换模型时炸。
    """
    columns = {
        "input_ids": [encoding.ids for encoding in encodings],
        "attention_mask": [encoding.attention_mask for encoding in encodings],
        "token_type_ids": [encoding.type_ids for encoding in encodings],
    }
    feed: dict[str, np.ndarray] = {}
    for spec in session.get_inputs():
        column = columns.get(spec.name)
        if column is None:
            raise ValueError(f"模型要求未知输入 {spec.name!r}")
        feed[spec.name] = np.asarray(column, dtype=np.int64)
    return feed


def _score_passages(
    tokenizer: Tokenizer,
    session: ort.InferenceSession,
    query: str,
    passages: list[str],
    batch_size: int,
) -> list[float]:
    """分批 pair 编码 → 推理 → sigmoid，返回与 ``passages`` 等长的分数。"""
    scores: list[float] = []
    for start in range(0, len(passages), batch_size):
        batch = passages[start : start + batch_size]
        # 必须整批一次编码：tokenizers 的 padding 只在**批内**对齐，逐条 encode()
        # 得到的是不等长序列，堆成 ndarray 时会直接抛 inhomogeneous shape。
        encodings = tokenizer.encode_batch([(query, passage) for passage in batch])
        logits = session.run(None, _build_feed(session, encodings))[0]
        scores.extend(float(value) for value in _sigmoid(np.asarray(logits)).reshape(-1))
    return scores


class CrossEncoderReranker:
    """ONNX cross-encoder 精排器（构造即加载；模型缺失时 ``available=False``）。"""

    def __init__(
        self,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = DEFAULT_BATCH_SIZE,
        num_threads: int = DEFAULT_NUM_THREADS,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._max_length = int(max_length)
        self._batch_size = max(1, int(batch_size))
        self._num_threads = int(num_threads)
        self._session: ort.InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None
        self._load_error: str | None = None
        self._load()

    @property
    def available(self) -> bool:
        """模型与分词器都就绪才为 True；任一缺失或加载失败都是 False。"""
        return self._session is not None and self._tokenizer is not None

    @property
    def load_error(self) -> str | None:
        """不可用的原因（可用时为 None），供降级日志与排查使用。"""
        return self._load_error

    def score(self, query: str, passages: list[str]) -> list[float]:
        """给一批 passage 打相关度分（0~1，越大越相关）。

        Returns:
            与 ``passages`` 等长的分数列表；**不可用或推理失败时返回空列表**。
            调用方据此退回原排序——长度不足即视为「没打成」，别把空列表当真分数用。
        """
        if not passages:
            return []
        tokenizer, session = self._tokenizer, self._session
        if not self.available or tokenizer is None or session is None:
            return []
        try:
            return _score_passages(
                tokenizer, session, query, passages, self._batch_size
            )
        except Exception as exc:
            logger.warning(
                "rerank 打分失败，退回原排序：%s: %s", type(exc).__name__, exc
            )
            return []

    def _load(self) -> None:
        """加载模型与分词器；任何失败都只记 ``_load_error``，不抛。"""
        model_path = self._model_dir / MODEL_FILENAME
        tokenizer_path = self._model_dir / TOKENIZER_FILENAME
        missing = [
            name
            for name, path in (
                (MODEL_FILENAME, model_path),
                (TOKENIZER_FILENAME, tokenizer_path),
            )
            if not path.is_file()
        ]
        if missing:
            self._load_error = f"模型文件缺失: {', '.join(missing)}"
            logger.warning("rerank 不可用（%s）：%s", self._model_dir, self._load_error)
            return
        try:
            self._tokenizer = _load_tokenizer(tokenizer_path, self._max_length)
            self._session = _create_session(model_path, self._num_threads)
        except Exception as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.warning("rerank 加载失败（%s）：%s", model_path, self._load_error)


_reranker: CrossEncoderReranker | None = None
# 首次并发请求可能同时走到这里：570MB 模型被加载两份会瞬间翻倍内存，故加锁。
_reranker_lock = threading.Lock()


def get_reranker(
    model_dir: Path | str | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
    batch_size: int = DEFAULT_BATCH_SIZE,
    num_threads: int = DEFAULT_NUM_THREADS,
) -> CrossEncoderReranker:
    """惰性单例：首次调用才加载模型，进程内只保留一份。

    参数**只在首次调用时生效**（之后返回同一实例）——改配置请重启进程；测试里
    monkeypatch 模块级 ``_reranker`` 置 None 后重取即可。``model_dir`` 留空用
    ``DEFAULT_MODEL_DIR``。
    """
    global _reranker
    if _reranker is not None:
        return _reranker
    with _reranker_lock:
        if _reranker is None:
            _reranker = CrossEncoderReranker(
                model_dir if model_dir is not None else DEFAULT_MODEL_DIR,
                max_length=max_length,
                batch_size=batch_size,
                num_threads=num_threads,
            )
    return _reranker


def get_active_reranker() -> CrossEncoderReranker | None:
    """检索链路的取用入口：**开关关闭或模型不可用 → None**（调用方退回余弦排序）。

    与 ``get_reranker()`` 的分工：后者是 US-002 的纯惰性单例，离线实验脚本
    （``scripts/experiments/ragas_eval/rerank_separability.py``）直接拿它跑真模型，
    不受生产开关影响；本函数才是生产链路的唯一入口，把「开关」与「可用性」两件事
    收在一处——调用方拿到 None 就按原排序走，不必自己判 ``available``。

    ``RERANKER_ENABLED=false``（默认）时**不触碰模型目录**（连实例都不建），
    所以默认关闭的部署零新增开销、也不会因为模型没下载而报警。

    降级可观测性：开关开着但模型缺失 / 加载失败时，``_load()`` 会留一条含原因的
    warning（US-009 依赖这条），本函数只负责把它翻成 None。
    """
    if not RERANKER_ENABLED:
        return None
    reranker = get_reranker(
        model_dir=RERANKER_MODEL_DIR,
        max_length=RERANKER_MAX_LENGTH,
        batch_size=RERANKER_BATCH_SIZE,
        num_threads=RERANKER_NUM_THREADS,
    )
    return reranker if reranker.available else None
