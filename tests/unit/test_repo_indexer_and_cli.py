"""Unit tests for InitialRepoIndexer, IncrementalBranchSyncer, indexing_tasks, and CLI index."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from codesight.cli.commands.index import parse_github_repo_arg
from codesight.cli.main import app as cli_app
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.indexer import (
    MAX_INDEXABLE_BLOB_BYTES,
    AsyncRepoIndexLock,
    GitHubRepoIndexClient,
    IncrementalBranchSyncer,
    IndexProgressSnapshot,
    InitialRepoIndexer,
)
from codesight.rag.models import (
    EMBEDDING_DIMENSIONS,
    EmbeddingConfigError,
    QdrantZeroCodePoint,
    ZeroCodeViolationError,
)
from codesight.rag.qdrant_store import QdrantPointRef, QdrantZeroCodeStore
from codesight.vcs.github_auth import GitHubAuthConfigError, GitHubAuthError, GitHubAuthProvider
from codesight.worker.celery_app import handle_push_event, sync_default_branch
from codesight.worker.tasks.indexing_tasks import (
    PushSyncRunner,
    extract_push_coords,
    has_indexing_credentials,
    run_push_webhook_task,
)


def _b64(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.b64encode(raw).decode("ascii")


def _vec(seed: str) -> list[float]:
    arr = [0.01] * EMBEDDING_DIMENSIONS
    arr[sum(ord(c) for c in seed) % 32] = 5.0
    return arr


class _FakeGenAIClient:
    def __init__(self) -> None:
        self.batch_calls: int = 0
        self.embedded_inputs: list[str] = []
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        self.batch_calls += 1
        self.embedded_inputs.extend(contents)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=_vec(txt)) for txt in contents])


@dataclass
class _InMemoryZeroCodeStore:
    """In-memory fake matching QdrantZeroCodeStore's tenant-scoped contract."""

    points: dict[str, dict[str, Any]] = field(default_factory=dict)
    ensured_calls: int = 0
    delete_by_repo_calls: int = 0
    delete_by_file_calls: list[tuple[tuple[str, ...], frozenset[str]]] = field(default_factory=list)
    delete_by_ids_calls: list[tuple[str, ...]] = field(default_factory=list)

    async def ensure_collection(self) -> None:
        self.ensured_calls += 1

    async def get_existing_point_ids(
        self,
        point_ids: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
    ) -> set[str]:
        found: set[str] = set()
        for pid in point_ids:
            payload = self.points.get(pid)
            if payload is None:
                continue
            if repo_full_name is not None and payload.get("repo_full_name") != repo_full_name:
                continue
            if repo_id is not None and payload.get("repo_id") != repo_id:
                continue
            found.add(pid)
        return found

    @staticmethod
    def _matches_scope(
        payload: dict[str, Any],
        *,
        repo_full_name: str | None,
        repo_id: str | None,
        match_any_scope: bool,
    ) -> bool:
        if match_any_scope and repo_full_name is not None and repo_id is not None:
            return (
                payload.get("repo_full_name") == repo_full_name or payload.get("repo_id") == repo_id
            )
        if repo_full_name is not None and payload.get("repo_full_name") != repo_full_name:
            return False
        if repo_id is not None and payload.get("repo_id") != repo_id:
            return False
        return True

    async def upsert_points(self, points: Sequence[QdrantZeroCodePoint]) -> int:
        for pt in points:
            self.points[pt.point_id] = dict(pt.payload)
        return len(points)

    async def list_repo_point_refs(
        self,
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
    ) -> tuple[QdrantPointRef, ...]:
        refs: list[QdrantPointRef] = []
        for pid, payload in self.points.items():
            if not self._matches_scope(
                payload,
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                match_any_scope=match_any_scope,
            ):
                continue
            refs.append(
                QdrantPointRef(
                    point_id=pid,
                    file_path=str(payload["file_path"]),
                    blob_sha=str(payload["blob_sha"]),
                    repo_full_name=(
                        str(payload["repo_full_name"])
                        if payload.get("repo_full_name") is not None
                        else None
                    ),
                    repo_id=(
                        str(payload["repo_id"]) if payload.get("repo_id") is not None else None
                    ),
                )
            )
        return tuple(refs)

    async def has_legacy_slug_points(
        self,
        *,
        repo_id: str,
        expected_repo_full_name: str,
    ) -> bool:
        return any(
            payload.get("repo_id") == repo_id
            and payload.get("repo_full_name") != expected_repo_full_name
            for payload in self.points.values()
        )

    async def delete_points_by_ids(
        self,
        point_ids: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
    ) -> int:
        self.delete_by_ids_calls.append(tuple(point_ids))
        removed = 0
        for pid in point_ids:
            payload = self.points.get(pid)
            if payload is None:
                continue
            if not self._matches_scope(
                payload,
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                match_any_scope=match_any_scope,
            ):
                continue
            del self.points[pid]
            removed += 1
        return removed

    async def delete_by_file_paths(
        self,
        file_paths: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        exclude_point_ids: Sequence[str] = (),
    ) -> None:
        excluded = frozenset(exclude_point_ids)
        path_set = set(file_paths)
        self.delete_by_file_calls.append((tuple(sorted(path_set)), excluded))
        to_remove: list[str] = []
        for pid, payload in self.points.items():
            if pid in excluded:
                continue
            if repo_full_name is not None and payload.get("repo_full_name") != repo_full_name:
                continue
            if repo_id is not None and payload.get("repo_id") != repo_id:
                continue
            if payload.get("file_path") in path_set:
                to_remove.append(pid)
        for pid in to_remove:
            del self.points[pid]

    async def delete_by_repo(
        self,
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
    ) -> None:
        self.delete_by_repo_calls += 1
        to_remove = [
            pid
            for pid, payload in self.points.items()
            if self._matches_scope(
                payload,
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                match_any_scope=match_any_scope,
            )
        ]
        for pid in to_remove:
            del self.points[pid]


class _FakeRedisLockClient:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, token: str) -> int:
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def _make_settings(*, token: str = "ghp_test", gemini_key: str = "gem_test") -> Settings:
    return Settings(
        _env_file=None,
        github_token=SecretStr(token) if token else None,
        gemini_api_key=SecretStr(gemini_key) if gemini_key else None,
    )


def test_parse_github_repo_arg_accepts_slug_and_https_url_and_blocks_ssrf() -> None:
    assert parse_github_repo_arg("Tieens-04/CodeSight-AI") == "Tieens-04/CodeSight-AI"
    assert (
        parse_github_repo_arg("https://github.com/Tieens-04/CodeSight-AI")
        == "Tieens-04/CodeSight-AI"
    )
    assert (
        parse_github_repo_arg("https://www.github.com/Tieens-04/CodeSight-AI.git/")
        == "Tieens-04/CodeSight-AI"
    )

    for bad in [
        "",
        "only-owner",
        "http://github.com/org/repo",
        "https://evil.example.com/org/repo",
        "https://user:pass@github.com/org/repo",
        "https://github.com:8443/org/repo",
        "https://github.com/org",
        "https://github.com/../etc",
    ]:
        with pytest.raises(ValueError):
            parse_github_repo_arg(bad)


async def test_github_index_client_error_mapping_and_same_origin_guard() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/acme/rate"):
            return httpx.Response(429, headers={"Retry-After": "7"})
        if path.endswith("/repos/acme/server"):
            return httpx.Response(503)
        if path.endswith("/repos/acme/auth"):
            return httpx.Response(401)
        if path.endswith("/repos/acme/redirect"):
            return httpx.Response(302, headers={"Location": "https://evil.example.com/pwn"})
        if "/compare/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={})

    settings = _make_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        client = GitHubRepoIndexClient(auth, http_client=http)

        with pytest.raises(RateLimitTaskError) as rate_exc:
            await client.fetch_repo_metadata("acme/rate")
        assert rate_exc.value.retry_after_seconds == 7.0

        with pytest.raises(RetryableTaskError):
            await client.fetch_repo_metadata("acme/server")

        with pytest.raises(GitHubAuthError):
            await client.fetch_repo_metadata("acme/auth")

        with pytest.raises(RuntimeError, match="cross-origin"):
            await client.fetch_repo_metadata("acme/redirect")

        cmp_snap = await client.fetch_compare("acme/app", "aaa", "bbb")
        assert cmp_snap.not_found is True


