"""Shared pytest fixtures for isolated temporary PostgreSQL test databases."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote_plus

import asyncpg
import pytest

from codesight.core.config import Settings


@pytest.fixture
async def isolated_postgres_dsn() -> AsyncIterator[str]:
    """Create a temporary isolated database on codesight-postgres (5433) and drop it on exit."""
    settings = Settings()
    db_name = f"codesight_test_{uuid.uuid4().hex[:8]}"
    admin_conn = await asyncpg.connect(dsn=settings.postgres_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    user = quote_plus(settings.postgres_user)
    password = quote_plus(settings.postgres_password.get_secret_value())
    temp_dsn = (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{db_name}"
    )
    try:
        yield temp_dsn
    finally:
        cleanup_conn = await asyncpg.connect(dsn=settings.postgres_dsn)
        try:
            await cleanup_conn.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await cleanup_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await cleanup_conn.close()
