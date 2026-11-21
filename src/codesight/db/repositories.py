"""Shared SQLAlchemy 2.0 async helpers for collision-safe Repository state upserts."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from codesight.db.models import Repository


def stale_repo_name(full_name: str, suffix: str) -> str:
    """Build a bounded stale slug when a repository is renamed or recreated on GitHub."""
    tag = f"__stale_{suffix}"
    return f"{full_name[: max(0, 255 - len(tag))]}{tag}"


async def apply_repo_upsert(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
) -> Repository | None:
    """Resolve and update an existing Repository row, disambiguating stale slug collisions."""
    trimmed_sha = last_indexed_sha[:64] if last_indexed_sha is not None else None
    if github_repo_id is None:
        existing = (
            await session.execute(select(Repository).where(Repository.full_name == full_name))
        ).scalar_one_or_none()
        if existing is None:
            return None
        needs_update = (
            existing.default_branch != default_branch
            or (installation_id is not None and existing.installation_id != installation_id)
            or (trimmed_sha is not None and existing.last_indexed_sha != trimmed_sha)
        )
        if needs_update:
            existing.default_branch = default_branch
            if installation_id is not None:
                existing.installation_id = installation_id
            if trimmed_sha is not None:
                existing.last_indexed_sha = trimmed_sha
            await session.flush()
        return existing

    rows = list(
        (
            await session.execute(
                select(Repository).where(
                    or_(
                        Repository.github_repo_id == github_repo_id,
                        Repository.full_name == full_name,
                    )
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
        needs_update = (
            by_id.full_name != full_name
            or by_id.default_branch != default_branch
            or (installation_id is not None and by_id.installation_id != installation_id)
            or (trimmed_sha is not None and by_id.last_indexed_sha != trimmed_sha)
        )
        if needs_update:
            by_id.full_name = full_name
            by_id.default_branch = default_branch
            if installation_id is not None:
                by_id.installation_id = installation_id
            if trimmed_sha is not None:
                by_id.last_indexed_sha = trimmed_sha
            await session.flush()
        return by_id

    if by_name is not None:
        if by_name.github_repo_id is None:
            by_name.github_repo_id = github_repo_id
            by_name.default_branch = default_branch
            if installation_id is not None:
                by_name.installation_id = installation_id
            if trimmed_sha is not None:
                by_name.last_indexed_sha = trimmed_sha
            await session.flush()
            return by_name
        by_name.full_name = stale_repo_name(by_name.full_name, str(by_name.github_repo_id))
        await session.flush()
        return None

    return None


async def upsert_repository(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
    last_indexed_sha: str | None = None,
) -> Repository:
    """Idempotently upsert a Repository inside savepoints with IntegrityError retry."""
    trimmed_sha = last_indexed_sha[:64] if last_indexed_sha is not None else None
    for _ in range(3):
        try:
            async with session.begin_nested():
                updated = await apply_repo_upsert(
                    session,
                    github_repo_id=github_repo_id,
                    full_name=full_name,
                    default_branch=default_branch,
                    installation_id=installation_id,
                    last_indexed_sha=trimmed_sha,
                )
                if updated is not None:
                    return updated

                repo = Repository(
                    github_repo_id=github_repo_id,
                    full_name=full_name,
                    default_branch=default_branch,
                    installation_id=installation_id,
                    last_indexed_sha=trimmed_sha,
                )
                session.add(repo)
                await session.flush()
                return repo
        except IntegrityError:
            continue

    resolved = await apply_repo_upsert(
        session,
        github_repo_id=github_repo_id,
        full_name=full_name,
        default_branch=default_branch,
        installation_id=installation_id,
        last_indexed_sha=trimmed_sha,
    )
    if resolved is None:
        raise RuntimeError(f"Failed to upsert repository '{full_name}'.")
    return resolved