async def test_async_repo_index_lock_serializes_and_releases_atomically() -> None:
    redis_client = _FakeRedisLockClient()
    lock = AsyncRepoIndexLock(
        redis_client,  # type: ignore[arg-type]
        ttl_seconds=60,
        acquire_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    async with lock.hold("repo-1"):
        assert "repo_index_lock:repo-1" in redis_client.store
        with pytest.raises(RetryableTaskError, match="busy"):
            async with lock.hold("repo-1"):
                pass

    assert "repo_index_lock:repo-1" not in redis_client.store


async def test_initial_repo_indexer_filters_skips_unchanged_and_prunes_stale_points() -> None:
    blob_requests: list[str] = []
    blobs: dict[str, bytes] = {
        "sha_py_v1": (b"def greet(name: str) -> str:\n    return f'hi {name}'\n"),
        "sha_py_v2": (b"def greet_v2(name: str) -> str:\n    return f'hello {name}'\n"),
        "sha_ts_v1": (b"export function add(a: number, b: number): number { return a + b; }\n"),
        "sha_md_v1": (b"# Architecture\nZero-Code Persistence stores only metadata.\n"),
        "sha_bin": b"\x00\x01\x02not-text",
        "sha_cfg": b"ignore_paths:\n  - generated/**\n",
    }
    current_tree = [
        {"path": "src/app.py", "mode": "100644", "type": "blob", "sha": "sha_py_v1", "size": 60},
        {"path": "web/math.ts", "mode": "100644", "type": "blob", "sha": "sha_ts_v1", "size": 70},
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "sha_md_v1", "size": 65},
        {"path": "src/empty.py", "mode": "100644", "type": "blob", "sha": "sha_empty", "size": 0},
        {"path": "src/link.py", "mode": "120000", "type": "blob", "sha": "sha_link", "size": 12},
        {
            "path": "src/huge.py",
            "mode": "100644",
            "type": "blob",
            "sha": "sha_huge",
            "size": MAX_INDEXABLE_BLOB_BYTES + 1,
        },
        {"path": "src/bin.py", "mode": "100644", "type": "blob", "sha": "sha_bin", "size": 12},
        {
            "path": "generated/auto.py",
            "mode": "100644",
            "type": "blob",
            "sha": "sha_py_v1",
            "size": 60,
        },
        {"path": "package-lock.json", "mode": "100644", "type": "blob", "sha": "sha_l", "size": 10},
        {"path": "CHANGELOG.md", "mode": "100644", "type": "blob", "sha": "sha_c", "size": 10},
    ]
    current_sha = "1111111111111111111111111111111111111111"
    truncated_flag = False

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/demo":
            return httpx.Response(
                200,
                json={"id": 42, "full_name": "acme/demo", "default_branch": "main"},
            )
        if path.startswith("/repos/acme/demo/commits/"):
            return httpx.Response(200, json={"sha": current_sha})
        if path == "/repos/acme/demo/contents/.codesight.yaml":
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": len(blobs["sha_cfg"]),
                    "encoding": "base64",
                    "content": _b64(blobs["sha_cfg"]),
                },
            )
        if path == "/repos/acme/demo/contents/.codesight.yml":
            return httpx.Response(404)
        if path.startswith("/repos/acme/demo/git/trees/"):
            return httpx.Response(
                200,
                json={"sha": current_sha, "truncated": truncated_flag, "tree": current_tree},
            )
        if path.startswith("/repos/acme/demo/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            blob_requests.append(sha)
            return httpx.Response(
                200,
                json={"sha": sha, "encoding": "base64", "content": _b64(blobs[sha])},
            )
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    store = _InMemoryZeroCodeStore()
    progress_events: list[IndexProgressSnapshot] = []

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            http_client=http,
            wave_size=2,
        )

        # Run 1: Initial full index
        report1 = await indexer.index_repository(
            "acme/demo",
            repo_id="repo-uuid-1",
            progress_callback=progress_events.append,
        )
        assert report1.commit_sha == current_sha
        assert report1.eligible_files == 4  # app.py, math.ts, README.md, bin.py (skipped on decode)
        assert report1.fetched_files == 4
        assert report1.unchanged_files_skipped == 0
        assert report1.code_chunks == 2
        assert report1.doc_chunks == 1
        assert report1.upserted_points == 3
        assert len(store.points) == 3
        assert len(progress_events) >= 1

        # Run 2: Re-index unchanged commit -> 0 blob downloads, 0 Gemini batch calls
        blob_requests.clear()
        calls_before = fake_genai.batch_calls
        report2 = await indexer.index_repository("acme/demo", repo_id="repo-uuid-1")
        assert report2.unchanged_files_skipped == 3
        assert report2.upserted_points == 0
        assert report2.stale_points_deleted == 0
        assert fake_genai.batch_calls == calls_before
        assert "sha_py_v1" not in blob_requests
        assert "sha_ts_v1" not in blob_requests

        # Run 3: Modify app.py and delete web/math.ts -> zero-downtime prune removes old symbols
        current_sha = "2222222222222222222222222222222222222222"
        current_tree[:] = [
            {
                "path": "src/app.py",
                "mode": "100644",
                "type": "blob",
                "sha": "sha_py_v2",
                "size": 64,
            },
            {
                "path": "README.md",
                "mode": "100644",
                "type": "blob",
                "sha": "sha_md_v1",
                "size": 65,
            },
        ]
        report3 = await indexer.index_repository("acme/demo", repo_id="repo-uuid-1")
        assert report3.unchanged_files_skipped == 1  # README.md skipped
        assert report3.upserted_points == 1  # greet_v2
        assert report3.stale_points_deleted == 2  # old greet + deleted add
        stored_symbols = {p["symbol_name"] for p in store.points.values()}
        assert stored_symbols == {"greet_v2", "Architecture"}

        # Run 4: Truncated tree must NOT delete paths absent from truncated tree
        truncated_flag = True
        current_tree[:] = [
            {
                "path": "src/app.py",
                "mode": "100644",
                "type": "blob",
                "sha": "sha_py_v2",
                "size": 64,
            }
        ]
        report4 = await indexer.index_repository("acme/demo", repo_id="repo-uuid-1")
        assert report4.truncated_tree is True
        assert report4.stale_points_deleted == 0
        assert {p["symbol_name"] for p in store.points.values()} == {"greet_v2", "Architecture"}

        # Run 5: --recreate wipes repo upfront
        truncated_flag = False
        report5 = await indexer.index_repository("acme/demo", repo_id="repo-uuid-1", recreate=True)
        assert store.delete_by_repo_calls == 1
        assert report5.upserted_points == 1
        assert {p["symbol_name"] for p in store.points.values()} == {"greet_v2"}


