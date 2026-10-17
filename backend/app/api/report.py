"""竞品分析报告 API：生成 Markdown 报告并提供下载。"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.settings import get_rating_dimensions
from app.db import get_session
from app.services import report_service

router = APIRouter(prefix="/api/report", tags=["report"])


class ReportRequest(BaseModel):
    ids: list[int] | None = Field(
        default=None, description="参与报告的竞品 id；不传则导出全部已入库竞品"
    )


class ReportOut(BaseModel):
    filename: str
    competitor_count: int
    download_url: str


@router.post("/export", response_model=ReportOut)
async def export_report(
    payload: ReportRequest, session: AsyncSession = Depends(get_session)
) -> ReportOut:
    """生成 Markdown 分析报告（汇总竞品档案 + 评分对比 + 规则结论）。"""
    competitors = await report_service.collect_competitors(session, payload.ids)
    if not competitors:
        raise HTTPException(status_code=404, detail="没有可导出的已入库竞品")

    dims = await get_rating_dimensions(session)
    markdown = report_service.build_markdown(competitors, dims)
    filename = report_service.save_report(markdown)

    return ReportOut(
        filename=filename,
        competitor_count=len(competitors),
        download_url=report_service.download_url(filename),
    )


@router.get("/download/{filename}")
async def download_report(filename: str) -> FileResponse:
    """下载已生成的报告文件。"""
    try:
        path: Path = report_service.resolve_report_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="非法文件名") from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="报告文件不存在") from e
    return FileResponse(path, media_type="text/markdown", filename=filename)
