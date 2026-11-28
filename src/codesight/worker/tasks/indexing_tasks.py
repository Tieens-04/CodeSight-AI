"""Celery default-branch push indexing runner with Postgres idempotency and Redis lock."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final

import httpx
import redis.asyncio as redis_async
import redis.exceptions
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight.config_reader.repo_config import RepoConfigLoader
from codesight.core.config import Settings, get_settings
from codesight.core.logging import bind_trace_id, clear_trace_context, get_trace_id
from codesight.db.models import WebhookEvent
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.indexer import (
    AsyncRepoIndexLock,
    GitHubRepoIndexClient,
    IncrementalBranchSyncer,
    InitialRepoIndexer,
)
from codesight.rag.models import EmbeddingConfigError
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.vcs.github_auth import GitHubAuthConfigError, GitHubAuthProvider
from codesight.worker.lifecycle import classify_retryable_exception

_ZERO_SHA: Final[str] = "0" * 40
logger = structlog.get_logger("codesight.worker.indexing_tasks")


def has_indexing_credentials(settings: Settings) -> bool:
    """Return True when both GitHub and Gemini embedding credentials are configured."""
    return bool(settings.has_github_credentials and settings.has_gemini_api_key)


def can_run_live_push_sync(settings: Settings) -> bool:
    """Return True when push sync has sufficient credentials for current environment."""
    if has_indexing_credentials(settings):
        return True
    return bool(
        settings.has_github_credentials
        and not settings.index_embed_code_chunks
        and settings.app_env != "production"
    )


def log_push_stub_fallback(
    payload: Mapping[str, Any],
    *,
    settings: Settings,
    trace_id: str,
) -> None:
    """Emit warning when dev push sync falls back to stub mode without credentials."""
    if not can_run_live_push_sync(settings):
        logger.warning(
            "push_sync_skipped_missing_credentials",
            trace_id=trace_id,
            repo=str(payload.get("repo_full_name", "")),
            ref=str(payload.get("ref", "")),
            has_github_credentials=settings.has_github_credentials,
            has_gemini_api_key=settings.has_gemini_api_key,
            index_embed_code_chunks=settings.index_embed_code_chunks,
        )
    logger.info(
        "webhook_push_stub_received",
        trace_id=trace_id,
        repo=str(payload.get("repo_full_name", "")),
        ref=str(payload.get("ref", "")),
    )


def extract_push_coords(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Extract `(repo_id, repo_full_name, after_sha)` from a push dispatch payload."""
    repo_id = payload.get("repo_id")
    repo_full_name = payload.get("repo_full_name")
    after_sha = payload.get("after_sha")
    if (
        isinstance(repo_id, str)
        and repo_id.strip()
        and isinstance(repo_full_name, str)
        and repo_full_name.strip()
        and isinstance(after_sha, str)
        and after_sha.strip()
        and after_sha.strip() != _ZERO_SHA
    ):
        return repo_id.strip(), repo_full_name.strip(), after_sha.strip()
    return None


async def _persist_webhook_event_status(
    session_factory: async_sessionmaker[AsyncSession] | None,
    delivery_id: str | None,
    *,
    webhook_status: str,
    trace_id: str,
) -> None:
    if session_factory is None or not delivery_id:
        return
    try:
        async with session_factory() as session:
            await session.execute(
                sa.update(WebhookEvent)
                .where(WebhookEvent.delivery_id == delivery_id)
                .values(status=webhook_status)
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "worker_push_audit_persist_failed",
            trace_id=trace_id,
            error_type=type(exc).__name__,
        )