async def test_incremental_branch_syncer_handles_fast_path_rename_stale_and_fallbacks() -> None:
    blobs: dict[str, bytes] = {
        "sha_a_v1": b"def old_fn() -> int:\n    return 1\n",
        "sha_a_v2": b"def new_fn() -> int:\n    return 2\n",
        "sha_b_v1": b"def helper_b() -> int:\n    return 3\n",
        "sha_r_v2": b"def renamed_helper() -> int:\n    return 4\n",
        "sha_empty_mod": b"# only comment, no symbols\n",
    }
    compare_responses: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    branch_head_sha = "sha_2"
    tree_entries = [
        {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "sha_a_v1", "size": 40},
        {"path": "src/b.py", "mode": "100644", "type": "blob", "sha": "sha_b_v1", "size": 40},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/sync":
            return httpx.Response(
                200,
                json={"id": 99, "full_name": "acme/sync", "default_branch": "main"},
            )
        if path.startswith("/repos/acme/sync/commits/"):
            ref = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": branch_head_sha if ref == "main" else ref})
        if path.startswith("/repos/acme/sync/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith("/repos/acme/sync/git/trees/"):
            return httpx.Response(
                200,
                json={"sha": branch_head_sha, "truncated": False, "tree": tree_entries},
            )
        if path.startswith("/repos/acme/sync/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"sha": sha, "encoding": "base64", "content": _b64(blobs[sha])},
            )
        if "/compare/" in path:
            spec = path.split("/compare/", 1)[1]
            base, head = spec.split("...", 1)
            code, body = compare_responses[(base, head)]
            return httpx.Response(code, json=body)
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    store = _InMemoryZeroCodeStore()
    redis_client = _FakeRedisLockClient()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            http_client=http,
        )
        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=indexer.repo_client,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            redis_client=redis_client,  # type: ignore[arg-type]
        )

        # 1. Bootstrap when last_indexed_sha is None
        boot = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_1",
            last_indexed_sha=None,
            repo_id="repo-99",
        )
        assert boot.mode == "bootstrap_full_index"
        assert {p["symbol_name"] for p in store.points.values()} == {"old_fn", "helper_b"}

        # 2. No-op when already indexed
        noop = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_1",
            last_indexed_sha="sha_1",
            repo_id="repo-99",
        )
        assert noop.mode == "noop_already_indexed"

        # 3. Fast-path ahead: modify a.py + rename b.py -> c.py
        compare_responses[("sha_1", "sha_2")] = (
            200,
            {
                "status": "ahead",
                "total_commits": 1,
                "files": [
                    {"filename": "src/a.py", "status": "modified", "sha": "sha_a_v2"},
                    {
                        "filename": "src/c.py",
                        "previous_filename": "src/b.py",
                        "status": "renamed",
                        "sha": "sha_r_v2",
                    },
                ],
            },
        )
        fast = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_2",
            last_indexed_sha="sha_1",
            repo_id="repo-99",
        )
        assert fast.mode == "incremental_compare"
        assert {p["symbol_name"] for p in store.points.values()} == {"new_fn", "renamed_helper"}
        assert all(p["file_path"] != "src/b.py" for p in store.points.values())

        # 4. Out-of-order stale push ('behind' while branch HEAD is still sha_2)
        branch_head_sha = "sha_2"
        compare_responses[("sha_2", "sha_1")] = (
            200,
            {"status": "behind", "total_commits": 0, "files": []},
        )
        stale = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_1",
            last_indexed_sha="sha_2",
            repo_id="repo-99",
        )
        assert stale.mode == "skipped_stale_push"
        assert {p["symbol_name"] for p in store.points.values()} == {"new_fn", "renamed_helper"}

        # 5. True force-push revert ('behind' where branch HEAD really IS sha_1) -> fallback
        branch_head_sha = "sha_1"
        revert = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_1",
            last_indexed_sha="sha_2",
            repo_id="repo-99",
        )
        assert revert.mode == "fallback_full_reconcile"
        assert {p["symbol_name"] for p in store.points.values()} == {"old_fn", "helper_b"}

        # 6. Diverged / 404 / .codesight.yaml change triggers fallback cleanly
        compare_responses[("sha_1", "sha_3")] = (404, {"message": "Not Found"})
        branch_head_sha = "sha_3"
        fb404 = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_3",
            last_indexed_sha="sha_1",
            repo_id="repo-99",
        )
        assert fb404.mode == "fallback_full_reconcile"

        compare_responses[("sha_3", "sha_4")] = (
            200,
            {
                "status": "ahead",
                "total_commits": 1,
                "files": [
                    {"filename": ".codesight.yaml", "status": "modified", "sha": "sha_cfg"},
                ],
            },
        )
        branch_head_sha = "sha_4"
        fb_cfg = await syncer.sync_push(
            repo_full_name="acme/sync",
            after_sha="sha_4",
            last_indexed_sha="sha_3",
            repo_id="repo-99",
        )
        assert fb_cfg.mode == "fallback_full_reconcile"


async def test_push_task_helpers_and_celery_stub_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert extract_push_coords({"repo_id": "r1", "repo_full_name": "a/b", "after_sha": "abc"}) == (
        "r1",
        "a/b",
        "abc",
    )
    assert extract_push_coords({"repo_full_name": "a/b"}) is None

    assert has_indexing_credentials(_make_settings()) is True
    assert has_indexing_credentials(_make_settings(gemini_key="")) is False
    assert has_indexing_credentials(_make_settings(token="")) is False

    # Dev stub fallback remains backward compatible with Week 1 webhook tests
    res = handle_push_event({"repo_full_name": "org/repo", "ref": "refs/heads/main"})
    assert res["status"] == "accepted"

    # Production without credentials fails fast
    prod_settings = Settings(
        _env_file=None,
        app_env="production",
        postgres_password=SecretStr("real_prod_pw"),
    )
    with pytest.raises(GitHubAuthConfigError):
        await run_push_webhook_task(
            {
                "repo_id": str(uuid.uuid4()),
                "repo_full_name": "acme/prod",
                "after_sha": "abcdef123456",
            },
            settings=prod_settings,
        )

    # PushSyncRunner skips already processed delivery_id
    runner = PushSyncRunner(settings=_make_settings())

    async def _already_done(_delivery_id: str | None, *, trace_id: str) -> bool:
        return True

    monkeypatch.setattr(runner, "_is_delivery_already_processed", _already_done)
    skipped = await runner.execute(
        {
            "delivery_id": "deliv-1",
            "repo_id": str(uuid.uuid4()),
            "repo_full_name": "acme/demo",
            "after_sha": "abc123",
        }
    )
    assert skipped["status"] == "completed"


def test_cli_index_command_renders_summary_and_handles_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()

    # Invalid repo format -> exit code 2
    bad_res = runner.invoke(cli_app, ["index", "invalid-slug"])
    assert bad_res.exit_code == 2

    # Missing GEMINI_API_KEY -> exit code 2
    monkeypatch.setattr(
        "codesight.cli.commands.index.get_settings",
        lambda: _make_settings(gemini_key=""),
    )
    no_key_res = runner.invoke(cli_app, ["index", "acme/demo"])
    assert no_key_res.exit_code == 2

    # Happy path with non-default --branch warns and renders summary to stdout
    from codesight.rag.indexer import InitialIndexReport

    fake_report = InitialIndexReport(
        repo_full_name="acme/demo",
        repo_id="repo-uuid-1",
        default_branch="main",
        indexed_branch="feat/demo",
        commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        tree_entries=10,
        eligible_files=4,
        unchanged_files_skipped=1,
        fetched_files=3,
        skipped_files=6,
        code_chunks=5,
        doc_chunks=2,
        embedded_points=7,
        upserted_points=7,
        stale_points_deleted=0,
        persisted_cursor=False,
        truncated_tree=False,
        elapsed_ms=123.4,
    )

    async def _fake_run_cli_index(*_args: Any, **_kwargs: Any) -> InitialIndexReport:
        return fake_report

    monkeypatch.setattr(
        "codesight.cli.commands.index.get_settings",
        lambda: _make_settings(),
    )
    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _fake_run_cli_index)

    ok_res = runner.invoke(
        cli_app,
        ["index", "https://github.com/acme/demo.git", "--branch", "feat/demo"],
    )
    assert ok_res.exit_code == 0
    assert "acme/demo" in ok_res.stdout
    assert "abcdef123456" in ok_res.stdout

    # Progress callback invoked & runtime errors map to expected exit codes
    async def _progress_run(*_args: Any, **kwargs: Any) -> InitialIndexReport:
        cb = kwargs.get("progress_callback")
        if cb is not None:
            cb(
                IndexProgressSnapshot(
                    stage="indexing",
                    processed_files=3,
                    total_files=3,
                    upserted_points=7,
                    unchanged_files=1,
                )
            )
        return fake_report

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _progress_run)
    prog_res = runner.invoke(cli_app, ["index", "acme/demo"])
    assert prog_res.exit_code == 0

    async def _cfg_error_run(*_args: Any, **_kwargs: Any) -> InitialIndexReport:
        raise EmbeddingConfigError("Invalid Gemini API key")

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _cfg_error_run)
    assert runner.invoke(cli_app, ["index", "acme/demo"]).exit_code == 2

    from codesight.rag.indexer import RepoIndexError

    async def _runtime_error_run(*_args: Any, **_kwargs: Any) -> InitialIndexReport:
        raise RepoIndexError("GitHub 404 repo not found", status_code=404)

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _runtime_error_run)
    assert runner.invoke(cli_app, ["index", "acme/demo"]).exit_code == 1


