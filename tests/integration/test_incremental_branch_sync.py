"""Live Docker integration tests for InitialRepoIndexer & IncrementalBranchSyncer (CA-14)."""

from __future__ import annotations

import base64
import hashlib
import time
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
