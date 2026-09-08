"""RAG 问答 API。

- POST /api/rag/ask    ：问答（检索命中直出 + 来源，未命中 LLM 兜底）
- GET  /api/rag/status ：知识库就绪状态
"""

from fastapi import APIRouter

from app.rag.retriever import get_retriever
from app.schemas.rag import AskRequest, AskResponse
from app.services import rag_service

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest) -> AskResponse:
    """二手交易知识库问答。"""
    return await rag_service.ask(payload.question, top_k=payload.top_k)


@router.get("/status")
async def status() -> dict:
    """返回向量库就绪状态。"""
    retriever = get_retriever()
    return {
        "ready": retriever.is_ready,
        "message": "知识库就绪" if retriever.is_ready else "知识库未就绪，请先运行建库脚本",
    }