async def test_qdrant_store_reconcile_helpers_and_missing_collection_guard() -> None:
    from qdrant_client.http.exceptions import UnexpectedResponse

    deleted_selectors: list[Any] = []
    scroll_pages = [
        (
            [
                SimpleNamespace(
                    id="11111111-1111-1111-1111-111111111111",
                    payload={"file_path": "./src/a.py", "blob_sha": "sha1"},
                )
            ],
            "offset-2",
        ),
        (
            [
                SimpleNamespace(
                    id="22222222-2222-2222-2222-222222222222",
                    payload={"file_path": "src/b.py", "blob_sha": "sha2"},
                )
            ],
            None,
        ),
    ]

    class _FakeQdrantClient:
        def __init__(self, *, missing: bool = False) -> None:
            self.missing = missing
            self.page_idx = 0

        async def scroll(self, **_kwargs: Any) -> Any:
            if self.missing:
                raise UnexpectedResponse(404, "Not Found", b"", httpx.Headers())
            page = scroll_pages[self.page_idx]
            self.page_idx += 1
            return page

        async def delete(self, **kwargs: Any) -> None:
            if self.missing:
                raise UnexpectedResponse(404, "Not Found", b"", httpx.Headers())
            deleted_selectors.append(kwargs["points_selector"])

    store = QdrantZeroCodeStore(
        settings=_make_settings(),
        client=_FakeQdrantClient(),  # type: ignore[arg-type]
    )
    refs = await store.list_repo_point_refs(repo_full_name="acme/demo")
    assert len(refs) == 2
    assert refs[0].file_path == "src/a.py"
    assert refs[1].blob_sha == "sha2"

    assert await store.delete_points_by_ids([], repo_full_name="acme/demo") == 0
    removed = await store.delete_points_by_ids(
        [
            "11111111-1111-1111-1111-111111111111",
            "11111111-1111-1111-1111-111111111111",
        ],
        repo_full_name="acme/demo",
    )
    assert removed == 1
    assert len(deleted_selectors) == 1

    await store.delete_by_file_paths(
        ["src/a.py"],
        repo_full_name="acme/demo",
        exclude_point_ids=["22222222-2222-2222-2222-222222222222"],
    )
    assert deleted_selectors[-1].filter.must_not is not None

    with pytest.raises(ZeroCodeViolationError):
        await store.delete_points_by_ids(["not-a-uuid"], repo_full_name="acme/demo")

    missing_store = QdrantZeroCodeStore(
        settings=_make_settings(),
        client=_FakeQdrantClient(missing=True),  # type: ignore[arg-type]
    )
    assert await missing_store.list_repo_point_refs(repo_full_name="acme/demo") == ()
    assert (
        await missing_store.delete_points_by_ids(
            ["11111111-1111-1111-1111-111111111111"],
            repo_full_name="acme/demo",
        )
        == 0
    )


async def test_blob_edge_cases_subchunk_snippet_identical_compare_and_lock_fallback() -> None:
    import redis.exceptions

    # 1. Lock fallback without eval and fail-open on Redis error
    class _NoEvalRedis:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            self.data[key] = value
            return True

        async def get(self, key: str) -> bytes | None:
            val = self.data.get(key)
            return val.encode("utf-8") if val is not None else None

        async def delete(self, key: str) -> None:
            self.data.pop(key, None)

    no_eval = _NoEvalRedis()
    async with AsyncRepoIndexLock(no_eval).hold("repo-no-eval"):  # type: ignore[arg-type]
        assert "repo_index_lock:repo-no-eval" in no_eval.data
    assert "repo_index_lock:repo-no-eval" not in no_eval.data

    class _BrokenRedis:
        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            raise redis.exceptions.ConnectionError("redis down")

    async with AsyncRepoIndexLock(_BrokenRedis()).hold("repo-broken"):  # type: ignore[arg-type]
        pass

    # 2. Blob client handles 404, oversize, bad base64, transport error, and long sub-chunk snippet
    long_py_lines = ["def giant_func(x: int) -> int:"]
    long_py_lines.extend([f"    x += {i}" for i in range(1, 180)])
    long_py_lines.append("    return x")
    long_py_src = "\n".join(long_py_lines).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/blobs/missing"):
            return httpx.Response(404)
        if path.endswith("/git/blobs/oversize"):
            return httpx.Response(
                200,
                json={"size": MAX_INDEXABLE_BLOB_BYTES + 10, "encoding": "base64", "content": ""},
            )
        if path.endswith("/git/blobs/badb64"):
            return httpx.Response(200, json={"size": 4, "encoding": "base64", "content": "!!!"})
        if path.endswith("/git/blobs/transport"):
            raise httpx.ConnectTimeout("timeout")
        if path.endswith("/git/blobs/longpy"):
            return httpx.Response(
                200,
                json={"size": len(long_py_src), "encoding": "base64", "content": _b64(long_py_src)},
            )
        if path == "/repos/acme/sub":
            return httpx.Response(
                200, json={"id": 7, "full_name": "acme/sub", "default_branch": "main"}
            )
        if path.startswith("/repos/acme/sub/commits/"):
            return httpx.Response(200, json={"sha": "c" * 40})
        if path.startswith("/repos/acme/sub/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith("/repos/acme/sub/git/trees/"):
            return httpx.Response(
                200,
                json={
                    "sha": "c" * 40,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/giant.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "longpy",
                            "size": len(long_py_src),
                        }
                    ],
                },
            )
        if "/compare/" in path:
            return httpx.Response(
                200, json={"status": "identical", "total_commits": 0, "files": []}
            )
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    store = _InMemoryZeroCodeStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        repo_client = GitHubRepoIndexClient(auth, http_client=http)
        assert await repo_client.fetch_blob_bytes("acme/sub", "missing") is None
        assert await repo_client.fetch_blob_bytes("acme/sub", "oversize") is None
        assert await repo_client.fetch_blob_bytes("acme/sub", "badb64") is None
        with pytest.raises(RetryableTaskError):
            await repo_client.fetch_blob_bytes("acme/sub", "transport")

        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            repo_client=repo_client,
            http_client=http,
        )
        rep = await indexer.index_repository("acme/sub", repo_id="r-sub")
        assert rep.code_chunks >= 2
        assert any("context:\n" in text for text in fake_genai.embedded_inputs)

        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=repo_client,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
        )
        identical_res = await syncer.sync_push(
            repo_full_name="acme/sub",
            after_sha="d" * 40,
            last_indexed_sha="c" * 40,
            repo_id="r-sub",
        )
        assert identical_res.mode == "noop_already_indexed"


