"""add message_feedbacks 表

Revision ID: 3a1b9c2d
Revises: 02f9df80926d
Create Date: 2026-09-10

新增问答反馈表：记录用户对 AI 回答的 👍/👎，作为检索质量评测与 rerank 调参的数据来源。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "3a1b9c2d"
down_revision: Union[str, Sequence[str], None] = "02f9df80926d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_feedbacks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source_doc_ids", sa.JSON(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.session_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_message_feedbacks_user_id", "message_feedbacks", ["user_id"], unique=False
    )
    op.create_index(
        "ix_message_feedbacks_session_id",
        "message_feedbacks",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_feedbacks_session_id", table_name="message_feedbacks")
    op.drop_index("ix_message_feedbacks_user_id", table_name="message_feedbacks")
    op.drop_table("message_feedbacks")
