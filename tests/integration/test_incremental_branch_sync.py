"""Live Docker integration tests for InitialRepoIndexer & IncrementalBranchSyncer (CA-14)."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from codesight.cli.commands.index import run_cli_index
from codesight.core.config import Settings
from codesight.db.models import Base, Repository, WebhookEvent
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import EMBEDDING_DIMENSIONS
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.worker.tasks.indexing_tasks import PushSyncRunner


def _sha1_blob(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
    ).hexdigest()


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _deterministic_vec(text: str) -> list[float]:
    vec = [0.01] * EMBEDDING_DIMENSIONS
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    for idx in range(16):
        vec[idx] = float(digest[idx] + 1) / 256.0
    return vec


class _DeterministicGenAIClient:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        self.batch_calls += 1
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_deterministic_vec(txt)) for txt in contents]
        )


def _build_100_file_fixture() -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    tree: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}

    def _add(path: str, content: bytes) -> None:
        sha = _sha1_blob(content)
        blobs[sha] = content
        tree.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": sha,
                "size": len(content),
            }
        )

    # 60 Python modules
    for i in range(60):
        src = (
            f'"""Module {i:02d}."""\n\n'
            f"def legacy_func_{i:02d}(value: int) -> int:\n"
            f'    """Compute score for module {i:02d}."""\n'
            f"    return value + {i}\n\n"
            f"def helper_call_{i:02d}(x: int) -> int:\n"
            f"    return legacy_func_{i:02d}(x) * 2\n"
        ).encode()
        _add(f"src/pkg/module_{i:02d}.py", src)

    # 30 TypeScript / TSX modules
    for i in range(30):
        ext = "tsx" if i % 2 == 0 else "ts"
        src = (
            f"export function OldWidget{i:02d}(count: number): number {{\n"
            f"  return count + {i};\n"
            f"}}\n"
        ).encode()
        _add(f"web/src/comp_{i:02d}.{ext}", src)

    # 5 Architecture Markdown documents
    arch_docs = {
        "README.md": b"# Sample Repo\n## Overview\nZero-Code RAG architecture sample.\n",
        "AGENTS.md": b"# Project Invariants\nNever store customer source code in Qdrant.\n",
        "CONTRIBUTING.md": b"# Contributing\nRun uv run pytest before opening PRs.\n",
        "docs/adr/0001-rag.md": b"# ADR 0001\n## Qdrant gRPC\nUse 768-dim cosine vectors.\n",
        "docs/architecture/pipeline.md": b"# Pipeline\n## Sync\nIncremental branch sync on push.\n",
    }
    for doc_path, doc_bytes in arch_docs.items():
        _add(doc_path, doc_bytes)

    # 5 Noise / excluded files -> total = 100 files
    noise_files = {
        "package-lock.json": b'{"lockfileVersion": 3}\n',
        "uv.lock": b"version = 1\n",
        "assets/logo.svg": b"<svg></svg>\n",
        "dist/app.min.js": b"console.log(1);\n",
        "CHANGELOG.md": b"# Changelog\n- v0.1.0 initial release\n",
    }
    for noise_path, noise_bytes in noise_files.items():
        _add(noise_path, noise_bytes)

    assert len(tree) == 100
    return tree, blobs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initial_100_file_index_and_incremental_2_file_sync_live_stack(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    zerocode_auditor: Any,
) -> None:
    repo_slug = "Tieens-04/sample-100-repo"
    commit_v1 = "a" * 40
    commit_v2 = "b" * 40
    commit_v3 = "c" * 40

    tree_entries, blobs = _build_100_file_fixture()

    # Prepare v2 modified blobs for 2 files (1 Python + 1 TSX)
    mod_py_v2 = (
        b'"""Module 00 refactored."""\n\n'
        b"def refactored_func_00(value: int) -> int:\n"
        b'    """Refactored implementation replacing legacy_func_00."""\n'
        b"    return value * 10\n"
    )
    mod_py_v2_sha = _sha1_blob(mod_py_v2)
    blobs[mod_py_v2_sha] = mod_py_v2

    mod_tsx_v2 = b"export function NewWidget00(count: number): number {\n  return count * 2;\n}\n"
    mod_tsx_v2_sha = _sha1_blob(mod_tsx_v2)
    blobs[mod_tsx_v2_sha] = mod_tsx_v2

    # Tree snapshot for v3 force-push reconciliation (module_01 removed, module_00 updated)
    tree_v3 = [
        (
            {**e, "sha": mod_py_v2_sha, "size": len(mod_py_v2)}
            if e["path"] == "src/pkg/module_00.py"
            else (
                {**e, "sha": mod_tsx_v2_sha, "size": len(mod_tsx_v2)}
                if e["path"] == "web/src/comp_00.tsx"
                else e
            )
        )
        for e in tree_entries
        if e["path"] != "src/pkg/module_01.py"
    ]

    active_commit = commit_v1
    active_tree = tree_entries
    blob_fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal blob_fetch_count
        path = request.url.path
        if path == f"/repos/{repo_slug}":
            return httpx.Response(
                200,
                json={"id": 10014, "full_name": repo_slug, "default_branch": "main"},
            )
        if path.startswith(f"/repos/{repo_slug}/commits/"):
            ref = path.rsplit("/", 1)[-1]
            sha = active_commit if ref == "main" else ref
            return httpx.Response(200, json={"sha": sha})
        if path.startswith(f"/repos/{repo_slug}/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith(f"/repos/{repo_slug}/git/trees/"):
            return httpx.Response(
                200,
                json={"sha": active_commit, "truncated": False, "tree": active_tree},
            )
        if path.startswith(f"/repos/{repo_slug}/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            blob_fetch_count += 1
            return httpx.Response(
                200,
                json={"sha": sha, "encoding": "base64", "content": _b64(blobs[sha])},
            )
        if path == f"/repos/{repo_slug}/compare/{commit_v1}...{commit_v2}":
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "total_commits": 1,
                    "files": [
                        {
                            "filename": "src/pkg/module_00.py",
                            "status": "modified",
                            "sha": mod_py_v2_sha,
                        },
                        {
                            "filename": "web/src/comp_00.tsx",
                            "status": "modified",
                            "sha": mod_tsx_v2_sha,
                        },
                    ],
                },
            )
        if path == f"/repos/{repo_slug}/compare/{commit_v2}...{commit_v3}":
            return httpx.Response(
                200,
                json={"status": "diverged", "total_commits": 2, "files": []},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    engine = create_async_engine(isolated_postgres_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    base_settings = Settings(
        _env_file=None,
        qdrant_collection_name=isolated_qdrant_collection,
        github_token=SecretStr("ghp_live_integration_mock"),
        gemini_api_key=SecretStr("gem_live_integration_mock"),
    )
    fake_genai = _DeterministicGenAIClient()

    redis_client = redis_async.from_url(base_settings.redis_url, decode_responses=True)
    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client,
            QdrantZeroCodeStore(
                settings=base_settings,
                collection_name=isolated_qdrant_collection,
            ) as store,
            GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
        ):
            # --- AC-1: Index ~100-file repository in < 30 seconds + persist last_indexed_sha ---
            t0 = time.perf_counter()
            init_report = await run_cli_index(
                repo_slug,
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                store=store,
                embedder=embedder,
            )
            init_elapsed = time.perf_counter() - t0

            assert init_elapsed < 30.0, f"Initial index of 100 files took {init_elapsed:.2f}s"
            assert init_report.tree_entries == 100
            assert init_report.eligible_files == 95
            assert init_report.skipped_files == 5
            assert init_report.code_chunks == 150  # 60*2 Python + 30*1 TS/TSX
            assert init_report.doc_chunks >= 5
            assert init_report.persisted_cursor is True

            async with session_factory() as session:
                repo_row = (
                    await session.execute(
                        sa.select(Repository).where(Repository.full_name == repo_slug)
                    )
                ).scalar_one()
                assert repo_row.last_indexed_sha == commit_v1
                repo_id_str = str(repo_row.id)

                session.add(
                    WebhookEvent(
                        delivery_id="ca14-push-v2",
                        event_type="push",
                        action=None,
                        status="queued",
                    )
                )
                session.add(
                    WebhookEvent(
                        delivery_id="ca14-push-v3-force",
                        event_type="push",
                        action=None,
                        status="queued",
                    )
                )
                await session.commit()

            # Verify live NFR-3 audit after initial index
            assert (
                zerocode_auditor.audit_live_collection(
                    base_settings.qdrant_http_url, isolated_qdrant_collection, 500
                )
                == 0
            )

            # --- AC-2: Push modifying 2 files updates Qdrant cleanly in < 5 seconds ---
            active_commit = commit_v2
            runner = PushSyncRunner(
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                redis_client=redis_client,
                store=store,
                embedder=embedder,
            )
            push_payload_v2 = {
                "delivery_id": "ca14-push-v2",
                "repo_id": repo_id_str,
                "github_repo_id": 10014,
                "repo_full_name": repo_slug,
                "default_branch": "main",
                "installation_id": None,
                "ref": "refs/heads/main",
                "before_sha": commit_v1,
                "after_sha": commit_v2,
            }

            t1 = time.perf_counter()
            sync_result = await runner.execute(push_payload_v2)
            sync_elapsed = time.perf_counter() - t1

            assert sync_elapsed < 5.0, f"Incremental 2-file sync took {sync_elapsed:.2f}s"
            assert sync_result["status"] == "completed"
            assert sync_result["mode"] == "incremental_compare"

            # Verify 0 stale old symbols remain and both new symbols are live in Qdrant
            old_hits = await store.find_by_symbol_names(
                ["legacy_func_00", "helper_call_00", "OldWidget00"],
                repo_full_name=repo_slug,
            )
            assert old_hits == (), f"Stale metadata leaked after sync: {old_hits}"

            new_hits = await store.find_by_symbol_names(
                ["refactored_func_00", "NewWidget00"],
                repo_full_name=repo_slug,
            )
            assert {pt.payload["symbol_name"] for pt in new_hits} == {
                "refactored_func_00",
                "NewWidget00",
            }
            assert {pt.payload["blob_sha"] for pt in new_hits} == {
                mod_py_v2_sha,
                mod_tsx_v2_sha,
            }

            async with session_factory() as session:
                repo_row = (
                    await session.execute(sa.select(Repository).where(Repository.id == repo_row.id))
                ).scalar_one()
                evt_row = (
                    await session.execute(
                        sa.select(WebhookEvent).where(WebhookEvent.delivery_id == "ca14-push-v2")
                    )
                ).scalar_one()
                assert repo_row.last_indexed_sha == commit_v2
                assert evt_row.status == "processed"

            # Duplicate delivery replay must be an idempotent no-op (0 Gemini/GitHub calls)
            calls_before_replay = fake_genai.batch_calls
            blobs_before_replay = blob_fetch_count
            replay_result = await runner.execute(push_payload_v2)
            assert replay_result["status"] == "completed"
            assert fake_genai.batch_calls == calls_before_replay
            assert blob_fetch_count == blobs_before_replay

            # --- Force-push diverged fallback cleans removed file with 0 redundant fetches ---
            active_commit = commit_v3
            active_tree = tree_v3
            blobs_before_v3 = blob_fetch_count
            push_payload_v3 = {
                **push_payload_v2,
                "delivery_id": "ca14-push-v3-force",
                "before_sha": commit_v2,
                "after_sha": commit_v3,
            }
            v3_result = await runner.execute(push_payload_v3)
            assert v3_result["status"] == "completed"
            assert v3_result["mode"] == "fallback_full_reconcile"
            # All remaining 94 files already match (file_path, blob_sha) -> 0 blob fetches!
            assert blob_fetch_count == blobs_before_v3

            removed_hits = await store.find_by_symbol_names(
                ["legacy_func_01", "helper_call_01"],
                repo_full_name=repo_slug,
            )
            assert removed_hits == ()

            # Verify Redis lock was released cleanly
            assert await redis_client.get(f"repo_index_lock:{repo_id_str}") is None

            # Final live Zero-Code Persistence audit
            assert (
                zerocode_auditor.audit_live_collection(
                    base_settings.qdrant_http_url, isolated_qdrant_collection, 500
                )
                == 0
            )
    finally:
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f03_upsert_repository_state_recreated_and_renamed_live_postgres(
    isolated_postgres_dsn: str,
) -> None:
    from codesight.rag.indexer import upsert_repository_state

    engine = create_async_engine(isolated_postgres_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    try:
        # Seed two repositories: github_repo_id=100 holding "acme/api", and 200 holding "acme/old"
        id_100, _ = await upsert_repository_state(
            session_factory,
            full_name="acme/api",
            default_branch="main",
            github_repo_id=100,
            last_indexed_sha="sha_100",
        )
        id_200, _ = await upsert_repository_state(
            session_factory,
            full_name="acme/old",
            default_branch="main",
            github_repo_id=200,
            last_indexed_sha="sha_200",
        )

        # 1. Repo 200 renamed onto "acme/api" (collides with 100) -> resolves cleanly
        resolved_200, updated_200 = await upsert_repository_state(
            session_factory,
            full_name="acme/api",
            default_branch="main",
            github_repo_id=200,
            last_indexed_sha="sha_200_renamed",
        )
        assert resolved_200 == id_200
        assert updated_200 is True

        # 2. Recreated "acme/api" with new github_repo_id=300 -> new UUID, old 200 staled
        resolved_300, updated_300 = await upsert_repository_state(
            session_factory,
            full_name="acme/api",
            default_branch="main",
            github_repo_id=300,
            last_indexed_sha="sha_300_fresh",
        )
        assert resolved_300 != id_200
        assert resolved_300 != id_100
        assert updated_300 is True

        # 3. Optimistic expected_previous_sha refuses stale rollback in live Postgres
        stale_id, stale_updated = await upsert_repository_state(
            session_factory,
            full_name="acme/api",
            default_branch="main",
            github_repo_id=300,
            last_indexed_sha="sha_300_old",
            expected_previous_sha="sha_300_prior",
        )
        assert stale_id == resolved_300
        assert stale_updated is False

        # 4. Concurrent READ COMMITTED race: SELECT ... FOR UPDATE serializes late writer
        import asyncio

        async with session_factory() as holding_session:
            await holding_session.execute(
                sa.update(Repository)
                .where(Repository.github_repo_id == 300)
                .values(last_indexed_sha="sha_300_concurrent_newer")
            )
            # holding_session holds row lock without committing yet!
            racing_task = asyncio.create_task(
                upsert_repository_state(
                    session_factory,
                    full_name="acme/api",
                    default_branch="main",
                    github_repo_id=300,
                    last_indexed_sha="sha_300_stale_overwrite",
                    expected_previous_sha="sha_300_fresh",
                )
            )
            await asyncio.sleep(0.1)
            await holding_session.commit()
            race_id, race_updated = await racing_task
            assert race_id == resolved_300
            assert race_updated is False

        async with session_factory() as session:
            all_rows = (await session.execute(sa.select(Repository))).scalars().all()
            by_gh = {r.github_repo_id: r for r in all_rows}
            assert by_gh[100].full_name == "acme/api__stale_100"
            assert by_gh[200].full_name == "acme/api__stale_200"
            assert by_gh[200].last_indexed_sha == "sha_200_renamed"
            assert by_gh[300].full_name == "acme/api"
            assert by_gh[300].last_indexed_sha == "sha_300_concurrent_newer"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s2_01_live_redis_lua_lock_loss_blocks_stale_qdrant_delete_and_cursor_write(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
) -> None:
    from codesight.core.errors import RetryableTaskError
    from codesight.rag.indexer import (
        AsyncRepoIndexLock,
        IncrementalBranchSyncer,
        InitialRepoIndexer,
    )
    from codesight.vcs.github_auth import GitHubAuthProvider

    repo_slug = f"codesight-eval/live-lock-race-{uuid.uuid4().hex[:8]}"
    sha_v1 = "1" * 40
    sha_v2_stale = "2" * 40
    sha_v3_fresh = "3" * 40

    src_v1 = b"def svc_v1() -> int:\n    return 1\n"
    src_v2 = b"def svc_v2_stale() -> int:\n    return 2\n"
    src_v3 = b"def svc_v3_fresh() -> int:\n    return 3\n"
    blobs = {
        _sha1_blob(src_v1): src_v1,
        _sha1_blob(src_v2): src_v2,
        _sha1_blob(src_v3): src_v3,
    }
    sha_blob_v1 = _sha1_blob(src_v1)
    sha_blob_v2 = _sha1_blob(src_v2)
    sha_blob_v3 = _sha1_blob(src_v3)

    engine = create_async_engine(isolated_postgres_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    settings = Settings(
        _env_file=None,
        qdrant_collection_name=isolated_qdrant_collection,
        github_token=SecretStr("ghp_live_lock_race"),
        gemini_api_key=SecretStr("gem_live_lock_race"),
    )
    redis_client = redis_async.from_url(settings.redis_url, decode_responses=True)
    steal_during_v2 = False

    try:
        async with (
            QdrantZeroCodeStore(
                settings=settings,
                collection_name=isolated_qdrant_collection,
            ) as store,
        ):

            async def _handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path == f"/repos/{repo_slug}":
                    return httpx.Response(
                        200,
                        json={"id": 8801, "full_name": repo_slug, "default_branch": "main"},
                    )
                if path.startswith(f"/repos/{repo_slug}/commits/"):
                    ref = path.rsplit("/", 1)[-1]
                    return httpx.Response(200, json={"sha": sha_v1 if ref == "main" else ref})
                if path.startswith(f"/repos/{repo_slug}/contents/.codesight."):
                    return httpx.Response(404)
                if path.startswith(f"/repos/{repo_slug}/git/trees/"):
                    return httpx.Response(
                        200,
                        json={
                            "sha": sha_v1,
                            "truncated": False,
                            "tree": [
                                {
                                    "path": "src/svc.py",
                                    "mode": "100644",
                                    "type": "blob",
                                    "sha": sha_blob_v1,
                                    "size": len(src_v1),
                                }
                            ],
                        },
                    )
                if path == f"/repos/{repo_slug}/compare/{sha_v1}...{sha_v2_stale}":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ahead",
                            "total_commits": 1,
                            "files": [
                                {
                                    "filename": "src/svc.py",
                                    "status": "modified",
                                    "sha": sha_blob_v2,
                                }
                            ],
                        },
                    )
                if path == f"/repos/{repo_slug}/compare/{sha_v1}...{sha_v3_fresh}":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ahead",
                            "total_commits": 2,
                            "files": [
                                {
                                    "filename": "src/svc.py",
                                    "status": "modified",
                                    "sha": sha_blob_v3,
                                }
                            ],
                        },
                    )
                if path.startswith(f"/repos/{repo_slug}/git/blobs/"):
                    blob_sha = path.rsplit("/", 1)[-1]
                    if steal_during_v2 and blob_sha == sha_blob_v2:
                        # Simulate lock expiry + Push v3 acquiring real Redis lock & finishing first
                        await redis_client.delete(
                            AsyncRepoIndexLock.lock_key(repo_slug.lower()),
                            AsyncRepoIndexLock.lock_key(repo_id_str),
                        )
                        v3_syncer = IncrementalBranchSyncer(
                            initial_indexer=indexer,
                            repo_client=indexer.repo_client,
                            embedder=None,
                            store=store,
                            session_factory=session_factory,
                            lock=AsyncRepoIndexLock(redis_client),
                        )
                        await v3_syncer.sync_push(
                            repo_full_name=repo_slug,
                            after_sha=sha_v3_fresh,
                            repo_id=repo_id_str,
                            github_repo_id=8801,
                        )
                    return httpx.Response(
                        200,
                        json={
                            "sha": blob_sha,
                            "encoding": "base64",
                            "content": _b64(blobs[blob_sha]),
                        },
                    )
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
                auth = GitHubAuthProvider(settings, http_client=http)
                lock = AsyncRepoIndexLock(redis_client)
                indexer = InitialRepoIndexer(
                    auth_provider=auth,
                    embedder=None,
                    store=store,
                    http_client=http,
                    session_factory=session_factory,
                    repo_lock=lock,
                )
                init_rep = await indexer.index_repository(repo_slug)
                repo_id_str = init_rep.repo_id or ""
                assert repo_id_str

                syncer = IncrementalBranchSyncer(
                    initial_indexer=indexer,
                    repo_client=indexer.repo_client,
                    embedder=None,
                    store=store,
                    session_factory=session_factory,
                    lock=lock,
                )
                steal_during_v2 = True
                with pytest.raises(RetryableTaskError, match="lock"):
                    await syncer.sync_push(
                        repo_full_name=repo_slug,
                        after_sha=sha_v2_stale,
                        repo_id=repo_id_str,
                        github_repo_id=8801,
                    )

                # Qdrant MUST retain Push v3's fresh_fn (never deleted by Push v2!)
                v3_hits = await store.find_by_symbol_names(
                    ["svc_v3_fresh"], repo_full_name=repo_slug
                )
                v2_hits = await store.find_by_symbol_names(
                    ["svc_v2_stale"], repo_full_name=repo_slug
                )
                assert len(v3_hits) == 1
                assert v2_hits == ()

                async with session_factory() as session:
                    row = (
                        await session.execute(
                            sa.select(Repository).where(Repository.full_name == repo_slug)
                        )
                    ).scalar_one()
                    assert row.last_indexed_sha == sha_v3_fresh
    finally:
        await redis_client.delete(AsyncRepoIndexLock.lock_key(repo_slug))
        await redis_client.aclose()
        await engine.dispose()


def _load_run_m2_eval_module() -> Any:
    import importlib.util
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "run_m2_eval_live_test", repo_root / "evals" / "run_m2_eval.py"
    )
    assert spec is not None and spec.loader is not None
    cli_mod = importlib.util.module_from_spec(spec)
    sys.modules["run_m2_eval_live_test"] = cli_mod
    spec.loader.exec_module(cli_mod)
    return cli_mod


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s2_02_and_s2_03_eval_cli_isolates_postgres_cursor_and_preserves_main_collection_live(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from urllib.parse import urlsplit

    import asyncpg

    from codesight.rag.indexer import (
        AsyncRepoIndexLock,
        IncrementalBranchSyncer,
        InitialRepoIndexer,
    )
    from codesight.rag.live_eval import M2LiveEvaluationReport
    from codesight.vcs.github_auth import GitHubAuthProvider

    cli_mod = _load_run_m2_eval_module()

    main_db_name = urlsplit(isolated_postgres_dsn).path.lstrip("/")
    run_tag = uuid.uuid4().hex[:8]
    py_slug = f"codesight-eval/cursor-isolation-{run_tag}"
    ts_slug = f"codesight-eval/ts-isolation-{run_tag}"
    sha_v0 = "0" * 39 + "1"
    sha_v1 = "b" * 40
    src_v0 = b"def v0_fn() -> int:\n    return 0\n"
    src_v1 = b"def v1_fn() -> int:\n    return 1\n"
    blob_v0 = _sha1_blob(src_v0)
    blob_v1 = _sha1_blob(src_v1)

    main_engine = create_async_engine(isolated_postgres_dsn, echo=False)
    async with main_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    main_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        main_engine, expire_on_commit=False, autoflush=False
    )

    main_settings = Settings(
        _env_file=None,
        postgres_db=main_db_name,
        qdrant_collection_name=isolated_qdrant_collection,
        github_token=SecretStr("ghp_eval_isolation"),
        gemini_api_key=SecretStr("gem_eval_isolation"),
    )
    redis_client = redis_async.from_url(main_settings.redis_url, decode_responses=True)
    eval_col = f"codesight_m2_eval_it_{uuid.uuid4().hex[:8]}"

    def _gh_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{py_slug}":
            return httpx.Response(
                200, json={"id": 9901, "full_name": py_slug, "default_branch": "main"}
            )
        if path.startswith(f"/repos/{py_slug}/commits/"):
            return httpx.Response(200, json={"sha": sha_v1})
        if path.startswith(f"/repos/{py_slug}/contents/.codesight."):
            return httpx.Response(404)
        if path.startswith(f"/repos/{py_slug}/git/trees/"):
            return httpx.Response(
                200,
                json={
                    "sha": sha_v1,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/main.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_v1,
                            "size": len(src_v1),
                        }
                    ],
                },
            )
        if path == f"/repos/{py_slug}/compare/{sha_v0}...{sha_v1}":
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "total_commits": 1,
                    "files": [{"filename": "src/main.py", "status": "modified", "sha": blob_v1}],
                },
            )
        if path.endswith(f"/git/blobs/{blob_v1}"):
            return httpx.Response(
                200, json={"sha": blob_v1, "encoding": "base64", "content": _b64(src_v1)}
            )
        if path.endswith(f"/git/blobs/{blob_v0}"):
            return httpx.Response(
                200, json={"sha": blob_v0, "encoding": "base64", "content": _b64(src_v0)}
            )
        return httpx.Response(404)

    try:
        async with QdrantZeroCodeStore(
            settings=main_settings,
            collection_name=isolated_qdrant_collection,
        ) as main_store:
            # 1. Seed main Qdrant collection & main PostgreSQL cursor at sha_v0
            await main_store.ensure_collection()
            async with main_session_factory() as session:
                repo_row = Repository(
                    github_repo_id=9901,
                    full_name=py_slug,
                    default_branch="main",
                    last_indexed_sha=sha_v0,
                )
                session.add(repo_row)
                await session.commit()
                main_repo_id = str(repo_row.id)

            # 2. S2-03 live: Passing pre-existing collection to _run_async raises ValueError
            #    and leaves isolated_qdrant_collection intact!
            args_collide = cli_mod._build_parser().parse_args(
                [
                    "--py-repo",
                    py_slug,
                    "--ts-repo",
                    ts_slug,
                    "--collection",
                    isolated_qdrant_collection,
                ]
            )
            with pytest.raises(ValueError, match="already exists"):
                await cli_mod._run_async(
                    args_collide,
                    main_settings,
                    py_repo=py_slug,
                    ts_repo=ts_slug,
                )
            assert await main_store.client.collection_exists(isolated_qdrant_collection) is True

            # 3. S2-02 live: _run_async indexes sha_v1 into isolated temp Postgres + eval_col
            observed_eval_dbs: list[str] = []

            async def _eval_indexing_sha_v1(**kw: Any) -> M2LiveEvaluationReport:
                eval_sf = kw["session_factory"]
                eval_store = kw["store"]
                eval_cfg: Settings = kw["settings"]
                observed_eval_dbs.append(eval_cfg.postgres_db)
                async with httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler)) as http:
                    await run_cli_index(
                        py_slug,
                        settings=eval_cfg,
                        http_client=http,
                        redis_client=redis_client,
                        session_factory=eval_sf,
                        store=eval_store,
                        embed_code_chunks=False,
                    )
                return M2LiveEvaluationReport(
                    py_repo=py_slug,
                    ts_repo=ts_slug,
                    collection_name=eval_col,
                    recall_at_5=1.0,
                    p50_retrieval_ms=50.0,
                    tokens_saved_pct=55.0,
                    zerocode_violations=0,
                )

            monkeypatch.setattr(cli_mod, "run_milestone2_live_evaluation", _eval_indexing_sha_v1)
            monkeypatch.setattr(cli_mod, "validate_m2_kpi_thresholds", lambda _r: [])

            args_ok = cli_mod._build_parser().parse_args(
                ["--py-repo", py_slug, "--ts-repo", ts_slug, "--collection", eval_col]
            )
            rc = await cli_mod._run_async(
                args_ok,
                main_settings,
                py_repo=py_slug,
                ts_repo=ts_slug,
            )
            assert rc == 0
            assert len(observed_eval_dbs) == 1
            assert observed_eval_dbs[0] != main_db_name
            assert observed_eval_dbs[0].startswith("codesight_m2_eval_")

            # Temporary eval Qdrant collection and temporary Postgres DB were both cleaned up!
            assert await main_store.client.collection_exists(eval_col) is False
            admin_conn = await asyncpg.connect(dsn=main_settings.postgres_dsn)
            try:
                exists_after = await admin_conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", observed_eval_dbs[0]
                )
                assert exists_after is None
            finally:
                await admin_conn.close()

            # Main PostgreSQL cursor MUST still be sha_v0, so worker sync_push(sha_v1) runs!
            async with main_session_factory() as session:
                refreshed = (
                    await session.execute(
                        sa.select(Repository).where(Repository.full_name == py_slug)
                    )
                ).scalar_one()
                assert refreshed.last_indexed_sha == sha_v0

            async with httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler)) as http:
                auth = GitHubAuthProvider(main_settings, http_client=http)
                lock = AsyncRepoIndexLock(redis_client)
                init_idx = InitialRepoIndexer(
                    auth_provider=auth,
                    embedder=None,
                    store=main_store,
                    http_client=http,
                    session_factory=main_session_factory,
                    repo_lock=lock,
                )
                syncer = IncrementalBranchSyncer(
                    initial_indexer=init_idx,
                    repo_client=init_idx.repo_client,
                    embedder=None,
                    store=main_store,
                    session_factory=main_session_factory,
                    lock=lock,
                )
                worker_rep = await syncer.sync_push(
                    repo_full_name=py_slug,
                    after_sha=sha_v1,
                    repo_id=main_repo_id,
                    github_repo_id=9901,
                )
                assert worker_rep.mode == "incremental_compare"
                hits = await main_store.find_by_symbol_names(["v1_fn"], repo_full_name=py_slug)
                assert len(hits) == 1
    finally:
        if await QdrantZeroCodeStore(settings=main_settings).client.collection_exists(eval_col):
            tmp_q = QdrantZeroCodeStore(settings=main_settings)
            await tmp_q.client.delete_collection(eval_col)
            await tmp_q.aclose()
        await redis_client.delete(
            AsyncRepoIndexLock.lock_key(py_slug),
            AsyncRepoIndexLock.lock_key(ts_slug),
        )
        await redis_client.aclose()
        await main_engine.dispose()