async def test_push_sync_runner_failure_classification_and_celery_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    recorded_statuses: list[str] = []

    runner = PushSyncRunner(settings=_make_settings())

    async def _fake_record(_delivery_id: str | None, *, webhook_status: str, trace_id: str) -> None:
        recorded_statuses.append(webhook_status)

    monkeypatch.setattr(runner, "_record_webhook_status", _fake_record)

    with pytest.raises(ValueError, match="missing"):
        await runner.execute({"repo_full_name": "acme/demo"})

    # Retryable error on non-final attempt does NOT mark webhook failed
    async def _raise_retryable(*_args: Any, **_kwargs: Any) -> Any:
        raise RetryableTaskError("transient")

    monkeypatch.setattr(
        "codesight.worker.tasks.indexing_tasks.IncrementalBranchSyncer.sync_push",
        _raise_retryable,
    )
    payload = {
        "delivery_id": "deliv-err",
        "repo_id": str(uuid.uuid4()),
        "repo_full_name": "acme/demo",
        "after_sha": "abc1234",
    }
    with pytest.raises(RetryableTaskError):
        await runner.execute(payload, is_final_attempt=False)
    assert recorded_statuses == []

    # Final attempt marks webhook failed
    with pytest.raises(RetryableTaskError):
        await runner.execute(payload, is_final_attempt=True)
    assert recorded_statuses == ["failed"]

    # CancelledError marks webhook failed
    async def _raise_cancel(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "codesight.worker.tasks.indexing_tasks.IncrementalBranchSyncer.sync_push",
        _raise_cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await runner.execute(payload)
    assert recorded_statuses == ["failed", "failed"]

    # Dev stub on sync_default_branch and run_push_webhook_task
    assert (
        sync_default_branch({"repo_full_name": "org/repo", "ref": "refs/heads/main"})["status"]
        == "accepted"
    )
    stub_res = await run_push_webhook_task(
        {"repo_full_name": "org/repo"},
        settings=_make_settings(gemini_key=""),
    )
    assert stub_res["status"] == "accepted"


async def test_f01_out_of_order_diverged_push_and_non_default_ref_guard() -> None:
    blobs: dict[str, bytes] = {
        "sha_a_v1": b"def stale_a() -> int:\n    return 1\n",
        "sha_b_v1": b"def live_b() -> int:\n    return 2\n",
    }
    branch_head = "sha_B"
    trees: dict[str, list[dict[str, Any]]] = {
        "sha_A": [
            {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "sha_a_v1", "size": 36}
        ],
        "sha_B": [
            {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "sha_b_v1", "size": 35}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/f01":
            return httpx.Response(
                200, json={"id": 101, "full_name": "acme/f01", "default_branch": "main"}
            )
        if path.startswith("/repos/acme/f01/commits/"):
            ref = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": branch_head if ref == "main" else ref})
        if path.startswith("/repos/acme/f01/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith("/repos/acme/f01/git/trees/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": sha, "truncated": False, "tree": trees[sha]})
        if path.startswith("/repos/acme/f01/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"sha": sha, "encoding": "base64", "content": _b64(blobs[sha])}
            )
        if "/compare/" in path:
            spec = path.split("/compare/", 1)[1]
            base, head = spec.split("...", 1)
            if (base, head) == ("sha_B", "sha_A"):
                return httpx.Response(
                    200, json={"status": "diverged", "total_commits": 1, "files": []}
                )
            if (base, head) == ("sha_0", "sha_A"):
                return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    store = _InMemoryZeroCodeStore()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            http_client=http,
        )
        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=indexer.repo_client,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
        )

        # Seed index at live main HEAD sha_B
        await indexer.index_repository("acme/f01", ref="sha_B", repo_id="r-f01")
        assert {p["symbol_name"] for p in store.points.values()} == {"live_b"}

        # 1. Late retry of sha_A returning "diverged" while main HEAD is still sha_B -> skip!
        stale_div = await syncer.sync_push(
            repo_full_name="acme/f01",
            after_sha="sha_A",
            last_indexed_sha="sha_B",
            repo_id="r-f01",
        )
        assert stale_div.mode == "skipped_stale_push"
        assert stale_div.persisted_cursor is False
        assert {p["symbol_name"] for p in store.points.values()} == {"live_b"}

        # 2. compare_404 when main HEAD already moved to sha_B -> reconcile sha_B, not sha_A
        store.points.clear()
        reconciled = await syncer.sync_push(
            repo_full_name="acme/f01",
            after_sha="sha_A",
            last_indexed_sha="sha_0",
            repo_id="r-f01",
        )
        assert reconciled.mode == "fallback_full_reconcile"
        assert reconciled.after_sha == "sha_B"
        assert {p["symbol_name"] for p in store.points.values()} == {"live_b"}

        # 3. Zero-SHA & non-default ref guard
        assert (
            extract_push_coords(
                {"repo_id": "r-f01", "repo_full_name": "acme/f01", "after_sha": "0" * 40}
            )
            is None
        )
        wrong_ref = await syncer.sync_push(
            repo_full_name="acme/f01",
            after_sha="sha_A",
            default_branch="main",
            pushed_ref="refs/heads/master",
            last_indexed_sha="sha_B",
            repo_id="r-f01",
        )
        assert wrong_ref.mode == "skipped_stale_push"
        assert {p["symbol_name"] for p in store.points.values()} == {"live_b"}


async def test_f02_tenant_scoped_existing_ids_repo_id_rotation_and_repo_rename() -> None:
    # 1. QdrantZeroCodeStore.get_existing_point_ids checks tenant payload when scoped
    pid_match = str(uuid.uuid4())
    pid_other_repo_id = str(uuid.uuid4())

    class _FakeQdrantClient:
        async def retrieve(self, **kwargs: Any) -> list[Any]:
            assert kwargs.get("with_payload") == ["repo_full_name", "repo_id"]
            return [
                SimpleNamespace(
                    id=pid_match,
                    payload={"repo_full_name": "acme/demo", "repo_id": "uuid-new"},
                ),
                SimpleNamespace(
                    id=pid_other_repo_id,
                    payload={"repo_full_name": "acme/demo", "repo_id": "uuid-old"},
                ),
            ]

    scoped_store = QdrantZeroCodeStore(
        settings=_make_settings(),
        client=_FakeQdrantClient(),  # type: ignore[arg-type]
    )
    existing = await scoped_store.get_existing_point_ids(
        [pid_match, pid_other_repo_id],
        repo_full_name="acme/demo",
        repo_id="uuid-new",
    )
    assert existing == {pid_match}

    # 2. InitialRepoIndexer re-upserts with new repo_id & IncrementalBranchSyncer catches rename
    src_a = b"def svc() -> int:\n    return 1\n"
    src_b = b"def other() -> int:\n    return 2\n"
    current_slug = "acme/demo"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{current_slug}":
            return httpx.Response(
                200, json={"id": 202, "full_name": current_slug, "default_branch": "main"}
            )
        if path.startswith(f"/repos/{current_slug}/commits/"):
            return httpx.Response(200, json={"sha": "sha_cur"})
        if path.startswith(f"/repos/{current_slug}/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith(f"/repos/{current_slug}/git/trees/"):
            return httpx.Response(
                200,
                json={
                    "sha": "sha_cur",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/svc.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob_a",
                            "size": len(src_a),
                        },
                        {
                            "path": "src/other.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob_b",
                            "size": len(src_b),
                        },
                    ],
                },
            )
        if path.endswith("/git/blobs/blob_a"):
            return httpx.Response(
                200, json={"sha": "blob_a", "encoding": "base64", "content": _b64(src_a)}
            )
        if path.endswith("/git/blobs/blob_b"):
            return httpx.Response(
                200, json={"sha": "blob_b", "encoding": "base64", "content": _b64(src_b)}
            )
        if "/compare/" in path:
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "total_commits": 1,
                    "files": [{"filename": "src/other.py", "status": "modified", "sha": "blob_b"}],
                },
            )
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    mem_store = _InMemoryZeroCodeStore()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
            http_client=http,
        )
        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=indexer.repo_client,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
        )

        # Seed under old repo_id="uuid-old"
        await indexer.index_repository("acme/demo", repo_id="uuid-old")
        assert all(p.get("repo_id") == "uuid-old" for p in mem_store.points.values())

        # Re-index same slug under rotated repo_id="uuid-new" -> must update payload repo_id!
        await indexer.index_repository("acme/demo", repo_id="uuid-new")
        assert mem_store.points
        assert all(p.get("repo_id") == "uuid-new" for p in mem_store.points.values())

        # Repo renamed on GitHub: acme/demo -> acme/renamed (same repo_id="uuid-new")
        current_slug = "acme/renamed"
        rename_rep = await syncer.sync_push(
            repo_full_name="acme/renamed",
            after_sha="sha_cur",
            last_indexed_sha="sha_prev",
            repo_id="uuid-new",
        )
        assert rename_rep.mode == "fallback_full_reconcile"
        assert mem_store.points
        assert all(p.get("repo_full_name") == "acme/renamed" for p in mem_store.points.values())


async def test_f03_upsert_repository_state_handles_recreated_and_renamed_collisions() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.exc import IntegrityError

    from codesight.db.models import Repository
    from codesight.rag.indexer import upsert_repository_state

    rows: list[Repository] = [
        Repository(
            id=uuid.uuid4(),
            github_repo_id=100,
            full_name="acme/api",
            default_branch="main",
            last_indexed_sha="old_sha_100",
        ),
        Repository(
            id=uuid.uuid4(),
            github_repo_id=200,
            full_name="acme/old-slug",
            default_branch="main",
            last_indexed_sha="old_sha_200",
        ),
    ]

    class _FakeScalarResult:
        def __init__(self, items: list[Repository]) -> None:
            self._items = items

        def scalar_one_or_none(self) -> Repository | None:
            return self._items[0] if self._items else None

        def scalars(self) -> _FakeScalarResult:
            return self

        def all(self) -> list[Repository]:
            return list(self._items)

    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _FakeSession:
        def __init__(self) -> None:
            self._pending: list[Repository] = []

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def begin_nested(self) -> _NoopCtx:
            return _NoopCtx()

        def add(self, obj: Repository) -> None:
            if obj.id is None:
                obj.id = uuid.uuid4()
            self._pending.append(obj)

        async def execute(self, stmt: Any) -> _FakeScalarResult:
            compiled = stmt.compile(dialect=postgresql.dialect())
            sql_text = str(compiled)
            params = compiled.params
            gh_id = next((v for k, v in params.items() if "github_repo_id" in k), None)
            slug_val = next((v for k, v in params.items() if "full_name" in k), None)
            matched: list[Repository] = []
            for r in rows:
                if "github_repo_id" in sql_text and "full_name" in sql_text:
                    if r.github_repo_id == gh_id or r.full_name == slug_val:
                        matched.append(r)
                elif "github_repo_id" in sql_text:
                    if r.github_repo_id == gh_id:
                        matched.append(r)
                elif "full_name" in sql_text:
                    if r.full_name == slug_val:
                        matched.append(r)
            return _FakeScalarResult(matched)

        async def flush(self) -> None:
            candidate = [*rows, *self._pending]
            seen_names: set[str] = set()
            seen_ids: set[int] = set()
            for r in candidate:
                if r.full_name in seen_names:
                    raise IntegrityError("stmt", {}, Exception("uq_repositories_full_name"))
                seen_names.add(r.full_name)
                if r.github_repo_id is not None:
                    if r.github_repo_id in seen_ids:
                        raise IntegrityError(
                            "stmt", {}, Exception("uq_repositories_github_repo_id")
                        )
                    seen_ids.add(r.github_repo_id)

        async def commit(self) -> None:
            for obj in self._pending:
                if obj not in rows:
                    rows.append(obj)
            self._pending.clear()

        async def rollback(self) -> None:
            self._pending.clear()

    factory = lambda: _FakeSession()  # noqa: E731

    # 1. Repo 200 renamed from acme/old-slug -> acme/api while stale repo 100 still holds acme/api
    renamed_id, renamed_sha = await upsert_repository_state(
        factory,  # type: ignore[arg-type]
        full_name="acme/api",
        default_branch="main",
        github_repo_id=200,
        last_indexed_sha="sha_200_new",
    )
    assert renamed_id == str(rows[1].id)
    assert renamed_sha == "sha_200_new"
    assert rows[0].full_name == "acme/api__stale_100"

    # 2. Recreated repo: same slug acme/api gets brand-new github_repo_id=300
    new_id, new_sha = await upsert_repository_state(
        factory,  # type: ignore[arg-type]
        full_name="acme/api",
        default_branch="main",
        github_repo_id=300,
        last_indexed_sha="sha_300_new",
    )
    assert new_id != str(rows[1].id)
    assert new_sha == "sha_300_new"
    assert rows[1].full_name == "acme/api__stale_200"


