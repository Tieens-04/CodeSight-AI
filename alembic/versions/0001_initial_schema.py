"""Initial CodeSight-AI PostgreSQL 16 schema (4 core tables).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create repositories, webhook_events, pr_reviews, and review_findings."""
    op.create_table(
        "repositories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column(
            "default_branch",
            sa.String(length=128),
            server_default=sa.text("'main'"),
            nullable=False,
        ),
        sa.Column("last_indexed_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_repositories"),
        sa.UniqueConstraint("full_name", name="uq_repositories_full_name"),
        sa.UniqueConstraint("github_repo_id", name="uq_repositories_github_repo_id"),
    )
    op.create_index(
        "ix_repositories_installation_id",
        "repositories",
        ["installation_id"],
        unique=False,
    )

    op.create_table(
        "webhook_events",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('received', 'queued', 'ignored', 'processed', 'failed')",
            name="ck_webhook_events_status_valid",
        ),
        sa.PrimaryKeyConstraint("delivery_id", name="pk_webhook_events"),
    )
    op.create_index(
        "ix_webhook_events_received_at",
        "webhook_events",
        ["received_at"],
        unique=False,
    )

    op.create_table(
        "pr_reviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("debt_score", sa.Integer(), nullable=True),
        sa.Column(
            "critical_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "warning_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "info_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("tokens_saved_pct", sa.Double(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("pr_number > 0", name="ck_pr_reviews_pr_number_positive"),
        sa.CheckConstraint(
            "debt_score IS NULL OR (debt_score >= 0 AND debt_score <= 100)",
            name="ck_pr_reviews_debt_score_range",
        ),
        sa.CheckConstraint("critical_count >= 0", name="ck_pr_reviews_critical_count_nonneg"),
        sa.CheckConstraint("warning_count >= 0", name="ck_pr_reviews_warning_count_nonneg"),
        sa.CheckConstraint("info_count >= 0", name="ck_pr_reviews_info_count_nonneg"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_pr_reviews_latency_ms_nonneg",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'superseded', 'failed')",
            name="ck_pr_reviews_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repositories.id"],
            name="fk_pr_reviews_repo_id_repositories",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pr_reviews"),
    )
    op.create_index(
        "ix_pr_reviews_repo_pr_created",
        "pr_reviews",
        ["repo_id", "pr_number", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_pr_reviews_repo_head_sha",
        "pr_reviews",
        ["repo_id", "head_sha"],
        unique=False,
    )
    op.create_index(
        "ix_pr_reviews_created_at",
        "pr_reviews",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "review_findings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("pr_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("suggestion_code", sa.Text(), nullable=True),
        sa.Column("accepted_on_merge", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name="ck_review_findings_line_order",
        ),
        sa.CheckConstraint(
            "severity IN ('CRITICAL', 'WARNING', 'INFO')",
            name="ck_review_findings_severity_valid",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_review_findings_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["pr_review_id"],
            ["pr_reviews.id"],
            name="fk_review_findings_pr_review_id_pr_reviews",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_findings"),
    )
    op.create_index(
        "ix_review_findings_pr_review_id",
        "review_findings",
        ["pr_review_id"],
        unique=False,
    )
    op.create_index(
        "ix_review_findings_file_path",
        "review_findings",
        ["file_path"],
        unique=False,
    )
    op.create_index(
        "ix_review_findings_accepted_on_merge",
        "review_findings",
        ["accepted_on_merge"],
        unique=False,
    )


def downgrade() -> None:
    """Drop all four core tables in reverse foreign-key order."""
    op.drop_index("ix_review_findings_accepted_on_merge", table_name="review_findings")
    op.drop_index("ix_review_findings_file_path", table_name="review_findings")
    op.drop_index("ix_review_findings_pr_review_id", table_name="review_findings")
    op.drop_table("review_findings")

    op.drop_index("ix_pr_reviews_created_at", table_name="pr_reviews")
    op.drop_index("ix_pr_reviews_repo_head_sha", table_name="pr_reviews")
    op.drop_index("ix_pr_reviews_repo_pr_created", table_name="pr_reviews")
    op.drop_table("pr_reviews")

    op.drop_index("ix_webhook_events_received_at", table_name="webhook_events")
    op.drop_table("webhook_events")

    op.drop_index("ix_repositories_installation_id", table_name="repositories")
    op.drop_table("repositories")
