"""知识库管理 API（仅管理员）。

- GET    /api/kb/documents      列出全部语料文档
- POST   /api/kb/documents      上传新增 markdown 文档
- DELETE /api/kb/documents      删除指定文档
- POST   /api/kb/rebuild        重建向量库（分块+embedding+建库，后台执行）

普通用户访问一律 403。
"""

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel

from app.deps import require_admin
from app.services import kb_service

router = APIRouter(prefix="/api/kb", tags=["kb"])

# 后台重建任务状态（简单内存态：正在重建时的 chunk 数，完成后保留最近一次结果）
_rebuild_state: dict = {"running": False, "last_result": None}


class UploadResponse(BaseModel):
    path: str
    filename: str
    category: str


@router.get("/documents")
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    _=Depends(require_admin),
) -> dict:
    """列出知识库文档（仅管理员），支持分页。

    返回 { total, page, page_size, items }；默认每页 20 条。
    """
    docs = kb_service.list_documents()
    total = len(docs)
    start = (page - 1) * page_size
    page_items = docs[start : start + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "path": d.path,
                "filename": d.filename,
                "kind": d.kind,
                "category": d.category,
                "size": d.size,
                "modified_at": d.modified_at,
            }
            for d in page_items
        ],
    }


@router.post("/documents", response_model=UploadResponse)
async def upload_document(
    file: UploadFile,
    category: str | None = None,
    _=Depends(require_admin),
) -> UploadResponse:
    """上传新增 markdown 文档（仅管理员）。"""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(content) > 2 * 1024 * 1024:  # 2MB 上限
        raise HTTPException(status_code=400, detail="文件过大（>2MB）")

    try:
        doc = kb_service.upload_document(file.filename or "", content, category)
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件须为 UTF-8 编码") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return UploadResponse(path=doc.path, filename=doc.filename, category=doc.category)


@router.delete("/documents")
async def delete_document(
    path: str = Query(..., min_length=1, max_length=500),
    _=Depends(require_admin),
) -> dict:
    """删除指定文档（仅管理员）。path 为相对项目根路径，经 query 传入。"""
    try:
        deleted = kb_service.delete_document(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"deleted": deleted, "path": path}


def _run_rebuild() -> None:
    """后台执行重建，捕获异常写入状态。"""
    _rebuild_state["running"] = True
    _rebuild_state["last_result"] = None
    try:
        result = kb_service.rebuild_vector_store()
        _rebuild_state["last_result"] = {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001 - 后台任务需兜底所有异常
        _rebuild_state["last_result"] = {"ok": False, "error": str(exc)}
    finally:
        _rebuild_state["running"] = False


@router.post("/rebuild")
async def rebuild(
    background_tasks: BackgroundTasks, _=Depends(require_admin)
) -> dict:
    """触发重建向量库（后台执行，返回当前状态）。"""
    if _rebuild_state["running"]:
        raise HTTPException(status_code=409, detail="重建已在进行中")

    background_tasks.add_task(_run_rebuild)
    return {"triggered": True}


@router.get("/rebuild/status")
async def rebuild_status(_=Depends(require_admin)) -> dict:
    """查询重建任务状态。"""
    return _rebuild_state
