"""rerank 降级路径与开关一致性（US-009）。

与 ``test_rag_rerank_flow.py`` 的分工：那里用「契约替身」注入精排器（只验证排序键
替换），这里走**真实链路** —— ``HybridRetriever.search`` / ``search_dual`` →
``_rerank_scores`` → ``reranker.get_active_reranker()`` → ``get_reranker()`` →
真 ``CrossEncoderReranker``，只把 ``_load_tokenizer`` / ``_create_session`` 换成假
实现（不加载 570MB 真模型）。

把 PRD 原文的三类故障真的注入进去，并断言**最终检索结果**：

1. **模型文件缺失** → ``get_active_reranker()`` 返回 None（连加载都不试）；
2. **分词器加载失败** → ``_load()`` 捕获，``available=False``；
3. **ONNX 推理抛异常** → ``score()`` 捕获，返回空列表 → 空字典。

每条都要求：结果**非空**、与关闭态**逐位一致**、``rerank_score`` 全为 None、
留一条含原因的 warning、**不向上抛**。另外守住两条一致性：

- 「问答接口不返回 5xx」的服务层等价物：``rag_service.ask`` 照常命中直出；
- **惰性降级**：开关开着 + 模型坏，服务启动（建检索器）不触发任何加载，
  故障只在首次检索时暴露一次，之后复用失败单例、不反复重试。
"""

from __future__ import annotations

import pytest
from test_rag_rerank_flow import _DOC_IDS, _RRF, _V_SCORES, _ids, _texts
from test_reranker import _FakeSession, _FakeTokenizer

from app.rag import reranker as reranker_module
from app.rag import retriever as retriever_module
from app.rag.retriever import HybridRetriever

# 关闭态（余弦序）基线：与 test_rag_rerank_flow 的候选池同源。
_OFF_IDS = ["c0", "c1", "c2", "c6", "c7"]


def _explode(*_args, **_kwargs):  # pragma: no cover —— 被调用即用例失败
    raise AssertionError("这一步不该尝试加载模型")


def _retriever(monkeypatch) -> HybridRetriever:
    """只暴露候选池的检索器替身（绕开 ``__init__`` 的 Chroma PersistentClient）。"""
    inst = HybridRetriever.__new__(HybridRetriever)
    inst._ready = True  # rag_service.ask 会读 is_ready
    inst._doc_ids = list(_DOC_IDS)
    monkeypatch.setattr(
        inst, "_hybrid_candidates", lambda query: (dict(_RRF), dict(_V_SCORES), _texts())
    )
    return inst


def _wire(monkeypatch, model_dir):
    """把生产链路接到「真 ``get_active_reranker`` + 真 ``CrossEncoderReranker``」上。

    只替换配置与加载器（不碰真模型），返回 ``(检索器, 开关盒)``：盒子 ``off=True``
    表示把取用入口换成恒返回 None，用来取**关闭态基线**。
    """
    monkeypatch.setattr(reranker_module, "RERANKER_ENABLED", True)
    monkeypatch.setattr(reranker_module, "RERANKER_MODEL_DIR", model_dir)
    monkeypatch.setattr(reranker_module, "_reranker", None)
    box = {"off": False}
    real = reranker_module.get_active_reranker
    monkeypatch.setattr(
        retriever_module, "get_active_reranker", lambda: None if box["off"] else real()
    )
    return _retriever(monkeypatch), box


def _off_state(inst: HybridRetriever, box: dict, top_k: int = 5):
    """在「开关关闭」下取一次基线（search 与 search_dual 都取）。"""
    box["off"] = True
    try:
        return inst.search("q", top_k=top_k), inst.search_dual("q", top_k=top_k)
    finally:
        box["off"] = False


@pytest.fixture
def model_dir(tmp_path):
    """「文件齐全」的假模型目录（内容无所谓，加载器已被换成假实现）。"""
    (tmp_path / reranker_module.MODEL_FILENAME).write_bytes(b"fake-onnx")
    (tmp_path / reranker_module.TOKENIZER_FILENAME).write_bytes(b"{}")
    return tmp_path


# ---------------------------------------------------------------------------
# 三类故障注入：最终检索结果非空且与关闭态逐位一致
# ---------------------------------------------------------------------------


def _fail_model_missing(monkeypatch, model_dir):
    """模型文件缺失：连加载都不该尝试（省掉几百 MB 的无谓读盘）。"""
    (model_dir / reranker_module.MODEL_FILENAME).unlink()
    monkeypatch.setattr(reranker_module, "_load_tokenizer", _explode)
    monkeypatch.setattr(reranker_module, "_create_session", _explode)


