"""Integration tests verifying Alembic migrations and PostgreSQL 16 constraints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from codesight.db.models import PRReview, Repository, ReviewFinding, WebhookEvent

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_alembic_config(dsn: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.attributes["configure_logger"] = False
    return cfg


def _inspect_schema(sync_conn: sa.Connection) -> dict[str, Any]:
    inspector = sa.inspect(sync_conn)
    tables = set(inspector.get_table_names())
    indexes: dict[str, set[str]] = {}
    uniques: dict[str, set[str]] = {}
    for table in ("repositories", "webhook_events", "pr_reviews", "review_findings"):
        if table in tables:
            indexes[table] = {
                idx["name"] for idx in inspector.get_indexes(table) if idx["name"] is not None
            }
            uniques[table] = {
                uq["name"]
                for uq in inspector.get_unique_constraints(table)
                if uq["name"] is not None
            }
    return {"tables": tables, "indexes": indexes, "uniques": uniques}


@pytest.mark.asyncio
async def test_alembic_upgrade_constraints_cascade_and_downgrade(
    isolated_postgres_dsn: str,
) -> None:
    cfg = _make_alembic_config(isolated_postgres_dsn)
    command.upgrade(cfg, "head")

    engine = create_async_engine(isolated_postgres_dsn)
    try:
        async with engine.connect() as conn:
            info = await conn.run_sync(_inspect_schema)
            version = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))

        assert version == "0001_initial_schema"
        assert {
            "repositories",
            "webhook_events",
            "pr_reviews",
            "review_findings",
            "alembic_version",
        } <= info["tables"]
        assert "ix_repositories_installation_id" in info["indexes"]["repositories"]
        assert "uq_repositories_github_repo_id" in info["uniques"]["repositories"]
        assert "uq_repositories_full_name" in info["uniques"]["repositories"]
        assert "ix_webhook_events_received_at" in info["indexes"]["webhook_events"]
        assert {
            "ix_pr_reviews_repo_pr_created",
            "ix_pr_reviews_repo_head_sha",
            "ix_pr_reviews_created_at",
        } <= info["indexes"]["pr_reviews"]
        assert {
            "ix_review_findings_pr_review_id",
            "ix_review_findings_file_path",
            "ix_review_findings_accepted_on_merge",
        } <= info["indexes"]["review_findings"]

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # Verify CHECK constraints reject out-of-range values
        async with session_factory() as session:
            repo = Repository(full_name="Tieens-04/CodeSight-AI", github_repo_id=101)
            session.add(repo)
            await session.flush()

            session.add(
                PRReview(
                    repo_id=repo.id,
                    pr_number=1,
                    head_sha="abc123",
                    debt_score=105,
                    status="completed",
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with session_factory() as session:
            repo = Repository(full_name="Tieens-04/CheckLineOrder", github_repo_id=102)
            session.add(repo)
            await session.flush()
            review = PRReview(
                repo_id=repo.id,
                pr_number=2,
                head_sha="def456",
                debt_score=82,
                status="completed",
            )
            session.add(review)
            await session.flush()

            session.add(
                ReviewFinding(
                    pr_review_id=review.id,
                    file_path="src/app.py",
                    start_line=20,
                    end_line=10,
                    severity="CRITICAL",
                    category="security",
                    confidence=0.95,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with session_factory() as session:
            session.add(
                WebhookEvent(
                    delivery_id="deliv-bad-status",
                    event_type="pull_request",
                    action="opened",
                    status="bogus",
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        # Verify valid cascade delete from Repository -> PRReview -> ReviewFinding
        async with session_factory() as session:
            repo = Repository(
                full_name="Tieens-04/CascadeRepo",
                github_repo_id=103,
                installation_id=5001,
            )
            session.add(repo)
            await session.flush()
            review = PRReview(
                repo_id=repo.id,
                pr_number=3,
                head_sha="999aaa",
                debt_score=90,
                status="completed",
                trace_id="trace-cascade-1",
            )
            session.add(review)
            await session.flush()
            finding = ReviewFinding(
                pr_review_id=review.id,
                file_path="src/db.py",
                start_line=5,
                end_line=7,
                severity="WARNING",
                category="performance",
                confidence=0.88,
                message="Avoid N+1 query in loop.",
                suggestion_code="await session.execute(stmt)",
            )
            session.add(finding)
            await session.commit()

            await session.delete(repo)
            await session.commit()

            remaining_reviews = await session.scalar(
                sa.select(sa.func.count()).select_from(PRReview)
            )
            remaining_findings = await session.scalar(
                sa.select(sa.func.count()).select_from(ReviewFinding)
            )
            assert remaining_reviews == 0
            assert remaining_findings == 0
    finally:
        await engine.dispose()

    # Verify full reversible downgrade -> upgrade round-trip
    command.downgrade(cfg, "base")
    engine_after_down = create_async_engine(isolated_postgres_dsn)
    try:
        async with engine_after_down.connect() as conn:
            info_down = await conn.run_sync(_inspect_schema)
        assert {
            "repositories",
            "webhook_events",
            "pr_reviews",
            "review_findings",
        }.isdisjoint(info_down["tables"])
    finally:
        await engine_after_down.dispose()

    command.upgrade(cfg, "head")
    engine_after_up = create_async_engine(isolated_postgres_dsn)
    try:
        async with engine_after_up.connect() as conn:
            info_up = await conn.run_sync(_inspect_schema)
        assert {
            "repositories",
            "webhook_events",
            "pr_reviews",
            "review_findings",
        } <= info_up["tables"]
    finally:
        await engine_after_up.dispose()
