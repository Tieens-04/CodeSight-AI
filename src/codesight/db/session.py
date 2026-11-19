"""Async SQLAlchemy 2.0 engine, session factory, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from codesight.core.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Build pooled asyncpg SQLAlchemy engine with pre-ping and bounded timeouts."""
    return create_async_engine(
        settings.sqlalchemy_async_dsn,
        pool_size=5,
        max_overflow=10,
        pool_timeout=5.0,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"timeout": 5.0, "command_timeout": 15.0},
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build async session factory avoiding implicit attribute expiry on commit."""
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a scoped AsyncSession from FastAPI application state."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized.",
        )
    typed_factory = cast(async_sessionmaker[AsyncSession], factory)
    async with typed_factory() as session:
        yield session
