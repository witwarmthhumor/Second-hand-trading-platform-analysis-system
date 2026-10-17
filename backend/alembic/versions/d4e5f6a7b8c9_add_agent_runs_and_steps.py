"""add agent_runs and agent_steps 表

Revision ID: d4e5f6a7b8c9
Revises: 3a1b9c2d
Create Date: 2026-09-17

新增多 Agent 竞品情报流水线的执行轨迹表：

- `agent_runs`：一次运行的状态、预算与实耗、起止时间、终止原因。
- `agent_steps`：该次运行的逐步轨迹，seq=0 为 Planner 任务书，
  按 (run_id, seq) 升序可还原完整决策链。

纯新增表，不改动既有表与数据；正文不入库（只存结构化摘要与证据标识）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "3a1b9c2d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("budget_cny", sa.Float(), nullable=False),
        sa.Column("cost_cny", sa.Float(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"], unique=False)
    op.create_index(
        "ix_agent_runs_started_at", "agent_runs", ["started_at"], unique=False
    )

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("cost_cny", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_agent_steps_run_id_seq", "agent_steps", ["run_id", "seq"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_steps_run_id_seq", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_table("agent_runs")
