"""initial production schema

Revision ID: 20260626_0001
Revises:
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260626_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("airtable_record_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("original_video_url", sa.Text(), nullable=False),
        sa.Column("product_image_url", sa.Text(), nullable=False),
        sa.Column("product_listing_url", sa.Text(), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_record_id"),
        sa.UniqueConstraint("external_id"),
    )
    op.create_index("ix_projects_external_id", "projects", ["external_id"])
    op.create_index(
        "ix_projects_airtable_record_id", "projects", ["airtable_record_id"]
    )
    op.create_index("ix_projects_status", "projects", ["status"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("queue_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("result", json_type, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_queue_name", "jobs", ["queue_name"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index(
        "ix_jobs_queue_claim",
        "jobs",
        ["queue_name", "status", "lease_expires_at"],
    )

    op.create_table(
        "shots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("airtable_record_id", sa.String(64), nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("script", json_type, nullable=True),
        sa.Column("generation_prompt", json_type, nullable=True),
        sa.Column("keyframe_url", sa.Text(), nullable=True),
        sa.Column("generated_video_url", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("selected_model", sa.String(128), nullable=True),
        sa.Column("quality_scores", json_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_record_id"),
        sa.UniqueConstraint(
            "project_id", "sequence_number", name="uq_shot_sequence"
        ),
    )
    op.create_index("ix_shots_project_id", "shots", ["project_id"])
    op.create_index("ix_shots_status", "shots", ["status"])

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=True),
        sa.Column("asset_type", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assets_project_id", "assets", ["project_id"])
    op.create_index("ix_assets_shot_id", "assets", ["shot_id"])
    op.create_index("ix_assets_asset_type", "assets", ["asset_type"])

    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=True),
        sa.Column("review_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=True),
        sa.Column("rejection_reason", sa.String(128), nullable=True),
        sa.Column("severity", sa.String(32), nullable=True),
        sa.Column("suggested_action", sa.String(128), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_project_id", "reviews", ["project_id"])
    op.create_index("ix_reviews_shot_id", "reviews", ["shot_id"])
    op.create_index("ix_reviews_review_type", "reviews", ["review_type"])
    op.create_index("ix_reviews_status", "reviews", ["status"])

    op.create_table(
        "failure_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("shot_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("failure_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_failure_events_project_id", "failure_events", ["project_id"]
    )
    op.create_index("ix_failure_events_shot_id", "failure_events", ["shot_id"])
    op.create_index("ix_failure_events_job_id", "failure_events", ["job_id"])
    op.create_index("ix_failure_events_stage", "failure_events", ["stage"])
    op.create_index(
        "ix_failure_events_failure_type", "failure_events", ["failure_type"]
    )


def downgrade() -> None:
    op.drop_table("failure_events")
    op.drop_table("reviews")
    op.drop_table("assets")
    op.drop_table("shots")
    op.drop_table("jobs")
    op.drop_table("projects")
