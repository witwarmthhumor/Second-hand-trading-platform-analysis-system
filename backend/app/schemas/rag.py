"""RAG 问答 Pydantic schema。"""

from typing import Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """问答请求。"""

    question: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(5, ge=1, le=10)
    session_id: str | None = Field(None, max_length=64)  # 多轮会话 ID，缺省为单轮


class SourceDoc(BaseModel):
    """命中的来源文档（供前端展示引用）。"""

    doc_id: str
    title: str
    text: str
    score: float


class AskResponse(BaseModel):
    """问答响应。"""

    answer: str
    mode: str  # "hit"（检索命中直出）| "fallback"（LLM 兜底生成）
    sources: list[SourceDoc] = []
    question: str
    session_id: str | None = None


class BatchDeleteRequest(BaseModel):
    """批量删除会话请求体。"""

    session_ids: list[str] = Field(..., min_length=1, max_length=100)


class FeedbackRequest(BaseModel):
    """问答反馈请求体（👍/👎）。"""

    session_id: str = Field(..., max_length=64)
    rating: str = Field(...)  # "like" | "dislike"
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field("", max_length=20000)
    # 回答模式：synth 为 P1-② 合成答案路径；用 Literal 拒绝任意字符串进库
    mode: Literal["hit", "fallback", "synth"] = "fallback"
    # 引用来源 id 数量有上限：反馈明细行会被反复全量读取，无上限可被刷出巨行
    source_doc_ids: list[str] = Field(default_factory=list, max_length=20)
    comment: str | None = Field(None, max_length=2000)
