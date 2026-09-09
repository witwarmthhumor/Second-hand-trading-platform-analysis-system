"""RAG 问答 Pydantic schema。"""

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
