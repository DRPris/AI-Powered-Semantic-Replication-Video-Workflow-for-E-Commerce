"""add token usage cost tracking

Revision ID: 20260626_0002
Revises: 20260626_0001
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260626_0002"
down_revision: Union[str, None] = "20260626_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "token_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("call_type", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_token_usage_project_id", "token_usage", ["project_id"])
    op.create_index("ix_token_usage_stage", "token_usage", ["stage"])
    op.create_index("ix_token_usage_call_type", "token_usage", ["call_type"])
    op.create_index("ix_token_usage_model", "token_usage", ["model"])
    op.create_index("ix_token_usage_created_at", "token_usage", ["created_at"])
    op.create_index(
        "ix_token_usage_project_created",
        "token_usage",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_token_usage_stage_model",
        "token_usage",
        ["stage", "model"],
    )


def downgrade() -> None:
    op.drop_table("token_usage")
