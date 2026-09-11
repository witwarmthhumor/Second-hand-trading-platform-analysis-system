"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

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
from app.diagnostics import collect_startup_warnings
from app.init_db import init_db
from app.logging_config import setup_logging
from app.metrics import get_metrics
from app.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.models import (  # noqa: F401  # 确保模型被注册到 Base
    AppSetting,
    ChatSession,
    Competitor,
    CompetitorChange,
    User,
)
from app.rate_limit import get_rate_limiter

# 尽早初始化日志：保证应用模块 import 阶段的日志也走统一格式
setup_logging()
logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_started_at: datetime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表 + seed 管理员 + 启动定时调度器。"""
    global _scheduler, _started_at
    await init_db()
    _started_at = datetime.now(UTC)

    # 启动 APScheduler：定时监控（F2，默认关闭；开启后执行真实的抓取-对比-落库）
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _monitor_job,
        "interval",
        hours=settings.monitor_interval_hours,
        id="competitor_monitor",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "应用启动完成",
        extra={
            "extra_fields": {
                "app": settings.app_name,
                "monitor_enabled": settings.monitor_enabled,
                "monitor_interval_hours": settings.monitor_interval_hours,
            }
        },
    )

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    logger.info("应用已停止")


async def _monitor_job() -> None:
    """定时监控任务（F2）：真实执行一轮「重抓 → 提炼 → 差异比对 → 落库变化」。

    默认关闭（``MONITOR_ENABLED=false``）：无人值守抓取会产生真实外网请求与 LLM 费用，
    必须显式开启。关闭时仅记一条 debug 日志，不做任何外部调用。
    """
    if not settings.monitor_enabled:
        logger.debug("监控未启用（MONITOR_ENABLED=false），跳过本轮")
        return

    from app.db import async_session_factory
    from app.services.monitor_service import run_monitor_cycle

    try:
        async with async_session_factory() as session:
            outcome = await run_monitor_cycle(session)
    except Exception:
        # 调度器里的异常若不吞掉会打印整段堆栈并可能影响后续调度，这里显式兜住
        logger.exception("监控轮次执行异常")
        return

    logger.info("监控轮次完成", extra={"extra_fields": outcome.as_dict()})


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# 请求中间件（注册顺序 = 由内到外）：限流在内层，先于业务短路返回 429；
# 上下文中间件在外层，保证被限流的请求也同样有 request_id、访问日志与指标。
# 两者都是纯 ASGI 实现，不破坏 SSE 流式。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestContextMiddleware)

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
    """健康检查：返回服务状态、数据库连通性与调度器状态。"""
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    scheduler_info: dict = {"running": False, "next_run": None}
    if _scheduler is not None:
        scheduler_info["running"] = _scheduler.running
        job = _scheduler.get_job("competitor_monitor")
        if job is not None and job.next_run_time is not None:
            scheduler_info["next_run"] = job.next_run_time.isoformat()

    return {
        "status": "ok",
        "database": "connected" if db_ok else "unavailable",
        "scheduler": scheduler_info,
        "uptime_seconds": (
            round((datetime.now(UTC) - _started_at).total_seconds(), 1)
            if _started_at
            else None
        ),
        "security_warnings": collect_startup_warnings(),
    }


@app.get("/metrics")
async def metrics() -> dict:
    """运行时可观测指标（请求计数/状态码分布/延迟统计 + 限流状态）。

    注：当前无鉴权，仅用于本机/内网运维观测；若对外暴露需在其前置一层访问控制。
    """
    return {**get_metrics().snapshot(), "rate_limit": get_rate_limiter().snapshot()}
