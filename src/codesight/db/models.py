"""SQLAlchemy 2.0 async ORM models for CodeSight-AI durable state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base sharing deterministic PostgreSQL constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repository(Base):
    """Registered GitHub or CLI-indexed repository metadata (zero source code stored)."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    github_repo_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        unique=True,
    )
    installation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    default_branch: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        server_default=sa.text("'main'"),
    )
    last_indexed_sha: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    reviews: Mapped[list[PRReview]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WebhookEvent(Base):
    """Idempotent log of GitHub webhook deliveries keyed by X-GitHub-Delivery."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'queued', 'ignored', 'processed', 'failed')",
            name="status_valid",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    action: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        index=True,
    )


class PRReview(Base):
    """Summary metrics and execution status for an autonomous pull request review."""

    __tablename__ = "pr_reviews"
    __table_args__ = (
        CheckConstraint("pr_number > 0", name="pr_number_positive"),
        CheckConstraint(
            "debt_score IS NULL OR (debt_score >= 0 AND debt_score <= 100)",
            name="debt_score_range",
        ),
        CheckConstraint("critical_count >= 0", name="critical_count_nonneg"),
        CheckConstraint("warning_count >= 0", name="warning_count_nonneg"),
        CheckConstraint("info_count >= 0", name="info_count_nonneg"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="latency_ms_nonneg",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'superseded', 'failed')",
            name="status_valid",
        ),
        Index("ix_pr_reviews_repo_pr_created", "repo_id", "pr_number", "created_at"),
        Index("ix_pr_reviews_repo_head_sha", "repo_id", "head_sha"),
        Index("ix_pr_reviews_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    pr_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    head_sha: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    debt_score: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    critical_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=sa.text("0"),
    )
    warning_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=sa.text("0"),
    )
    info_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=sa.text("0"),
    )
    tokens_saved_pct: Mapped[float | None] = mapped_column(
        Double,
        nullable=True,
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    repository: Mapped[Repository] = relationship(back_populates="reviews")
    findings: Mapped[list[ReviewFinding]] = relationship(
        back_populates="pr_review",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ReviewFinding(Base):
    """Line-level review finding metadata and AI-synthesized suggestion block."""

    __tablename__ = "review_findings"
    __table_args__ = (
        CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name="line_order",
        ),
        CheckConstraint(
            "severity IN ('CRITICAL', 'WARNING', 'INFO')",
            name="severity_valid",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    pr_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pr_reviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        index=True,
    )
    start_line: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    end_line: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        Double,
        nullable=False,
    )
    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    suggestion_code: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    accepted_on_merge: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        index=True,
    )

    pr_review: Mapped[PRReview] = relationship(back_populates="findings")
