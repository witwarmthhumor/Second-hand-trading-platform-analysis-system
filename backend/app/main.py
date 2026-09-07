"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api.competitors import router as competitors_router
from app.config import settings
from app.db import Base, engine
from app.models import Competitor  # noqa: F401  # 确保模型被注册到 Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表（阶段 1 用 create_all，规模增大后改 Alembic）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(competitors_router)


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
