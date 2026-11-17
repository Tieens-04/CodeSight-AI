"""Self-destructing ephemeral workspace guard enforcing Zero-Code Persistence (NFR-3)."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final

import structlog

DEFAULT_EPHEMERAL_PREFIX: Final[str] = "codesight-ws-"

logger = structlog.get_logger("codesight.core.ephemeral")


def _create_workspace_dir(base_dir: Path | str | None, prefix: str) -> Path:
    dir_arg = str(base_dir) if base_dir is not None else None
    ws_path = Path(tempfile.mkdtemp(prefix=prefix, dir=dir_arg))
    if os.name != "nt":
        os.chmod(ws_path, 0o700)
    return ws_path


def _on_rmtree_error(func: Callable[..., Any], target_path: str, _exc: BaseException) -> None:
    if not os.path.islink(target_path):
        mode = stat.S_IRWXU if os.path.isdir(target_path) else (stat.S_IWRITE | stat.S_IREAD)
        os.chmod(target_path, mode)
    func(target_path)


def _cleanup_workspace_dir(ws_path: Path) -> None:
    if not ws_path.exists():
        return
    try:
        shutil.rmtree(ws_path, onexc=_on_rmtree_error)
    except OSError as exc:
        logger.error(
            "ephemeral_workspace_cleanup_failed",
            error_type=type(exc).__name__,
        )


def resolve_safe_subpath(workspace_root: Path, relative_path: str | Path) -> Path:
    """Resolve a workspace-relative child path, rejecting traversal and symlink escapes."""
    raw = str(relative_path)
    if "\x00" in raw:
        raise ValueError("Workspace subpath must not contain null bytes.")
    cleaned = raw.strip()
    if not cleaned or cleaned.startswith(("/", "\\")):
        raise ValueError("Workspace subpath must be a non-empty relative path.")

    normalized = cleaned.replace("\\", "/")
    win_probe = PureWindowsPath(cleaned)
    posix_probe = PurePosixPath(normalized)
    if win_probe.is_absolute() or win_probe.drive or posix_probe.is_absolute():
        raise ValueError("Workspace subpath must not be absolute.")
    if ".." in posix_probe.parts or posix_probe.parts in {(), (".",)}:
        raise ValueError("Workspace subpath traversal ('..') is forbidden.")

    root_resolved = workspace_root.resolve()
    candidate = (root_resolved / normalized).resolve()
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise ValueError("Resolved workspace subpath escapes workspace root.")
    return candidate


@contextmanager
def ephemeral_workspace(
    *,
    base_dir: Path | str | None = None,
    prefix: str = DEFAULT_EPHEMERAL_PREFIX,
) -> Iterator[Path]:
    """Create an isolated temporary directory and guarantee recursive deletion in finally."""
    ws_path = _create_workspace_dir(base_dir, prefix)
    try:
        yield ws_path
    finally:
        _cleanup_workspace_dir(ws_path)


@asynccontextmanager
async def async_ephemeral_workspace(
    *,
    base_dir: Path | str | None = None,
    prefix: str = DEFAULT_EPHEMERAL_PREFIX,
) -> AsyncIterator[Path]:
    """Async counterpart to ephemeral_workspace() offloading filesystem I/O via to_thread."""
    ws_path = await asyncio.to_thread(_create_workspace_dir, base_dir, prefix)
    try:
        yield ws_path
    finally:
        await asyncio.to_thread(_cleanup_workspace_dir, ws_path)
