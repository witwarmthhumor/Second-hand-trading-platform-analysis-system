"""pytest 共享配置：测试环境建表与清理。

- 所有 async 测试共享同一个 event loop（loop_scope=session，已在 pyproject 配置），
  避免模块级 async engine 连接池跨 loop 复用导致 "another operation is in progress"。
- 用真实 engine 在 session 开始时建表、结束时删表，保证幂等。
"""

import pytest

from app.db import Base, engine
from app.models import Competitor  # noqa: F401  # 注册模型到 Base


@pytest.fixture(scope="session", autouse=True)
async def _init_db():
    """建表（测试开始）→ 删表（测试结束）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
