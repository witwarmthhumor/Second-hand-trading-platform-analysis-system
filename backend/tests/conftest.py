"""pytest 共享配置：测试环境建表与清理。

关键设计：
- 测试使用独立数据库 competitor_test（在 _init_db 中自动创建），
  绝不触碰开发库 competitor —— 此前 conftest 直接对开发库 drop_all，
  曾导致开发数据被测试清空（2026-09-08 修复）。
- 测试库连接通过环境变量 DATABASE_URL 注入（必须在 import app 之前设置，
  pydantic-settings 中环境变量优先级高于 .env）。
- 所有 async 测试共享同一个 event loop（loop_scope=session，已在 pyproject 配置），
  避免模块级 async engine 连接池跨 loop 复用导致 "another operation is in progress"。
"""

import os

import asyncpg
import pytest

TEST_DB = "competitor_test"
ADMIN_URL = "postgresql://competitor:competitor@localhost:5433/competitor"
TEST_URL = f"postgresql+asyncpg://competitor:competitor@localhost:5433/{TEST_DB}"

# 必须在导入 app.* 之前设置，确保 engine 指向测试库
os.environ["DATABASE_URL"] = TEST_URL

from app.db import Base, engine  # noqa: E402
from app.models import (  # noqa: E402, F401  # 注册模型到 Base
    ChatSession,
    Competitor,
    CompetitorChange,
)


@pytest.fixture(scope="session", autouse=True)
async def _init_db():
    """确保测试库存在 → 建表（测试开始）→ 删表（测试结束）。"""
    # CREATE DATABASE 不能在事务内执行，用独立的 asyncpg 连接在管理库上执行
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
