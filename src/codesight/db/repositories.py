"""Shared SQLAlchemy 2.0 async helpers for collision-safe Repository state upserts."""

from __future__ import annotations

from typing import Final

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from codesight.db.models import Repository

_UNSET: Final[object] = object()


def stale_repo_name(full_name: str, suffix: str) -> str:
    """Build a bounded stale slug when a repository is renamed or recreated on GitHub."""
    tag = f"__stale_{suffix}"
    return f"{full_name[: max(0, 255 - len(tag))]}{tag}"


def _should_advance_sha(
    current_sha: str | None,
    *,
    trimmed_sha: str | None,
    expected_previous_sha: str | object | None,
) -> bool:
    """Return True when `last_indexed_sha` may transition from `current_sha` to `trimmed_sha`."""
    if trimmed_sha is None:
        return False
    if expected_previous_sha is _UNSET:
        return True
    expected_trimmed = (
        expected_previous_sha[:64] if isinstance(expected_previous_sha, str) else None
    )
    return current_sha == expected_trimmed


def _build_repo_select(where_clause: ColumnElement[bool], *, lock_row: bool) -> Select[Repository]:
    stmt = select(Repository).where(where_clause)
    if lock_row:
        return stmt.execution_options(populate_existing=True).with_for_update()
    return stmt


async def _apply_repo_upsert_guarded(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
    expected_previous_sha: str | object | None = _UNSET,
) -> tuple[Repository | None, bool]:
    """Resolve and update Repository row with optional CAS check on `last_indexed_sha`."""
    trimmed_sha = last_indexed_sha[:64] if last_indexed_sha is not None else None
    lock_row = expected_previous_sha is not _UNSET
    if github_repo_id is None:
        existing = (
            await session.execute(
                _build_repo_select(Repository.full_name == full_name, lock_row=lock_row)
            )
        ).scalar_one_or_none()
        if existing is None:
            return None, False
        advance_sha = _should_advance_sha(
            existing.last_indexed_sha,
            trimmed_sha=trimmed_sha,
            expected_previous_sha=expected_previous_sha,
        )
        needs_update = (
            existing.default_branch != default_branch
            or (installation_id is not None and existing.installation_id != installation_id)
            or (advance_sha and existing.last_indexed_sha != trimmed_sha)
        )
        if needs_update:
            existing.default_branch = default_branch
            if installation_id is not None:
                existing.installation_id = installation_id
            if advance_sha and trimmed_sha is not None:
                existing.last_indexed_sha = trimmed_sha
            await session.flush()
        return existing, advance_sha

    rows = list(
        (
            await session.execute(
                _build_repo_select(
                    or_(
                        Repository.github_repo_id == github_repo_id,
                        Repository.full_name == full_name,
                    ),
                    lock_row=lock_row,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = next((r for r in rows if r.github_repo_id == github_repo_id), None)
    by_name = next((r for r in rows if r.full_name == full_name), None)

    if by_id is not None:
        if by_name is not None and by_name.id != by_id.id:
            suffix = (
                str(by_name.github_repo_id)
                if by_name.github_repo_id is not None
                else str(by_name.id)[:8]
            )
            by_name.full_name = stale_repo_name(by_name.full_name, suffix)
            await session.flush()
        advance_sha = _should_advance_sha(
            by_id.last_indexed_sha,
            trimmed_sha=trimmed_sha,
            expected_previous_sha=expected_previous_sha,
        )
        needs_update = (
            by_id.full_name != full_name
            or by_id.default_branch != default_branch
            or (installation_id is not None and by_id.installation_id != installation_id)
            or (advance_sha and by_id.last_indexed_sha != trimmed_sha)
        )
        if needs_update:
            by_id.full_name = full_name
            by_id.default_branch = default_branch
            if installation_id is not None:
                by_id.installation_id = installation_id
            if advance_sha and trimmed_sha is not None:
                by_id.last_indexed_sha = trimmed_sha
            await session.flush()
        return by_id, advance_sha

    if by_name is not None:
        if by_name.github_repo_id is None:
            advance_sha = _should_advance_sha(
                by_name.last_indexed_sha,
                trimmed_sha=trimmed_sha,
                expected_previous_sha=expected_previous_sha,
            )
            by_name.github_repo_id = github_repo_id
            by_name.default_branch = default_branch
            if installation_id is not None:
                by_name.installation_id = installation_id
            if advance_sha and trimmed_sha is not None:
                by_name.last_indexed_sha = trimmed_sha
            await session.flush()
            return by_name, advance_sha
        by_name.full_name = stale_repo_name(by_name.full_name, str(by_name.github_repo_id))
        await session.flush()
        return None, False

    return None, False


async def apply_repo_upsert(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
    expected_previous_sha: str | object | None = _UNSET,
) -> Repository | None:
    """Resolve and update an existing Repository row, disambiguating stale slug collisions."""
    row, _ = await _apply_repo_upsert_guarded(
        session,
        github_repo_id=github_repo_id,
        full_name=full_name,
        default_branch=default_branch,
        installation_id=installation_id,
        last_indexed_sha=last_indexed_sha,
        expected_previous_sha=expected_previous_sha,
    )
    return row


async def upsert_repository_guarded(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
    expected_previous_sha: str | object | None = _UNSET,
) -> tuple[Repository, bool]:
    """Idempotently upsert Repository and return `(row, cursor_updated)` under savepoints."""
    trimmed_sha = last_indexed_sha[:64] if last_indexed_sha is not None else None
    for _ in range(3):
        try:
            async with session.begin_nested():
                updated, cursor_updated = await _apply_repo_upsert_guarded(
                    session,
                    github_repo_id=github_repo_id,
                    full_name=full_name,
                    default_branch=default_branch,
                    installation_id=installation_id,
                    last_indexed_sha=trimmed_sha,
                    expected_previous_sha=expected_previous_sha,
                )
                if updated is not None:
                    return updated, cursor_updated

                allow_insert_sha = _should_advance_sha(
                    None,
                    trimmed_sha=trimmed_sha,
                    expected_previous_sha=expected_previous_sha,
                )
                repo = Repository(
                    github_repo_id=github_repo_id,
                    full_name=full_name,
                    default_branch=default_branch,
                    installation_id=installation_id,
                    last_indexed_sha=trimmed_sha if allow_insert_sha else None,
                )
                session.add(repo)
                await session.flush()
                return repo, allow_insert_sha
        except IntegrityError:
            continue

    resolved, cursor_updated = await _apply_repo_upsert_guarded(
        session,
        github_repo_id=github_repo_id,
        full_name=full_name,
        default_branch=default_branch,
        installation_id=installation_id,
        last_indexed_sha=trimmed_sha,
        expected_previous_sha=expected_previous_sha,
    )
    if resolved is None:
        raise RuntimeError(f"Failed to upsert repository '{full_name}'.")
    return resolved, cursor_updated


async def upsert_repository(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
    expected_previous_sha: str | object | None = _UNSET,
) -> Repository:
    """Idempotently upsert a Repository inside savepoints with IntegrityError retry."""
    row, _ = await upsert_repository_guarded(
        session,
        github_repo_id=github_repo_id,
        full_name=full_name,
        default_branch=default_branch,
        installation_id=installation_id,
        last_indexed_sha=last_indexed_sha,
        expected_previous_sha=expected_previous_sha,
    )
    return row