async def test_f04_qdrant_grpc_sqlalchemy_retry_classification_and_lock_retry_after() -> None:
    import grpc
    import sqlalchemy.exc as sa_exc
    from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

    from codesight.worker.lifecycle import classify_retryable_exception

    # 1. Lock contention carries retry_after_seconds=15.0 through classify_retryable_exception
    redis_client = _FakeRedisLockClient()
    lock = AsyncRepoIndexLock(
        redis_client,  # type: ignore[arg-type]
        ttl_seconds=90,
        acquire_timeout_seconds=0.02,
        poll_interval_seconds=0.005,
    )
    async with lock.hold("repo-lock-delay"):
        with pytest.raises(RetryableTaskError) as exc_info:
            async with lock.hold("repo-lock-delay"):
                pass
    assert getattr(exc_info.value, "retry_after_seconds", None) == 15.0
    assert classify_retryable_exception(exc_info.value) == (True, 15.0)

    # 2. Transient gRPC, Qdrant HTTP, and SQLAlchemy connection errors are retryable
    class _FakeGrpcError(grpc.RpcError):
        def __init__(self, status: grpc.StatusCode) -> None:
            self._status = status

        def code(self) -> grpc.StatusCode:
            return self._status

    assert classify_retryable_exception(_FakeGrpcError(grpc.StatusCode.UNAVAILABLE)) == (True, None)
    assert classify_retryable_exception(_FakeGrpcError(grpc.StatusCode.DEADLINE_EXCEEDED)) == (
        True,
        None,
    )
    assert classify_retryable_exception(_FakeGrpcError(grpc.StatusCode.INVALID_ARGUMENT)) == (
        False,
        None,
    )
    assert classify_retryable_exception(ResponseHandlingException(RuntimeError("drop"))) == (
        True,
        None,
    )
    assert classify_retryable_exception(
        UnexpectedResponse(503, "Service Unavailable", b"", httpx.Headers())
    ) == (True, None)
    assert classify_retryable_exception(
        UnexpectedResponse(400, "Bad Request", b"", httpx.Headers())
    ) == (False, None)
    assert classify_retryable_exception(
        sa_exc.OperationalError("SELECT 1", {}, Exception("conn reset"))
    ) == (True, None)
    assert classify_retryable_exception(sa_exc.TimeoutError("pool exhausted")) == (True, None)


async def test_f06_added_file_stale_cleanup_wave_sync_and_file_boundary_upsert() -> None:
    # 1. IncrementalBranchSyncer cleans stale orphan points when status == "added"
    new_py_v2 = b"def valid_fn() -> int:\n    return 42\n"
    compare_files = [{"filename": "src/new.py", "status": "added", "sha": "sha_new"}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/repos/acme/f06/contents/.codesight."):
            return httpx.Response(404)
        if "/compare/" in path:
            return httpx.Response(
                200, json={"status": "ahead", "total_commits": 1, "files": compare_files}
            )
        if path.endswith("/git/blobs/sha_new"):
            return httpx.Response(
                200, json={"sha": "sha_new", "encoding": "base64", "content": _b64(new_py_v2)}
            )
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    mem_store = _InMemoryZeroCodeStore()
    orphan_id = str(uuid.uuid4())
    mem_store.points[orphan_id] = {
        "repo_id": "repo-f06",
        "repo_full_name": "acme/f06",
        "file_path": "src/new.py",
        "blob_sha": "sha_orphan",
        "symbol_name": "orphan_fn",
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
            http_client=http,
        )
        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=indexer.repo_client,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
        )
        rep = await syncer.sync_push(
            repo_full_name="acme/f06",
            after_sha="sha_after",
            last_indexed_sha="sha_before",
            repo_id="repo-f06",
        )
        assert rep.mode == "incremental_compare"
        assert {p["symbol_name"] for p in mem_store.points.values()} == {"valid_fn"}

    # 2. QdrantZeroCodeStore.upsert_points respects file_path boundaries across batches
    batch_sizes: list[int] = []

    class _RecordingQdrantClient:
        async def upsert(self, **kwargs: Any) -> None:
            batch_sizes.append(len(kwargs["points"]))

    rec_store = QdrantZeroCodeStore(
        settings=settings,
        client=_RecordingQdrantClient(),  # type: ignore[arg-type]
    )

    def _pt(file_path: str, sym: str, line: int) -> QdrantZeroCodePoint:
        return QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=tuple(_vec(sym)),
            payload={
                "repo_id": "repo-f06",
                "repo_full_name": "acme/f06",
                "file_path": file_path,
                "blob_sha": "sha1",
                "chunk_type": "code",
                "language": "python",
                "symbol_name": sym,
                "symbol_kind": "function",
                "start_line": line,
                "end_line": line + 1,
                "signature": f"def {sym}() -> None",
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        )

    await rec_store.upsert_points(
        [
            _pt("src/a.py", "a1", 1),
            _pt("src/a.py", "a2", 5),
            _pt("src/b.py", "b1", 1),
            _pt("src/b.py", "b2", 5),
        ],
        batch_size=3,
    )
    assert batch_sizes == [2, 2]