def _fail_tokenizer_load(monkeypatch, _model_dir):
    """分词器加载失败：_load 捕获，session 根本不该被创建。"""

    def _boom(path, max_length):
        raise RuntimeError("tokenizer.json 解析失败")

    monkeypatch.setattr(reranker_module, "_load_tokenizer", _boom)
    monkeypatch.setattr(reranker_module, "_create_session", _explode)


def _fail_inference(monkeypatch, _model_dir):
    """加载成功、推理抛异常：score 捕获后返回空列表。"""
    session = _FakeSession(error=RuntimeError("ONNX session 爆炸"))
    monkeypatch.setattr(
        reranker_module, "_load_tokenizer", lambda path, max_length: _FakeTokenizer()
    )
    monkeypatch.setattr(reranker_module, "_create_session", lambda path, n: session)


@pytest.mark.parametrize(
    ("apply_failure", "reason"),
    [
        (_fail_model_missing, "模型文件缺失"),
        (_fail_tokenizer_load, "tokenizer.json 解析失败"),
        (_fail_inference, "ONNX session 爆炸"),
    ],
    ids=["模型缺失", "分词器加载失败", "推理抛异常"],
)
def test_三类故障都退回余弦排序(monkeypatch, model_dir, caplog, apply_failure, reason):
    """三类故障各自的最终检索结果都必须非空、与关闭态逐位一致，并留一条含原因的日志。"""
    apply_failure(monkeypatch, model_dir)
    inst, box = _wire(monkeypatch, model_dir)
    off, (off_breadth, off_depth) = _off_state(inst, box)

    with caplog.at_level("WARNING"):
        out = inst.search("q", top_k=5)
        breadth, depth = inst.search_dual("q", top_k=5)

    # 非空 + 逐位一致（含字段口径：rerank_score 一律 None，不是 0.0）
    assert out, "降级后检索结果不得为空"
    assert _ids(out) == _ids(off) == _OFF_IDS
    assert [r["rerank_score"] for r in out] == [None] * 5
    assert _ids(breadth) == _ids(off_breadth) == _OFF_IDS
    assert _ids(depth) == _ids(off_depth)
    assert [r["rerank_score"] for r in breadth + depth] == [None] * 10

    # 降级原因必须可观测（不向用户暴露堆栈，但日志里要能看见）
    assert reason in caplog.text


# ---------------------------------------------------------------------------
# 惰性降级：开关开着 + 模型坏，服务启动不失败
# ---------------------------------------------------------------------------


def test_模型坏掉时启动不失败_首次检索才惰性降级(monkeypatch, model_dir):
    """建检索器（≈服务启动）不触发任何加载；首次检索才暴露故障，之后不反复重试。

    这条守住 AC-3：``RERANKER_ENABLED=true`` 但模型缺失时，进程能正常起 ——
    加载被推迟到第一次真正需要打分的检索。
    """
    attempts: list[str] = []

    def _boom(path, max_length):
        attempts.append("tokenizer")
        raise RuntimeError("模型坏掉了")

    monkeypatch.setattr(reranker_module, "_load_tokenizer", _boom)
    monkeypatch.setattr(reranker_module, "_create_session", _explode)
    inst, _ = _wire(monkeypatch, model_dir)

    # 「启动」阶段：配置已打开、单例为空，但一次加载尝试都没有
    assert reranker_module.RERANKER_ENABLED is True
    assert reranker_module._reranker is None
    assert attempts == []

    out = inst.search("q", top_k=5)
    assert _ids(out) == _OFF_IDS  # 首次检索 → 惰性加载失败 → 退回余弦
    assert attempts == ["tokenizer"]

    # 失败实例被单例缓存：第二次检索不再重试（不会每个请求都读一遍模型）
    assert _ids(inst.search("q", top_k=5)) == _OFF_IDS
    assert attempts == ["tokenizer"]


# ---------------------------------------------------------------------------
# 服务层等价物：问答接口不因 rerank 故障而失败
# ---------------------------------------------------------------------------


async def test_问答接口在rerank故障时正常应答(monkeypatch, model_dir, caplog):
    """AC「不返回 5xx」的服务层等价物：rerank 推理抛异常时 ask 照常命中直出。"""
    from app.services import rag_service

    _fail_inference(monkeypatch, model_dir)
    inst, _ = _wire(monkeypatch, model_dir)
    monkeypatch.setattr(rag_service, "get_retriever", lambda: inst)
    monkeypatch.setattr(rag_service, "SYNTH_MODE", "off")  # 本用例只验直出路径

    with caplog.at_level("WARNING"):
        resp = await rag_service.ask("q")

    assert resp.mode == "hit"
    assert resp.answer == "chunk-0"  # 余弦序首位，与关闭态一致
    assert [s.doc_id for s in resp.sources] == ["A", "B", "C", "D"]
    assert "ONNX session 爆炸" in caplog.text
