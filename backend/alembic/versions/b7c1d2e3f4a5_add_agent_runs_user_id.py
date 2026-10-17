"""agent_runs 增加归属用户列

Revision ID: b7c1d2e3f4a5
Revises: d4e5f6a7b8c9
Create Date: 2026-09-17

US-017（运行列表与详情）要求「无权访问他人运行记录返回 404」，而 agent_runs 原来
没有归属字段，做不了隔离。此处补 ``user_id``：

- 可空：开关上线前已产生的运行行没有归属，回放时只有管理员可见；
- 外键 ``users.id`` + CASCADE：与会话表同一口径，删用户连带清掉其运行记录；
- 建索引：列表接口按归属过滤后再按时间倒序取最近 20 条。

纯加列，不改动既有数据（已有行的 user_id 保持 NULL）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("user_id", sa.Integer(), nullable=True))
    # 与既有迁移一致：外键不命名（Base 未配置 naming_convention）
    op.create_foreign_key(
        None, "agent_runs", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_column("agent_runs", "user_id")
