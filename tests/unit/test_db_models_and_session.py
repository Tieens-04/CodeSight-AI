"""Unit tests for SQLAlchemy 2.0 async ORM models and session factory."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codesight.core.config import Settings
from codesight.db.models import Base, PRReview, Repository, ReviewFinding, WebhookEvent
from codesight.db.session import create_db_engine, create_session_factory, get_db_session


def test_orm_metadata_defines_all_four_tables_and_forward_columns() -> None:
    assert set(Base.metadata.tables.keys()) == {
        "repositories",
        "webhook_events",
        "pr_reviews",
        "review_findings",
    }

    repo_cols = set(Repository.__table__.columns.keys())
    assert {
        "id",
        "github_repo_id",
        "installation_id",
        "full_name",
        "default_branch",
        "last_indexed_sha",
        "installed_at",
    } <= repo_cols
    assert Repository.__table__.columns["github_repo_id"].nullable is True

    pr_cols = set(PRReview.__table__.columns.keys())
    assert {
        "id",
        "repo_id",
        "pr_number",
        "head_sha",
        "debt_score",
        "critical_count",
        "warning_count",
        "info_count",
        "tokens_saved_pct",
        "latency_ms",
        "status",
        "trace_id",
        "created_at",
    } <= pr_cols

    finding_cols = set(ReviewFinding.__table__.columns.keys())
    assert {
        "id",
        "pr_review_id",
        "file_path",
        "start_line",
        "end_line",
        "severity",
        "category",
        "confidence",
        "message",
        "suggestion_code",
        "accepted_on_merge",
    } <= finding_cols

    wh_cols = set(WebhookEvent.__table__.columns.keys())
    assert wh_cols == {"delivery_id", "event_type", "action", "status", "received_at"}


@pytest.mark.asyncio
async def test_engine_pool_kwargs_and_get_db_session_lifecycle() -> None:
    settings = Settings(_env_file=None)
    with patch("codesight.db.session.create_async_engine") as mock_create:
        sentinel_engine = MagicMock()
        mock_create.return_value = sentinel_engine
        eng = create_db_engine(settings)
        assert eng is sentinel_engine
        mock_create.assert_called_once_with(
            settings.sqlalchemy_async_dsn,
            pool_size=5,
            max_overflow=10,
            pool_timeout=5.0,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"timeout": 5.0, "command_timeout": 15.0},
            echo=False,
        )

    fake_session = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = False
    factory = MagicMock(return_value=cm)
    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory))
    )

    yielded = [s async for s in get_db_session(fake_request)]  # type: ignore[arg-type]
    assert yielded == [fake_session]
    assert create_session_factory(sentinel_engine) is not None

    from fastapi import HTTPException

    uninitialized_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=None))
    )
    with pytest.raises(HTTPException) as exc_info:
        _ = [s async for s in get_db_session(uninitialized_request)]  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503