class PushSyncRunner:
    """Runs FR-07 IncrementalBranchSyncer for default-branch push webhooks with DB audit."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis_client: redis_async.Redis | None = None,
        store: QdrantZeroCodeStore | None = None,
        embedder: GeminiEmbedder | None = None,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._http_client = http_client
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._store = store
        self._embedder = embedder
        self._api_base_url = api_base_url.rstrip("/")

    async def _is_delivery_already_processed(
        self,
        delivery_id: str | None,
        *,
        trace_id: str,
    ) -> bool:
        """Guard against duplicate sync runs on task_acks_late redelivery."""
        if self._session_factory is None or not delivery_id:
            return False
        try:
            async with self._session_factory() as session:
                evt_status = await session.scalar(
                    sa.select(WebhookEvent.status).where(WebhookEvent.delivery_id == delivery_id)
                )
                return evt_status == "processed"
        except Exception as exc:
            logger.warning(
                "worker_push_idempotency_check_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )
            return False

    async def _record_webhook_status(
        self,
        delivery_id: str | None,
        *,
        webhook_status: str,
        trace_id: str,
    ) -> None:
        await _persist_webhook_event_status(
            self._session_factory,
            delivery_id,
            webhook_status=webhook_status,
            trace_id=trace_id,
        )

    async def execute(
        self,
        payload: Mapping[str, Any],
        *,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        """Execute incremental default-branch synchronization for a push webhook payload."""
        coords = extract_push_coords(payload)
        if coords is None:
            raise ValueError("Push webhook payload missing repo_id, repo_full_name, or after_sha.")
        repo_id, repo_full_name, after_sha = coords

        raw_delivery = payload.get("delivery_id")
        delivery_id = (
            raw_delivery.strip() if isinstance(raw_delivery, str) and raw_delivery.strip() else None
        )
        raw_branch = payload.get("default_branch")
        default_branch = (
            raw_branch.strip() if isinstance(raw_branch, str) and raw_branch.strip() else "main"
        )
        raw_inst = payload.get("installation_id")
        installation_id = (
            raw_inst if isinstance(raw_inst, int) and not isinstance(raw_inst, bool) else None
        )
        raw_gh_id = payload.get("github_repo_id")
        github_repo_id = (
            raw_gh_id if isinstance(raw_gh_id, int) and not isinstance(raw_gh_id, bool) else None
        )
        raw_ref = payload.get("ref")
        pushed_ref = raw_ref.strip() if isinstance(raw_ref, str) and raw_ref.strip() else None

        bound_here = False
        if get_trace_id() is None and delivery_id is not None:
            bind_trace_id(delivery_id)
            bound_here = True

        active_trace_id = get_trace_id() or delivery_id or "-"
        if await self._is_delivery_already_processed(delivery_id, trace_id=active_trace_id):
            logger.info(
                "worker_push_delivery_already_processed",
                trace_id=active_trace_id,
                repo=repo_full_name,
                after_sha=after_sha,
            )
            if bound_here:
                clear_trace_context()
            return {
                "status": "completed",
                "mode": "duplicate_skipped",
                "trace_id": active_trace_id,
            }

        owned_http: httpx.AsyncClient | None = None
        owned_redis: redis_async.Redis | None = None
        owned_store: QdrantZeroCodeStore | None = None
        owned_embedder: GeminiEmbedder | None = None

        try:
            active_http = self._http_client
            if active_http is None:
                owned_http = httpx.AsyncClient(
                    http2=True,
                    timeout=15.0,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=16),
                )
                active_http = owned_http

            active_redis = self._redis_client
            if active_redis is None:
                owned_redis = redis_async.from_url(
                    self._settings.redis_url,
                    decode_responses=True,
                    socket_timeout=0.5,
                    socket_connect_timeout=0.5,
                )
                active_redis = owned_redis

            active_store = self._store
            if active_store is None:
                owned_store = QdrantZeroCodeStore(settings=self._settings)
                active_store = owned_store

            active_embedder = self._embedder
            if active_embedder is None:
                if self._settings.has_gemini_api_key:
                    owned_embedder = GeminiEmbedder(settings=self._settings)
                    active_embedder = owned_embedder
                elif self._settings.index_embed_code_chunks:
                    raise EmbeddingConfigError(
                        "GEMINI_API_KEY is required when index_embed_code_chunks=True."
                    )

            auth = GitHubAuthProvider(
                self._settings,
                redis_client=active_redis,
                http_client=active_http,
                base_url=self._api_base_url,
            )
            config_loader = RepoConfigLoader(
                auth,
                http_client=active_http,
                base_url=self._api_base_url,
            )
            repo_client = GitHubRepoIndexClient(
                auth,
                http_client=active_http,
                base_url=self._api_base_url,
            )
            repo_lock = AsyncRepoIndexLock(active_redis)
            initial_indexer = InitialRepoIndexer(
                auth_provider=auth,
                embedder=active_embedder,
                store=active_store,
                http_client=active_http,
                repo_client=repo_client,
                config_loader=config_loader,
                session_factory=self._session_factory,
                repo_lock=repo_lock,
                base_url=self._api_base_url,
                embed_code_chunks=self._settings.index_embed_code_chunks,
            )
            syncer = IncrementalBranchSyncer(
                initial_indexer=initial_indexer,
                repo_client=repo_client,
                embedder=active_embedder,
                store=active_store,
                config_loader=config_loader,
                session_factory=self._session_factory,
                redis_client=active_redis,
                lock=repo_lock,
                embed_code_chunks=self._settings.index_embed_code_chunks,
            )

            report = await syncer.sync_push(
                repo_full_name=repo_full_name,
                after_sha=after_sha,
                default_branch=default_branch,
                pushed_ref=pushed_ref,
                repo_id=repo_id,
                github_repo_id=github_repo_id,
                installation_id=installation_id,
            )
            if bool(getattr(report, "truncated_tree", False)):
                await self._record_webhook_status(
                    delivery_id,
                    webhook_status="failed",
                    trace_id=active_trace_id,
                )
                logger.warning(
                    "worker_push_sync_incomplete_truncated_tree",
                    trace_id=active_trace_id,
                    repo=repo_full_name,
                    after_sha=after_sha,
                    mode=report.mode,
                    changed_files=report.changed_files,
                    upserted_points=report.upserted_points,
                    elapsed_ms=report.elapsed_ms,
                )
                return {
                    "status": "incomplete",
                    "mode": report.mode,
                    "trace_id": active_trace_id,
                }
            await self._record_webhook_status(
                delivery_id,
                webhook_status="processed",
                trace_id=active_trace_id,
            )
            logger.info(
                "worker_push_sync_completed",
                trace_id=active_trace_id,
                repo=repo_full_name,
                after_sha=after_sha,
                mode=report.mode,
                changed_files=report.changed_files,
                upserted_points=report.upserted_points,
                elapsed_ms=report.elapsed_ms,
            )
            return {
                "status": "completed",
                "mode": report.mode,
                "trace_id": active_trace_id,
            }
        except asyncio.CancelledError:
            if is_final_attempt:
                await self._record_webhook_status(
                    delivery_id,
                    webhook_status="failed",
                    trace_id=active_trace_id,
                )
            else:
                logger.warning(
                    "worker_push_sync_cancelled_deferred",
                    trace_id=active_trace_id,
                    repo=repo_full_name,
                )
            raise
        except Exception as exc:
            is_retryable, _ = classify_retryable_exception(exc)
            will_retry = is_retryable and not is_final_attempt
            if will_retry:
                logger.warning(
                    "worker_push_sync_retry_deferred",
                    trace_id=active_trace_id,
                    repo=repo_full_name,
                    error_type=type(exc).__name__,
                )
            else:
                await self._record_webhook_status(
                    delivery_id,
                    webhook_status="failed",
                    trace_id=active_trace_id,
                )
            raise
        finally:
            if owned_embedder is not None:
                try:
                    await owned_embedder.aclose()
                except Exception:
                    pass
            if owned_store is not None:
                try:
                    await owned_store.aclose()
                except Exception:
                    pass
            if owned_redis is not None:
                try:
                    await owned_redis.aclose()
                except (redis.exceptions.RedisError, OSError):
                    pass
            if owned_http is not None:
                try:
                    await owned_http.aclose()
                except (httpx.HTTPError, OSError):
                    pass
            if bound_here:
                clear_trace_context()


async def run_push_webhook_task(
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: redis_async.Redis | None = None,
    store: QdrantZeroCodeStore | None = None,
    embedder: GeminiEmbedder | None = None,
    api_base_url: str = "https://api.github.com",
    is_final_attempt: bool = False,
) -> dict[str, str]:
    """Run live FR-07 push sync when credentials are configured, else dev-mode stub."""
    active_settings = settings if settings is not None else get_settings()
    coords = extract_push_coords(payload)
    if coords is not None and can_run_live_push_sync(active_settings):
        runner = PushSyncRunner(
            settings=active_settings,
            http_client=http_client,
            session_factory=session_factory,
            redis_client=redis_client,
            store=store,
            embedder=embedder,
            api_base_url=api_base_url,
        )
        return await runner.execute(payload, is_final_attempt=is_final_attempt)

    if (
        coords is not None
        and active_settings.app_env == "production"
        and not has_indexing_credentials(active_settings)
    ):
        raw_delivery = payload.get("delivery_id")
        delivery_id = (
            raw_delivery.strip() if isinstance(raw_delivery, str) and raw_delivery.strip() else None
        )
        active_trace_id = get_trace_id() or delivery_id or "-"
        await _persist_webhook_event_status(
            session_factory,
            delivery_id,
            webhook_status="failed",
            trace_id=active_trace_id,
        )
        raise GitHubAuthConfigError(
            "GitHub and Gemini credentials are required in production worker push sync mode."
        )

    trace_id = get_trace_id() or "-"
    log_push_stub_fallback(payload, settings=active_settings, trace_id=trace_id)
    return {"status": "accepted", "trace_id": trace_id}
