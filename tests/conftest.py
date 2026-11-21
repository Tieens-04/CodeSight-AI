"""Shared pytest fixtures for isolated temporary PostgreSQL test databases and validators."""

from __future__ import annotations

import functools
import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest
from qdrant_client import AsyncQdrantClient

from codesight.core.config import Settings


@functools.lru_cache(maxsize=1)
def load_guard_validator() -> Any:
    """Load the github-diff-suggestion-guard validator script once per test session."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (
        repo_root
        / ".agents"
        / "skills"
        / "github-diff-suggestion-guard"
        / "scripts"
        / "validate_diff_and_payload.py"
    )
    spec = importlib.util.spec_from_file_location("validate_diff_and_payload", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def load_zerocode_auditor() -> Any:
    """Load the zerocode-ast-rag Qdrant NFR-3 auditor script once per test session."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (
        repo_root
        / ".agents"
        / "skills"
        / "zerocode-ast-rag"
        / "scripts"
        / "audit_qdrant_zerocode.py"
    )
    spec = importlib.util.spec_from_file_location("audit_qdrant_zerocode", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def guard_validator() -> Any:
    """Session-scoped fixture exposing the github-diff-suggestion-guard oracle."""
    return load_guard_validator()


@pytest.fixture(scope="session")
def zerocode_auditor() -> Any:
    """Session-scoped fixture exposing the zerocode-ast-rag NFR-3 audit script."""
    return load_zerocode_auditor()


@pytest.fixture
async def isolated_qdrant_collection() -> AsyncIterator[str]:
    """Yield an isolated Qdrant collection name on codesight-qdrant and delete it on exit."""
    settings = Settings()
    collection_name = f"{settings.qdrant_collection_name}_test_{uuid.uuid4().hex[:8]}"
    try:
        yield collection_name
    finally:
        client = AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_http_port,
            grpc_port=settings.qdrant_grpc_port,
            prefer_grpc=True,
            timeout=15,
        )
        try:
            if await client.collection_exists(collection_name):
                await client.delete_collection(collection_name)
        finally:
            await client.close()


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
