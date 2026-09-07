"""FastAPI 应用入口。"""

from fastapi import FastAPI
from sqlalchemy import text

from app.config import settings
from app.db import engine

app = FastAPI(title=settings.app_name)


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
