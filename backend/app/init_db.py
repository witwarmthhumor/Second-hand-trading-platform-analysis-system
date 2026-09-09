"""应用启动初始化：数据库迁移 + seed 内置管理员。

生产/开发启动走 Alembic 迁移（增量升级表结构，不丢数据）；
测试环境（tests/conftest.py）仍用 Base.metadata.create_all 快速建表，互不影响。
"""

from sqlalchemy import select

from app.config import settings
from app.db import Base, async_session_factory, engine
from app.models.user import ROLE_ADMIN, User
from app.security import hash_password


async def seed_admin() -> None:
    """seed 内置管理员：仅在 users 表为空时创建，避免覆盖已有账号/改密。"""
    async with async_session_factory() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            session.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    role=ROLE_ADMIN,
                )
            )
            await session.commit()


async def init_db() -> None:
    """启动初始化：执行 Alembic 迁移（建表/升级）并 seed 管理员。"""
    await run_migrations()
    await seed_admin()


async def run_migrations() -> None:
    """编程式执行 Alembic upgrade head。

    在独立线程中运行（alembic 内部用 asyncio.run 建立自己的事件循环），
    避免与 FastAPI 主事件循环冲突。
    """
    import asyncio

    def _sync_upgrade() -> None:
        from pathlib import Path

        from alembic.config import Config

        from alembic import command

        backend_dir = Path(__file__).resolve().parents[1]
        cfg = Config(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        command.upgrade(cfg, "head")

    await asyncio.to_thread(_sync_upgrade)


async def create_all_for_tests() -> None:
    """测试专用：直接按模型建表（等价 create_all），不依赖 Alembic。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