async def test_f07_production_missing_creds_marks_failed_and_live_celery_runtime_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codesight.worker.celery_app import _WORKER_RUNTIME

    executed_stmts: list[Any] = []
    committed = False

    class _RecordingSession:
        async def __aenter__(self) -> _RecordingSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, stmt: Any) -> None:
            executed_stmts.append(stmt)

        async def commit(self) -> None:
            nonlocal committed
            committed = True

    prod_settings = Settings(
        _env_file=None,
        app_env="production",
        postgres_password=SecretStr("real_prod_pw"),
    )
    with pytest.raises(GitHubAuthConfigError):
        await run_push_webhook_task(
            {
                "delivery_id": "deliv-prod-missing",
                "repo_id": str(uuid.uuid4()),
                "repo_full_name": "acme/prod",
                "after_sha": "abcdef123456",
            },
            settings=prod_settings,
            session_factory=lambda: _RecordingSession(),  # type: ignore[arg-type]
        )
    assert committed is True
    assert len(executed_stmts) == 1

    # Exercise live handle_push_event & sync_default_branch dispatch through _WORKER_RUNTIME
    called_modes: list[tuple[str, bool]] = []

    async def _fake_execute(
        self: Any,
        payload: dict[str, Any],
        *,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        called_modes.append((str(payload["after_sha"]), is_final_attempt))
        return {"status": "completed", "mode": "incremental_compare", "trace_id": "t-live"}

    monkeypatch.setattr("codesight.worker.celery_app.get_settings", lambda: _make_settings())
    monkeypatch.setattr(PushSyncRunner, "execute", _fake_execute)

    live_payload = {
        "delivery_id": "deliv-live-1",
        "repo_id": str(uuid.uuid4()),
        "repo_full_name": "acme/live",
        "default_branch": "main",
        "ref": "refs/heads/main",
        "after_sha": "123456abcdef",
    }
    try:
        res1 = handle_push_event(live_payload)
        res2 = sync_default_branch(live_payload)
        assert res1["status"] == "completed"
        assert res2["status"] == "completed"
        assert len(called_modes) == 2
    finally:
        _WORKER_RUNTIME.shutdown()


async def test_f08_cli_non_default_branch_isolation_recreate_guard_and_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codesight.cli.commands.index import run_cli_index

    main_src = b"def main_only() -> int:\n    return 1\n"
    feat_src = b"def feat_only() -> int:\n    return 2\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/branch":
            return httpx.Response(
                200, json={"id": 808, "full_name": "acme/branch", "default_branch": "main"}
            )
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "sha_main"})
        if path.endswith("/commits/feat/demo") or path.endswith("/commits/feat%2Fdemo"):
            return httpx.Response(200, json={"sha": "sha_feat"})
        if path.startswith("/repos/acme/branch/contents/.codesight."):
            return httpx.Response(404)
        if path.endswith("/git/trees/sha_main"):
            return httpx.Response(
                200,
                json={
                    "sha": "sha_main",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/main.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "b_main",
                            "size": len(main_src),
                        }
                    ],
                },
            )
        if path.endswith("/git/trees/sha_feat"):
            return httpx.Response(
                200,
                json={
                    "sha": "sha_feat",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/feat.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "b_feat",
                            "size": len(feat_src),
                        }
                    ],
                },
            )
        if path.endswith("/git/blobs/b_main"):
            return httpx.Response(
                200, json={"sha": "b_main", "encoding": "base64", "content": _b64(main_src)}
            )
        if path.endswith("/git/blobs/b_feat"):
            return httpx.Response(
                200, json={"sha": "b_feat", "encoding": "base64", "content": _b64(feat_src)}
            )
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    store = _InMemoryZeroCodeStore()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=store,  # type: ignore[arg-type]
            http_client=http,
        )
        await indexer.index_repository("acme/branch", repo_id="r-b1")
        feat_rep = await indexer.index_repository("acme/branch", ref="feat/demo", repo_id="r-b1")
        assert feat_rep.persisted_cursor is False
        assert feat_rep.stale_points_deleted == 0
        assert {p["symbol_name"] for p in store.points.values()} == {"main_only", "feat_only"}

        with pytest.raises(ValueError, match="--recreate"):
            await indexer.index_repository(
                "acme/branch", ref="feat/demo", repo_id="r-b1", recreate=True
            )

    # Standalone run_cli_index closes owned Redis (in App mode), HTTP, DB, Store, and Embedder
    closed: list[str] = []
    auth_redis_passed: list[Any] = []

    class _FakeOwnedHttp:
        async def aclose(self) -> None:
            closed.append("http")

    class _FakeOwnedEngine:
        async def dispose(self) -> None:
            closed.append("engine")

    class _FakeOwnedStore:
        async def aclose(self) -> None:
            closed.append("store")

    class _FakeOwnedEmbedder:
        async def aclose(self) -> None:
            closed.append("embedder")

    class _FakeOwnedRedis:
        async def aclose(self) -> None:
            closed.append("redis")

    class _FakeAuth:
        def __init__(self, _settings: Any, **kwargs: Any) -> None:
            auth_redis_passed.append(kwargs.get("redis_client"))

    class _FakeIndexer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def index_repository(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(repo_full_name="acme/branch", persisted_cursor=True)

        async def aclose(self) -> None:
            closed.append("indexer")

    app_settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr("pem"),
        gemini_api_key=SecretStr("gem_test"),
    )
    monkeypatch.setattr(
        "codesight.cli.commands.index.httpx.AsyncClient", lambda **_k: _FakeOwnedHttp()
    )
    monkeypatch.setattr(
        "codesight.cli.commands.index.create_db_engine", lambda _s: _FakeOwnedEngine()
    )
    monkeypatch.setattr("codesight.cli.commands.index.create_session_factory", lambda _e: object())
    monkeypatch.setattr(
        "codesight.cli.commands.index.QdrantZeroCodeStore", lambda **_k: _FakeOwnedStore()
    )
    monkeypatch.setattr(
        "codesight.cli.commands.index.GeminiEmbedder", lambda **_k: _FakeOwnedEmbedder()
    )
    monkeypatch.setattr(
        "codesight.cli.commands.index.redis_async.from_url", lambda *_a, **_k: _FakeOwnedRedis()
    )
    monkeypatch.setattr("codesight.cli.commands.index.GitHubAuthProvider", _FakeAuth)
    monkeypatch.setattr("codesight.cli.commands.index.InitialRepoIndexer", _FakeIndexer)

    await run_cli_index("acme/branch", settings=app_settings)
    assert auth_redis_passed and auth_redis_passed[0] is not None
    assert set(closed) == {"indexer", "embedder", "store", "redis", "http", "engine"}


def test_f04_and_f08_cli_error_handling_and_stream_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grpc
    import sqlalchemy.exc as sa_exc
    from qdrant_client.http.exceptions import ResponseHandlingException

    from codesight.rag.indexer import InitialIndexReport

    runner = CliRunner()
    monkeypatch.setattr("codesight.cli.commands.index.get_settings", lambda: _make_settings())

    # 1. SQLAlchemyError, Qdrant ResponseHandlingException, and grpc.RpcError exit 1 on stderr
    async def _raise_sa(*_args: Any, **_kwargs: Any) -> Any:
        raise sa_exc.OperationalError("SELECT 1", {}, Exception("pg down"))

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _raise_sa)
    sa_res = runner.invoke(cli_app, ["index", "acme/demo"])
    assert sa_res.exit_code == 1
    assert isinstance(sa_res.exception, SystemExit)
    assert sa_res.stdout == ""
    assert "Indexing failed" in sa_res.stderr

    async def _raise_qd(*_args: Any, **_kwargs: Any) -> Any:
        raise ResponseHandlingException(RuntimeError("qdrant down"))

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _raise_qd)
    qd_res = runner.invoke(cli_app, ["index", "acme/demo"])
    assert qd_res.exit_code == 1
    assert isinstance(qd_res.exception, SystemExit)
    assert qd_res.stdout == ""
    assert "Indexing failed" in qd_res.stderr

    async def _raise_grpc(*_args: Any, **_kwargs: Any) -> Any:
        raise grpc.RpcError("grpc unavailable")

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _raise_grpc)
    grpc_res = runner.invoke(cli_app, ["index", "acme/demo"])
    assert grpc_res.exit_code == 1
    assert isinstance(grpc_res.exception, SystemExit)
    assert grpc_res.stdout == ""
    assert "Indexing failed" in grpc_res.stderr

    # 2. --branch + --recreate exits with code 2 on stderr and clean stdout
    async def _raise_val(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("--recreate is only supported on default branch")

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _raise_val)
    cli_err = runner.invoke(
        cli_app, ["index", "acme/branch", "--branch", "feat/demo", "--recreate"]
    )
    assert cli_err.exit_code == 2
    assert cli_err.stdout == ""
    assert "--recreate" in cli_err.stderr

    # 3. Non-default --branch warning goes to stderr while summary report goes to stdout
    non_default_report = InitialIndexReport(
        repo_full_name="acme/branch",
        repo_id="r-b1",
        default_branch="main",
        indexed_branch="feat/demo",
        commit_sha="sha_feat_123456",
        tree_entries=1,
        eligible_files=1,
        unchanged_files_skipped=0,
        fetched_files=1,
        skipped_files=0,
        code_chunks=1,
        doc_chunks=0,
        embedded_points=1,
        upserted_points=1,
        stale_points_deleted=0,
        persisted_cursor=False,
        truncated_tree=False,
        elapsed_ms=10.0,
    )

    async def _ok_non_default(*_args: Any, **_kwargs: Any) -> InitialIndexReport:
        return non_default_report

    monkeypatch.setattr("codesight.cli.commands.index.run_cli_index", _ok_non_default)
    ok_res = runner.invoke(cli_app, ["index", "acme/branch", "--branch", "feat/demo"])
    assert ok_res.exit_code == 0
    assert "Notice:" in ok_res.stderr
    assert "Notice:" not in ok_res.stdout
    assert "acme/branch" in ok_res.stdout


