"""数据库连接：SQLAlchemy 异步 engine 与 session 工厂。

连接池调优（企业级基础配置）：
- pool_size=10 / max_overflow=20：异步场景默认 pool_size=5 偏小，放大以支撑并发
- pool_pre_ping=True：取连接前探活，避免 DB 重启后复用失效连接
- pool_recycle=1800：连接 30 分钟强制回收，规避 asyncpg 长连接被中间层断开
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


async def get_session() -> AsyncSession:
    """FastAPI 依赖：提供数据库会话。"""
    async with async_session_factory() as session:
        yield session
