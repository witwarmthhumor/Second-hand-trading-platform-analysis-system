"""baseline: 初始表结构

Revision ID: 02f9df80926d
Revises:
Create Date: 2026-09-09

初始完整表结构：users / chat_sessions / competitors / competitor_changes / app_settings。
手工编写（对齐 app.models），保证空库也能迁移到当前结构。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "02f9df80926d"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- users ----
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=False)

    # ---- chat_sessions ----
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"], unique=False)
    op.create_index("ix_chat_sessions_updated_at", "chat_sessions", ["updated_at"], unique=False)

    # ---- competitors ----
    op.create_table(
        "competitors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("positioning", sa.String(length=500), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("launch_status", sa.String(length=50), nullable=True),
        sa.Column("features", sa.Text(), nullable=True),
        sa.Column("target_users", sa.String(length=500), nullable=True),
        sa.Column("business_model", sa.String(length=500), nullable=True),
        sa.Column("pricing", sa.String(length=500), nullable=True),
        sa.Column("strengths", sa.Text(), nullable=True),
        sa.Column("weaknesses", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("ext_fields", sa.JSON(), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("source_raw", sa.Text(), nullable=True),
        sa.Column("data_status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_competitors_name", "competitors", ["name"], unique=False)
    op.create_index("ix_competitors_type", "competitors", ["type"], unique=False)
    op.create_index("ix_competitors_data_status", "competitors", ["data_status"], unique=False)

    # ---- competitor_changes ----
    op.create_table(
        "competitor_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("competitor_name", sa.String(length=200), nullable=False),
        sa.Column("field", sa.String(length=50), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_competitor_changes_competitor_id",
        "competitor_changes",
        ["competitor_id"],
        unique=False,
    )
    op.create_index("ix_competitor_changes_field", "competitor_changes", ["field"], unique=False)
    op.create_index(
        "ix_competitor_changes_changed_at",
        "competitor_changes",
        ["changed_at"],
        unique=False,
    )

    # ---- app_settings ----
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_competitor_changes_changed_at", table_name="competitor_changes")
    op.drop_index("ix_competitor_changes_field", table_name="competitor_changes")
    op.drop_index(
        "ix_competitor_changes_competitor_id", table_name="competitor_changes"
    )
    op.drop_table("competitor_changes")
    op.drop_index("ix_competitors_data_status", table_name="competitors")
    op.drop_index("ix_competitors_type", table_name="competitors")
    op.drop_index("ix_competitors_name", table_name="competitors")
    op.drop_table("competitors")
    op.drop_index("ix_chat_sessions_updated_at", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_user_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
