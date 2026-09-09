"""应用启动初始化：建表 + seed 内置管理员。"""

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
    """建表并 seed 内置管理员。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_admin()
