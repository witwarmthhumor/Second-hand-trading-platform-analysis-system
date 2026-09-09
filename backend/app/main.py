"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.changes import router as changes_router
from app.api.compare import router as compare_router
from app.api.competitors import router as competitors_router
from app.api.cost import router as cost_router
from app.api.fetch import router as fetch_router
from app.api.kb import router as kb_router
from app.api.rag import router as rag_router
from app.api.report import router as report_router
from app.api.settings import router as settings_router
from app.config import settings
from app.db import engine
from app.init_db import init_db
from app.models import (  # noqa: F401  # 确保模型被注册到 Base
    AppSetting,
    ChatSession,
    Competitor,
    CompetitorChange,
    User,
)

_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表 + seed 管理员 + 启动定时调度器。"""
    await init_db()

    # 启动 APScheduler：定时监控（每小时触发一次快照检查；骨架，实际抓取策略按需启用）
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _monitor_job,
        "interval",
        hours=1,
        id="competitor_monitor",
        replace_existing=True,
    )
    _scheduler.start()

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


async def _monitor_job() -> None:
    """定时监控任务：对已入库竞品做一次快照检查（占位，避免无人值守抓取副作用）。"""
    # 记录到日志即可；实际抓取对比策略待配置后启用
    from app.db import async_session_factory
    from app.services.change_service import list_stored_competitors

    async with async_session_factory() as session:
        _ = await list_stored_competitors(session)  # 触发一次查询，确认 DB 连通


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(auth_router)
app.include_router(competitors_router)
app.include_router(fetch_router)
app.include_router(compare_router)
app.include_router(settings_router)
app.include_router(report_router)
app.include_router(cost_router)
app.include_router(rag_router)
app.include_router(changes_router)
app.include_router(kb_router)


@app.get("/health")
async def health() -> dict:
    """健康检查：返回服务状态与数据库连通性。"""
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok",
        "database": "connected" if db_ok else "unavailable",
    }