async def test_fr02_recreated_repo_rotates_qdrant_repo_id_and_skips_stale_repo_pushes() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.exc import IntegrityError

    from codesight.db.models import Repository
    from codesight.rag.indexer import read_repository_state

    old_uuid = uuid.uuid4()
    rows: list[Repository] = [
        Repository(
            id=old_uuid,
            github_repo_id=100,
            full_name="acme/api",
            default_branch="main",
            last_indexed_sha="sha_old",
        )
    ]

    class _FakeScalarResult:
        def __init__(self, items: list[Repository]) -> None:
            self._items = items

        def scalar_one_or_none(self) -> Repository | None:
            return self._items[0] if self._items else None

        def scalars(self) -> _FakeScalarResult:
            return self

        def all(self) -> list[Repository]:
            return list(self._items)

    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _FakeSession:
        def __init__(self) -> None:
            self._pending: list[Repository] = []

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def begin_nested(self) -> _NoopCtx:
            return _NoopCtx()

        def add(self, obj: Repository) -> None:
            if obj.id is None:
                obj.id = uuid.uuid4()
            self._pending.append(obj)

        async def execute(self, stmt: Any) -> _FakeScalarResult:
            compiled = stmt.compile(dialect=postgresql.dialect())
            sql_text = str(compiled)
            where_sql = sql_text.split("WHERE", 1)[1] if "WHERE" in sql_text else sql_text
            params = compiled.params
            id_val = next(
                (
                    v
                    for k, v in params.items()
                    if "github_repo_id" not in k and (k == "id" or k.startswith("id_"))
                ),
                None,
            )
            gh_id = next((v for k, v in params.items() if "github_repo_id" in k), None)
            slug_val = next((v for k, v in params.items() if "full_name" in k), None)
            matched: list[Repository] = []
            for r in rows:
                if "repositories.id" in where_sql:
                    if r.id == id_val:
                        matched.append(r)
                elif "github_repo_id" in where_sql and "full_name" in where_sql:
                    if r.github_repo_id == gh_id or r.full_name == slug_val:
                        matched.append(r)
                elif "github_repo_id" in where_sql:
                    if r.github_repo_id == gh_id:
                        matched.append(r)
                elif "full_name" in where_sql:
                    if r.full_name == slug_val:
                        matched.append(r)
            return _FakeScalarResult(matched)

        async def flush(self) -> None:
            candidate = [*rows, *self._pending]
            seen_names: set[str] = set()
            seen_ids: set[int] = set()
            for r in candidate:
                if r.full_name in seen_names:
                    raise IntegrityError("stmt", {}, Exception("uq_repositories_full_name"))
                seen_names.add(r.full_name)
                if r.github_repo_id is not None:
                    if r.github_repo_id in seen_ids:
                        raise IntegrityError(
                            "stmt", {}, Exception("uq_repositories_github_repo_id")
                        )
                    seen_ids.add(r.github_repo_id)

        async def commit(self) -> None:
            for obj in self._pending:
                if obj not in rows:
                    rows.append(obj)
            self._pending.clear()

        async def rollback(self) -> None:
            self._pending.clear()

    factory = lambda: _FakeSession()  # noqa: E731

    new_src = b"def fresh_service() -> int:\n    return 200\n"
    feat_src = b"def feat_branch_fn() -> int:\n    return 201\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/api":
            return httpx.Response(
                200,
                json={"id": 200, "full_name": "acme/api", "default_branch": "main"},
            )
        if path.endswith("/commits/main") or path.endswith("/commits/sha_200_v1"):
            return httpx.Response(200, json={"sha": "sha_200_v1"})
        if path.endswith("/commits/feat/check") or path.endswith("/commits/feat%2Fcheck"):
            return httpx.Response(200, json={"sha": "sha_feat_200"})
        if path.startswith("/repos/acme/api/contents/.codesight."):
            return httpx.Response(404)
        if path.endswith("/git/trees/sha_200_v1"):
            return httpx.Response(
                200,
                json={
                    "sha": "sha_200_v1",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/service.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob_new_200",
                            "size": len(new_src),
                        }
                    ],
                },
            )
        if path.endswith("/git/trees/sha_feat_200"):
            return httpx.Response(
                200,
                json={
                    "sha": "sha_feat_200",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/feat_only.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob_feat_200",
                            "size": len(feat_src),
                        }
                    ],
                },
            )
        if path.endswith("/git/blobs/blob_new_200"):
            return httpx.Response(
                200, json={"sha": "blob_new_200", "encoding": "base64", "content": _b64(new_src)}
            )
        if path.endswith("/git/blobs/blob_feat_200"):
            return httpx.Response(
                200,
                json={"sha": "blob_feat_200", "encoding": "base64", "content": _b64(feat_src)},
            )
        if "/compare/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    settings = _make_settings()
    fake_genai = _FakeGenAIClient()
    mem_store = _InMemoryZeroCodeStore()
    mem_store.points[str(uuid.uuid4())] = {
        "repo_id": str(old_uuid),
        "repo_full_name": "acme/api",
        "file_path": "src/old_only.py",
        "blob_sha": "blob_old",
        "symbol_name": "old_symbol",
    }
    redis_client = _FakeRedisLockClient()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
            http_client=http,
            session_factory=factory,  # type: ignore[arg-type]
        )
        fast_lock = AsyncRepoIndexLock(
            redis_client,  # type: ignore[arg-type]
            acquire_timeout_seconds=0.02,
            poll_interval_seconds=0.005,
        )
        syncer = IncrementalBranchSyncer(
            initial_indexer=indexer,
            repo_client=indexer.repo_client,
            embedder=embedder,
            store=mem_store,  # type: ignore[arg-type]
            session_factory=factory,  # type: ignore[arg-type]
            redis_client=redis_client,  # type: ignore[arg-type]
            lock=fast_lock,
        )

        # Sub-case A: Fallback full reconcile on recreated repo rotates repo_id BEFORE Qdrant upsert
        recreated_rep = await syncer.sync_push(
            repo_full_name="acme/api",
            after_sha="sha_old_retry",
            repo_id=str(old_uuid),
            github_repo_id=100,
        )
        assert recreated_rep.mode == "fallback_full_reconcile"
        assert len(rows) == 2
        stale_row = next(r for r in rows if r.id == old_uuid)
        new_row = next(r for r in rows if r.id != old_uuid)
        assert stale_row.full_name == "acme/api__stale_100"
        assert new_row.github_repo_id == 200
        assert new_row.full_name == "acme/api"
        assert new_row.last_indexed_sha == "sha_200_v1"
        assert recreated_rep.repo_id == str(new_row.id)
        assert mem_store.points
        assert all(p.get("repo_id") == str(new_row.id) for p in mem_store.points.values())
        assert {p.get("symbol_name") for p in mem_store.points.values()} == {"fresh_service"}

        # Exercise non-default branch indexing & direct read_repository_state helper
        feat_rep = await indexer.index_repository(
            "acme/api", ref="feat/check", repo_id=str(new_row.id)
        )
        assert feat_rep.persisted_cursor is False
        assert await read_repository_state(
            factory,  # type: ignore[arg-type]
            repo_id=str(new_row.id),
            repo_full_name="acme/api",
        ) == (str(new_row.id), "sha_200_v1", "main", None)

        # Sub-case B: Late Celery push carrying already-staled old_uuid must be skipped
        late_stale_rep = await syncer.sync_push(
            repo_full_name="acme/api",
            after_sha="sha_stale_late",
            repo_id=str(old_uuid),
            github_repo_id=100,
        )
        assert late_stale_rep.mode == "skipped_stale_push"
        assert late_stale_rep.persisted_cursor is False
        assert new_row.last_indexed_sha == "sha_200_v1"
        assert all(p.get("repo_id") == str(new_row.id) for p in mem_store.points.values())

        # Sub-case C: Push without repo_id but conflicting github_repo_id=100 vs active 200 skips
        conflict_rep = await syncer.sync_push(
            repo_full_name="acme/api",
            after_sha="sha_conflict",
            repo_id=None,
            github_repo_id=100,
        )
        assert conflict_rep.mode == "skipped_stale_push"
        assert conflict_rep.persisted_cursor is False

        # Sub-case D: Normalized slug.lower() lock blocks concurrent stale-UUID sync
        async with fast_lock.hold("acme/api"):
            with pytest.raises(RetryableTaskError) as lock_exc:
                await syncer.sync_push(
                    repo_full_name="Acme/API",
                    after_sha="sha_contended",
                    repo_id=str(old_uuid),
                )
        assert lock_exc.value.retry_after_seconds == 15.0
