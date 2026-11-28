"""Week 1 E2E PR review runner wiring Check Runs, SmartDiffParser, Publisher, and audit DB."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import redis.asyncio as redis_async
import redis.exceptions
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight.agents.llm_factory import LLMFactory
from codesight.config_reader.repo_config import RepoConfigLoader
from codesight.core.config import Settings, get_settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import bind_trace_id, clear_trace_context, get_trace_id
from codesight.db.models import PRReview, Repository, WebhookEvent
from codesight.diff.models import ParsedPullRequestDiff
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.scoring.report_composer import ReportComposer
from codesight.vcs.blob_fetcher import GitHubBlobFetcher
from codesight.vcs.github_auth import GitHubAuthError, GitHubAuthProvider
from codesight.vcs.github_pr_client import (
    FetchedPullRequestDiff,
    GitHubPullRequestClient,
    apply_blob_shas_to_parsed_diff,
)
from codesight.vcs.github_publisher import (
    CheckConclusion,
    GitHubPublishError,
    GitHubReviewPublisher,
    PublishedReviewRecord,
    ReviewCommentDraft,
    format_markdown_code_span,
)
from codesight.worker.ai_review_pipeline import (
    PRReviewExecutionResult,
    execute_ai_pr_review,
    parse_uuid,
    persist_pr_review_result,
)
from codesight.worker.lifecycle import (
    PRReviewLifecycleContext,
    classify_retryable_exception,
    execute_pr_review_lifecycle,
)
from codesight.worker.sha_cache import RedisBlobSHACache
from codesight.worker.supersede_lock import AsyncSupersedeLock, PRSupersededError

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

logger = structlog.get_logger("codesight.worker.pr_review_runner")


def _select_smoke_line(parsed_diff: ParsedPullRequestDiff) -> tuple[str, int] | None:
    for parsed_file in parsed_diff.files:
        if parsed_file.change_type == "deleted":
            continue
        for hunk in parsed_file.hunks:
            for diff_line in hunk.lines:
                if (
                    diff_line.kind == "add"
                    and diff_line.right_line is not None
                    and parsed_file.is_line_commentable(diff_line.right_line)
                ):
                    return parsed_file.file_path, diff_line.right_line
        if parsed_file.valid_commentable_lines:
            return parsed_file.file_path, min(parsed_file.valid_commentable_lines)
    return None


def build_week1_smoke_drafts(
    parsed_diff: ParsedPullRequestDiff,
    *,
    enabled: bool = True,
) -> tuple[ReviewCommentDraft, ...]:
    """Build at most one RIGHT-side anchor comment for Milestone 1 E2E verification."""
    if not enabled:
        return ()
    target = _select_smoke_line(parsed_diff)
    if target is None:
        return ()
    file_path, target_line = target
    safe_path = format_markdown_code_span(file_path)
    # NFR-3: never echo customer raw source code inside review comments or logs.
    body = (
        f"CodeSight-AI Milestone 1 E2E verification anchor on {safe_path} (L{target_line}).\n"
        "Multi-agent AST & RAG review findings will appear inline here in Sprint 3."
    )
    return (
        ReviewCommentDraft(
            path=file_path,
            line=target_line,
            body=body,
            severity="INFO",
        ),
    )


def build_week1_summary_markdown(
    parsed_diff: ParsedPullRequestDiff,
    *,
    head_sha: str,
    config_hash: str,
    trace_id: str,
    diff_source: str = "diff",
) -> str:
    """Render a zero-code-persistence Week 1 PR diff analysis summary in Markdown."""
    m = parsed_diff.metrics
    lines = [
        "## CodeSight-AI — Milestone 1 Diff & Gate Summary",
        "",
        f"- **Commit:** `{head_sha[:12]}`",
        f"- **Trace ID:** `{trace_id}`",
        f"- **Config Hash:** `{config_hash}`",
        f"- **Diff Source:** `{diff_source}`",
        (
            f"- **Files Analyzed:** `{m.kept_files}/{m.total_files}` kept "
            f"(`{m.skipped_files}` noise/excluded files filtered)"
        ),
        (
            f"- **Estimated Token Reduction:** `{m.raw_estimated_tokens}` -> "
            f"`{m.kept_estimated_tokens}` tokens "
            f"(saved `{m.saved_estimated_tokens}` / **{m.tokens_saved_pct:.2f}%**)"
        ),
    ]
    if parsed_diff.skipped_files:
        lines.append("")
        lines.append("### Filtered Noise / Excluded Files")
        for skipped in parsed_diff.skipped_files[:20]:
            safe_skipped = format_markdown_code_span(skipped.file_path)
            lines.append(
                f"- {safe_skipped} (`{skipped.reason}`, ~{skipped.estimated_tokens} tokens)"
            )
    return "\n".join(lines)


async def _safe_complete_check_run(
    publisher: GitHubReviewPublisher,
    *,
    repo_full_name: str,
    check_run_id: int,
    conclusion: CheckConclusion,
    title: str,
    summary: str,
    installation_id: int | None,
    trace_id: str,
    pr_number: int,
    head_sha: str,
    failed_event: str,
    success_event: str | None = None,
) -> None:
    if publisher.has_completed_check_run(repo_full_name, check_run_id):
        return
    try:
        await publisher.complete_check_run(
            repo_full_name,
            check_run_id,
            conclusion=conclusion,
            title=title,
            summary=summary,
            installation_id=installation_id,
        )
        if success_event is not None:
            logger.info(
                success_event,
                trace_id=trace_id,
                repo=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                check_run_id=check_run_id,
            )
    except GitHubPublishError as pub_exc:
        if pub_exc.status_code == 422:
            logger.info(
                "worker_pr_check_run_already_completed",
                trace_id=trace_id,
                repo=repo_full_name,
                check_run_id=check_run_id,
            )
        else:
            logger.warning(
                failed_event,
                trace_id=trace_id,
                check_run_id=check_run_id,
                error_type=type(pub_exc).__name__,
            )
    except Exception as cleanup_exc:
        logger.warning(
            failed_event,
            trace_id=trace_id,
            check_run_id=check_run_id,
            error_type=type(cleanup_exc).__name__,
        )


@dataclass(slots=True)
class _RunnerInfra:
    auth: GitHubAuthProvider
    config_loader: RepoConfigLoader
    pr_client: GitHubPullRequestClient
    publisher: GitHubReviewPublisher
    supersede_lock: AsyncSupersedeLock
    active_redis: redis_async.Redis
    active_http: httpx.AsyncClient


@dataclass(slots=True)
class _LifecycleRunState:
    check_run_id: int | None = None
    check_run_finalized: bool = False
    audit_recorded: bool = False


class _BasePRReviewRunner:
    """Shared async PR review lifecycle, Check Run management, and cleanup spine."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis_client: redis_async.Redis | None = None,
        auth_provider: GitHubAuthProvider | None = None,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._http_client = http_client
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._auth_provider = auth_provider
        self._api_base_url = api_base_url.rstrip("/")

    async def _is_delivery_already_processed(
        self,
        delivery_id: str | None,
        *,
        trace_id: str,
    ) -> bool:
        """Guard against duplicate PR review publication on acks_late worker crash redelivery."""
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
                "worker_delivery_idempotency_check_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )
            return False

    async def _create_check_run_best_effort(
        self,
        publisher: GitHubReviewPublisher,
        *,
        repo_full_name: str,
        head_sha: str,
        trace_id: str,
        pr_number: int,
        installation_id: int | None,
        output_summary: str,
    ) -> int | None:
        try:
            created = await publisher.create_check_run(
                repo_full_name,
                head_sha=head_sha,
                status="in_progress",
                external_id=trace_id,
                output_title="CodeSight-AI Analyzing Pull Request",
                output_summary=output_summary,
                installation_id=installation_id,
            )
            return created.check_run_id
        except GitHubAuthError as auth_exc:
            if (
                auth_exc.status_code == 403
                and not self._settings.has_github_app_credentials
                and self._settings.has_github_token
            ):
                logger.warning(
                    "worker_pr_check_run_skipped_pat_mode",
                    trace_id=trace_id,
                    repo=repo_full_name,
                    pr_number=pr_number,
                )
                return None
            raise

    async def _complete_check_run_with_retry(
        self,
        publisher: GitHubReviewPublisher,
        *,
        repo_full_name: str,
        check_run_id: int,
        conclusion: CheckConclusion,
        title: str,
        summary: str,
        installation_id: int | None,
        trace_id: str,
    ) -> bool:
        # Post-publish PATCH must NOT trigger a full Celery retry (which would duplicate
        # the already-published PR review). Retry once locally, else warn and finish audit.
        for patch_attempt in range(2):
            try:
                await publisher.complete_check_run(
                    repo_full_name,
                    check_run_id,
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                    installation_id=installation_id,
                )
                return True
            except RetryableTaskError as patch_exc:
                if patch_attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(
                    "worker_pr_post_publish_check_run_failed",
                    trace_id=trace_id,
                    repo=repo_full_name,
                    check_run_id=check_run_id,
                    error_type=type(patch_exc).__name__,
                )
            except GitHubPublishError as pub_exc:
                if pub_exc.status_code == 422:
                    logger.info(
                        "worker_pr_check_run_already_completed",
                        trace_id=trace_id,
                        repo=repo_full_name,
                        check_run_id=check_run_id,
                    )
                    return True
                logger.warning(
                    "worker_pr_post_publish_check_run_failed",
                    trace_id=trace_id,
                    repo=repo_full_name,
                    check_run_id=check_run_id,
                    error_type=type(pub_exc).__name__,
                )
                break
            except Exception as patch_exc:
                logger.warning(
                    "worker_pr_post_publish_check_run_failed",
                    trace_id=trace_id,
                    repo=repo_full_name,
                    check_run_id=check_run_id,
                    error_type=type(patch_exc).__name__,
                )
                break
        return False

    async def _run_lifecycle_execute(
        self,
        payload: Mapping[str, Any],
        *,
        is_final_attempt: bool,
        run_review_fn: Callable[
            [PRReviewLifecycleContext, _RunnerInfra, _LifecycleRunState],
            Coroutine[Any, Any, None],
        ],
        run_publish_fn: Callable[
            [PRReviewLifecycleContext, _RunnerInfra, _LifecycleRunState],
            Coroutine[Any, Any, None],
        ],
        record_outcome_audit_fn: Callable[
            [str, str, int, str],
            Coroutine[Any, Any, None],
        ],
        cleanup_extra_fn: Callable[[], None] | None = None,
    ) -> dict[str, str]:
        repo_id = str(payload["repo_id"])
        repo_full_name = str(payload["repo_full_name"])
        pr_number = int(payload["pr_number"])
        head_sha = str(payload["head_sha"])
        raw_inst = payload.get("installation_id")
        installation_id = (
            raw_inst if isinstance(raw_inst, int) and not isinstance(raw_inst, bool) else None
        )
        raw_delivery = payload.get("delivery_id")
        delivery_id = raw_delivery if isinstance(raw_delivery, str) and raw_delivery else None

        bound_here = False
        if get_trace_id() is None and delivery_id is not None:
            bind_trace_id(delivery_id)
            bound_here = True

        active_trace_id = get_trace_id() or delivery_id or "-"
        if await self._is_delivery_already_processed(delivery_id, trace_id=active_trace_id):
            logger.info(
                "worker_pr_delivery_already_processed",
                trace_id=active_trace_id,
                repo=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
            )
            if bound_here:
                clear_trace_context()
            return {"status": "completed", "trace_id": active_trace_id}

        started_perf = time.perf_counter()
        owned_http: httpx.AsyncClient | None = None
        owned_redis: redis_async.Redis | None = None

        active_http = self._http_client
        if active_http is None:
            owned_http = httpx.AsyncClient(
                http2=True,
                timeout=15.0,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
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

        auth = (
            self._auth_provider
            if self._auth_provider is not None
            else GitHubAuthProvider(
                self._settings,
                redis_client=active_redis,
                http_client=active_http,
                base_url=self._api_base_url,
            )
        )
        infra = _RunnerInfra(
            auth=auth,
            config_loader=RepoConfigLoader(
                auth,
                http_client=active_http,
                base_url=self._api_base_url,
            ),
            pr_client=GitHubPullRequestClient(
                auth,
                http_client=active_http,
                base_url=self._api_base_url,
            ),
            publisher=GitHubReviewPublisher(
                auth,
                http_client=active_http,
                base_url=self._api_base_url,
            ),
            supersede_lock=AsyncSupersedeLock(active_redis),
            active_redis=active_redis,
            active_http=active_http,
        )
        state = _LifecycleRunState()

        async def _review_fn(ctx: PRReviewLifecycleContext) -> None:
            if ctx.supersede_lock is not None:
                await ctx.supersede_lock.ensure_active(
                    repo_id,
                    pr_number,
                    head_sha,
                    stage="pre_check_run",
                )
            await run_review_fn(ctx, infra, state)

        async def _publish_fn(ctx: PRReviewLifecycleContext) -> None:
            try:
                await run_publish_fn(ctx, infra, state)
            except PRSupersededError:
                state.check_run_finalized = bool(
                    state.check_run_id is not None
                    and infra.publisher.has_completed_check_run(repo_full_name, state.check_run_id)
                )
                raise

        async def _on_superseded_fn(ctx: PRReviewLifecycleContext) -> None:
            if state.check_run_id is not None and not state.check_run_finalized:
                state.check_run_finalized = True
                await _safe_complete_check_run(
                    infra.publisher,
                    repo_full_name=repo_full_name,
                    check_run_id=state.check_run_id,
                    conclusion="neutral",
                    title="CodeSight-AI Review Superseded",
                    summary=(
                        "Skipped review publication because a newer commit superseded "
                        f"{head_sha[:12]}."
                    ),
                    installation_id=installation_id,
                    trace_id=ctx.trace_id,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    failed_event="worker_pr_check_run_supersede_cleanup_failed",
                    success_event="worker_pr_check_run_superseded",
                )

        try:
            res = await execute_pr_review_lifecycle(
                payload,
                settings=self._settings,
                supersede_lock=infra.supersede_lock,
                config_loader=infra.config_loader,
                review_fn=_review_fn,
                publish_fn=_publish_fn,
                on_superseded_fn=_on_superseded_fn,
                check_entry_supersede=True,
            )
            if res.get("status") == "superseded":
                if not state.audit_recorded:
                    latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
                    await record_outcome_audit_fn(
                        "superseded",
                        "processed",
                        latency_ms,
                        res["trace_id"],
                    )
                    state.audit_recorded = True
                return res
            return {"status": "completed", "trace_id": res["trace_id"]}
        except asyncio.CancelledError:
            trace_id = get_trace_id() or "-"
            will_retry_cancel = not is_final_attempt
            if will_retry_cancel:
                logger.warning(
                    "worker_pr_review_cancelled_deferred",
                    trace_id=trace_id,
                    repo=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    check_run_id=state.check_run_id,
                )
            if state.check_run_id is not None and not state.check_run_finalized:
                state.check_run_finalized = True
                cancel_conclusion: CheckConclusion = "neutral" if will_retry_cancel else "failure"
                cancel_title = (
                    "CodeSight-AI Review Retry Scheduled"
                    if will_retry_cancel
                    else "CodeSight-AI Review Timed Out"
                )
                cancel_summary = (
                    f"Review execution for {head_sha[:12]} was interrupted before completion; "
                    "Celery backoff retry scheduled."
                    if will_retry_cancel
                    else f"Review execution for {head_sha[:12]} was cancelled before completion."
                )
                await _safe_complete_check_run(
                    infra.publisher,
                    repo_full_name=repo_full_name,
                    check_run_id=state.check_run_id,
                    conclusion=cancel_conclusion,
                    title=cancel_title,
                    summary=cancel_summary,
                    installation_id=installation_id,
                    trace_id=trace_id,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    failed_event="worker_pr_check_run_error_cleanup_failed",
                )
            if not will_retry_cancel and not state.audit_recorded:
                latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
                await record_outcome_audit_fn("failed", "failed", latency_ms, trace_id)
                state.audit_recorded = True
            raise
        except Exception as exc:
            trace_id = get_trace_id() or "-"
            is_retryable, _ = classify_retryable_exception(exc)
            will_retry = is_retryable and not is_final_attempt
            if state.check_run_id is not None and not state.check_run_finalized:
                state.check_run_finalized = True
                conclusion: CheckConclusion = "neutral" if will_retry else "failure"
                title = (
                    "CodeSight-AI Review Retry Scheduled"
                    if will_retry
                    else "CodeSight-AI Review Failed"
                )
                if will_retry:
                    summary = (
                        f"Transient {type(exc).__name__} encountered while processing "
                        f"{head_sha[:12]}; Celery backoff retry scheduled."
                    )
                elif is_retryable:
                    summary = (
                        f"Review failed after exhausting retries on {type(exc).__name__} "
                        f"for {head_sha[:12]}."
                    )
                else:
                    summary = f"Review aborted due to non-retryable {type(exc).__name__}."
                await _safe_complete_check_run(
                    infra.publisher,
                    repo_full_name=repo_full_name,
                    check_run_id=state.check_run_id,
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                    installation_id=installation_id,
                    trace_id=trace_id,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    failed_event="worker_pr_check_run_error_cleanup_failed",
                )
            if not will_retry and not state.audit_recorded:
                latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
                await record_outcome_audit_fn("failed", "failed", latency_ms, trace_id)
                state.audit_recorded = True
            raise
        finally:
            if cleanup_extra_fn is not None:
                cleanup_extra_fn()
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


class Week1PRReviewRunner(_BasePRReviewRunner):
    """Coordinates GitHub Checks, RAM diff filtering, Zero-422 publishing, and DB audit."""

    async def _record_audit(
        self,
        *,
        delivery_id: str | None,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        review_status: str,
        webhook_status: str,
        info_count: int,
        tokens_saved_pct: float | None,
        latency_ms: int,
        trace_id: str,
    ) -> None:
        if self._session_factory is None:
            return

        try:
            async with self._session_factory() as session:
                if delivery_id:
                    await session.execute(
                        sa.update(WebhookEvent)
                        .where(WebhookEvent.delivery_id == delivery_id)
                        .values(status=webhook_status)
                    )
                repo_uuid = parse_uuid(repo_id)
                if repo_uuid is not None:
                    repo_exists = await session.scalar(
                        sa.select(Repository.id).where(Repository.id == repo_uuid)
                    )
                    if repo_exists is not None:
                        session.add(
                            PRReview(
                                repo_id=repo_uuid,
                                pr_number=pr_number,
                                head_sha=head_sha[:64],
                                debt_score=None,
                                critical_count=0,
                                warning_count=0,
                                info_count=info_count,
                                tokens_saved_pct=tokens_saved_pct,
                                latency_ms=max(0, latency_ms),
                                status=review_status,
                                trace_id=trace_id[:128] if trace_id else None,
                            )
                        )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "worker_audit_persist_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )

    async def record_superseded_audit(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> None:
        """Persist superseded audit state when a task is aborted outside execute()."""
        raw_delivery = payload.get("delivery_id")
        delivery_id = raw_delivery if isinstance(raw_delivery, str) and raw_delivery else None
        repo_id = str(payload.get("repo_id") or "")
        raw_pr = payload.get("pr_number")
        pr_number = raw_pr if isinstance(raw_pr, int) and not isinstance(raw_pr, bool) else 0
        head_sha = str(payload.get("head_sha") or "")
        if not repo_id or pr_number < 1 or not head_sha:
            return
        await self._record_audit(
            delivery_id=delivery_id,
            repo_id=repo_id,
            pr_number=pr_number,
            head_sha=head_sha,
            review_status="superseded",
            webhook_status="processed",
            info_count=0,
            tokens_saved_pct=None,
            latency_ms=0,
            trace_id=trace_id,
        )

    async def execute(
        self,
        payload: Mapping[str, Any],
        *,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        """Run the Week 1 E2E PR review lifecycle for a webhook dispatch payload."""
        repo_id = str(payload["repo_id"])
        repo_full_name = str(payload["repo_full_name"])
        pr_number = int(payload["pr_number"])
        head_sha = str(payload["head_sha"])
        raw_inst = payload.get("installation_id")
        installation_id = (
            raw_inst if isinstance(raw_inst, int) and not isinstance(raw_inst, bool) else None
        )
        raw_delivery = payload.get("delivery_id")
        delivery_id = raw_delivery if isinstance(raw_delivery, str) and raw_delivery else None
        started_perf = time.perf_counter()

        fetched_diff: FetchedPullRequestDiff | None = None
        drafts: tuple[ReviewCommentDraft, ...] = ()
        summary_body = ""

        async def _run_review(
            ctx: PRReviewLifecycleContext,
            infra: _RunnerInfra,
            state: _LifecycleRunState,
        ) -> None:
            nonlocal fetched_diff, drafts, summary_body
            state.check_run_id = await self._create_check_run_best_effort(
                infra.publisher,
                repo_full_name=repo_full_name,
                head_sha=head_sha,
                trace_id=ctx.trace_id,
                pr_number=pr_number,
                installation_id=installation_id,
                output_summary="Filtering diff noise and mapping hunk coordinates.",
            )
            fetched_diff = await infra.pr_client.fetch_pull_request_diff(
                repo_full_name,
                pr_number,
                repo_config=ctx.repo_config,
                installation_id=installation_id,
            )
            drafts = build_week1_smoke_drafts(
                fetched_diff.parsed_diff,
                enabled=self._settings.week1_smoke_inline_comment,
            )
            summary_body = build_week1_summary_markdown(
                fetched_diff.parsed_diff,
                head_sha=head_sha,
                config_hash=ctx.repo_config.config_hash,
                trace_id=ctx.trace_id,
                diff_source=fetched_diff.diff_source,
            )
            m = fetched_diff.parsed_diff.metrics
            logger.info(
                "worker_pr_diff_analyzed",
                trace_id=ctx.trace_id,
                repo=repo_full_name,
                pr_number=pr_number,
                diff_source=fetched_diff.diff_source,
                kept_files=m.kept_files,
                skipped_files=m.skipped_files,
                tokens_saved_pct=m.tokens_saved_pct,
                draft_count=len(drafts),
            )

        async def _run_publish(
            ctx: PRReviewLifecycleContext,
            infra: _RunnerInfra,
            state: _LifecycleRunState,
        ) -> None:
            if fetched_diff is None:
                raise RuntimeError(
                    "_publish_fn invoked before fetched_diff was populated by _review_fn."
                )
            published = await infra.publisher.publish_review(
                repo_full_name,
                pr_number,
                head_sha=head_sha,
                parsed_diff=fetched_diff.parsed_diff,
                drafts=drafts,
                summary_body=summary_body,
                event="COMMENT",
                supersede_lock=ctx.supersede_lock,
                repo_id=repo_id,
                check_run_id=state.check_run_id,
                installation_id=installation_id,
            )
            latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
            if state.check_run_id is not None:
                m = fetched_diff.parsed_diff.metrics
                summary_text = (
                    f"Analyzed {m.kept_files}/{m.total_files} files "
                    f"({m.tokens_saved_pct:.1f}% token reduction); "
                    f"published {published.inline_count} inline and "
                    f"{published.downgraded_count} summary findings."
                )
                state.check_run_finalized = await self._complete_check_run_with_retry(
                    infra.publisher,
                    repo_full_name=repo_full_name,
                    check_run_id=state.check_run_id,
                    conclusion="success",
                    title="CodeSight-AI Milestone 1 Review Complete",
                    summary=summary_text,
                    installation_id=installation_id,
                    trace_id=ctx.trace_id,
                )
            await self._record_audit(
                delivery_id=delivery_id,
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                review_status="completed",
                webhook_status="processed",
                info_count=published.inline_count + published.downgraded_count,
                tokens_saved_pct=fetched_diff.parsed_diff.metrics.tokens_saved_pct,
                latency_ms=latency_ms,
                trace_id=ctx.trace_id,
            )
            state.audit_recorded = True
            logger.info(
                "worker_pr_review_completed",
                trace_id=ctx.trace_id,
                repo=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                check_run_id=state.check_run_id,
                review_id=published.review_id,
                inline_count=published.inline_count,
                downgraded_count=published.downgraded_count,
                tokens_saved_pct=fetched_diff.parsed_diff.metrics.tokens_saved_pct,
                latency_ms=latency_ms,
            )

        async def _record_outcome_audit(
            review_status: str,
            webhook_status: str,
            latency_ms: int,
            trace_id: str,
        ) -> None:
            tokens_pct = (
                fetched_diff.parsed_diff.metrics.tokens_saved_pct
                if fetched_diff is not None
                else None
            )
            await self._record_audit(
                delivery_id=delivery_id,
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                review_status=review_status,
                webhook_status=webhook_status,
                info_count=0,
                tokens_saved_pct=tokens_pct,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )

        return await self._run_lifecycle_execute(
            payload,
            is_final_attempt=is_final_attempt,
            run_review_fn=_run_review,
            run_publish_fn=_run_publish,
            record_outcome_audit_fn=_record_outcome_audit,
        )


class Sprint3PRReviewRunner(_BasePRReviewRunner):
    """Full AI PR review runner wiring SHA Cache, AST+RAG LangGraph, Scorer, Smart Gate & DB."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis_client: redis_async.Redis | None = None,
        qdrant_store: QdrantZeroCodeStore | None = None,
        embedder: GeminiEmbedder | None = None,
        llm_factory: LLMFactory | None = None,
        auth_provider: GitHubAuthProvider | None = None,
        api_base_url: str = "https://api.github.com",
        span_exporter: SpanExporter | None = None,
    ) -> None:
        super().__init__(
            settings=settings,
            http_client=http_client,
            session_factory=session_factory,
            redis_client=redis_client,
            auth_provider=auth_provider,
            api_base_url=api_base_url,
        )
        self._qdrant_store = qdrant_store
        self._embedder = embedder
        self._llm_factory = llm_factory
        self._span_exporter = span_exporter

    async def _record_audit_best_effort(
        self,
        *,
        delivery_id: str | None,
        repo_id: str,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        default_branch: str,
        installation_id: int | None,
        review_status: str,
        webhook_status: str,
        result: PRReviewExecutionResult | None,
        tokens_saved_pct: float | None,
        latency_ms: int,
        trace_id: str,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            await persist_pr_review_result(
                self._session_factory,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                result=result,
                repo_id=repo_id,
                delivery_id=delivery_id,
                default_branch=default_branch,
                installation_id=installation_id,
                review_status=review_status,
                webhook_status=webhook_status,
                tokens_saved_pct=tokens_saved_pct,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning(
                "worker_audit_persist_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )

    async def execute(
        self,
        payload: Mapping[str, Any],
        *,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        """Run full Sprint 3 AI PR review lifecycle with Smart Gate and SHA Cache."""
        repo_id = str(payload["repo_id"])
        repo_full_name = str(payload["repo_full_name"])
        pr_number = int(payload["pr_number"])
        head_sha = str(payload["head_sha"])
        raw_base = payload.get("base_sha")
        base_sha = raw_base.strip() if isinstance(raw_base, str) and raw_base.strip() else None
        raw_default_branch = payload.get("default_branch")
        default_branch = (
            raw_default_branch.strip()
            if isinstance(raw_default_branch, str) and raw_default_branch.strip()
            else "main"
        )
        raw_inst = payload.get("installation_id")
        installation_id = (
            raw_inst if isinstance(raw_inst, int) and not isinstance(raw_inst, bool) else None
        )
        raw_delivery = payload.get("delivery_id")
        delivery_id = raw_delivery if isinstance(raw_delivery, str) and raw_delivery else None
        started_perf = time.perf_counter()

        fetched_diff: FetchedPullRequestDiff | None = None
        ai_result: PRReviewExecutionResult | None = None

        async def _run_review(
            ctx: PRReviewLifecycleContext,
            infra: _RunnerInfra,
            state: _LifecycleRunState,
        ) -> None:
            nonlocal fetched_diff, ai_result
            state.check_run_id = await self._create_check_run_best_effort(
                infra.publisher,
                repo_full_name=repo_full_name,
                head_sha=head_sha,
                trace_id=ctx.trace_id,
                pr_number=pr_number,
                installation_id=installation_id,
                output_summary="Running AST/RAG analysis, SHA cache lookup, and Debt Scorer.",
            )

            async def _fetch_blob_shas_safe() -> dict[str, str]:
                try:
                    return await infra.pr_client.fetch_pr_file_blob_shas(
                        repo_full_name,
                        pr_number,
                        installation_id=installation_id,
                    )
                except RateLimitTaskError:
                    raise
                except (RetryableTaskError, GitHubPublishError, GitHubAuthError) as exc:
                    logger.warning(
                        "github_pr_blob_sha_enrichment_degraded",
                        repo=repo_full_name,
                        pr_number=pr_number,
                        error_type=type(exc).__name__,
                    )
                    return {}

            diff_res, sha_map = await asyncio.gather(
                infra.pr_client.fetch_pull_request_diff(
                    repo_full_name,
                    pr_number,
                    repo_config=ctx.repo_config,
                    installation_id=installation_id,
                ),
                _fetch_blob_shas_safe(),
            )
            fetched_diff = diff_res
            enriched_diff = (
                apply_blob_shas_to_parsed_diff(diff_res.parsed_diff, sha_map)
                if sha_map and diff_res.diff_source == "diff"
                else diff_res.parsed_diff
            )
            blob_fetcher = GitHubBlobFetcher(
                infra.auth,
                http_client=infra.active_http,
                base_url=self._api_base_url,
            )
            sha_cache = RedisBlobSHACache(infra.active_redis)
            ai_result = await execute_ai_pr_review(
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                parsed_diff=enriched_diff,
                repo_config=ctx.repo_config,
                settings=self._settings,
                trace_id=ctx.trace_id,
                base_ref=base_sha or default_branch,
                installation_id=installation_id,
                blob_fetcher=blob_fetcher,
                llm_factory=self._llm_factory,
                sha_cache=sha_cache,
                qdrant_store=self._qdrant_store,
                embedder=self._embedder,
                diff_source=diff_res.diff_source,
                span_exporter=self._span_exporter,
            )

        async def _load_publish_receipt(
            receipt_key: str, active_redis: redis_async.Redis
        ) -> PublishedReviewRecord | None:
            try:
                raw_receipt = await active_redis.get(receipt_key)
            except (redis.exceptions.RedisError, TimeoutError, OSError):
                return None
            if not raw_receipt:
                return None
            try:
                text = (
                    raw_receipt.decode("utf-8", errors="replace")
                    if isinstance(raw_receipt, bytes)
                    else str(raw_receipt)
                )
                data = json.loads(text)
                if not isinstance(data, dict):
                    return None
                return PublishedReviewRecord(
                    review_id=data.get("review_id"),
                    html_url=data.get("html_url"),
                    state=str(data.get("state", "COMMENTED")),
                    inline_count=int(data.get("inline_count", 0)),
                    downgraded_count=int(data.get("downgraded_count", 0)),
                    used_422_fallback=bool(data.get("used_422_fallback", False)),
                )
            except (ValueError, TypeError):
                return None

        async def _save_publish_receipt(
            receipt_key: str,
            active_redis: redis_async.Redis,
            rec: PublishedReviewRecord,
            *,
            trace_id: str,
        ) -> None:
            payload_json = json.dumps(
                {
                    "review_id": rec.review_id,
                    "html_url": rec.html_url,
                    "state": rec.state,
                    "inline_count": rec.inline_count,
                    "downgraded_count": rec.downgraded_count,
                    "used_422_fallback": rec.used_422_fallback,
                }
            )
            try:
                await active_redis.set(receipt_key, payload_json, ex=86_400)
            except (redis.exceptions.RedisError, TimeoutError, OSError) as exc:
                logger.warning(
                    "worker_pr_publish_receipt_write_failed",
                    trace_id=trace_id,
                    error_type=type(exc).__name__,
                )

        async def _run_publish(
            ctx: PRReviewLifecycleContext,
            infra: _RunnerInfra,
            state: _LifecycleRunState,
        ) -> None:
            if ai_result is None or fetched_diff is None:
                raise RuntimeError(
                    "_publish_fn invoked before ai_result was populated by _review_fn."
                )
            receipt_key = (
                f"pr_publish_receipt:{repo_id}:{pr_number}:{head_sha}:{delivery_id or 'nodelivery'}"
            )
            published = await _load_publish_receipt(receipt_key, infra.active_redis)
            if published is not None:
                logger.info(
                    "worker_pr_publish_receipt_reused",
                    trace_id=ctx.trace_id,
                    repo=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    review_id=published.review_id,
                )
            else:
                published = await infra.publisher.publish_review(
                    repo_full_name,
                    pr_number,
                    head_sha=head_sha,
                    parsed_diff=ai_result.parsed_diff,
                    drafts=ai_result.inline_drafts,
                    summary_body=ai_result.summary_markdown,
                    event=ai_result.gate.review_event,
                    supersede_lock=ctx.supersede_lock,
                    repo_id=repo_id,
                    check_run_id=state.check_run_id,
                    installation_id=installation_id,
                    after_sources_by_file=ai_result.after_sources_by_file,
                )
                await _save_publish_receipt(
                    receipt_key,
                    infra.active_redis,
                    published,
                    trace_id=ctx.trace_id,
                )
            latency_ms = max(0, int((time.perf_counter() - started_perf) * 1000))
            if state.check_run_id is not None:
                check_title, check_summary = ReportComposer().compose_check_summary(
                    ai_result.breakdown,
                    ai_result.gate,
                    inline_count=published.inline_count,
                    downgraded_count=published.downgraded_count,
                    cache_hits=ai_result.cache_hits,
                    total_files=len(ai_result.parsed_diff.files),
                )
                state.check_run_finalized = await self._complete_check_run_with_retry(
                    infra.publisher,
                    repo_full_name=repo_full_name,
                    check_run_id=state.check_run_id,
                    conclusion=ai_result.gate.check_conclusion,
                    title=check_title,
                    summary=check_summary,
                    installation_id=installation_id,
                    trace_id=ctx.trace_id,
                )
            await self._record_audit_best_effort(
                delivery_id=delivery_id,
                repo_id=repo_id,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                default_branch=default_branch,
                installation_id=installation_id,
                review_status="completed",
                webhook_status="processed",
                result=ai_result,
                tokens_saved_pct=ai_result.tokens_saved_pct,
                latency_ms=latency_ms,
                trace_id=ctx.trace_id,
            )
            state.audit_recorded = True
            logger.info(
                "worker_pr_review_completed",
                trace_id=ctx.trace_id,
                repo=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                check_run_id=state.check_run_id,
                review_id=published.review_id,
                debt_score=ai_result.breakdown.score,
                review_event=published.state,
                inline_count=published.inline_count,
                downgraded_count=published.downgraded_count,
                cache_hits=ai_result.cache_hits,
                tokens_saved_pct=ai_result.tokens_saved_pct,
                latency_ms=latency_ms,
            )

        async def _record_outcome_audit(
            review_status: str,
            webhook_status: str,
            latency_ms: int,
            trace_id: str,
        ) -> None:
            tokens_pct = (
                fetched_diff.parsed_diff.metrics.tokens_saved_pct
                if fetched_diff is not None
                else None
            )
            await self._record_audit_best_effort(
                delivery_id=delivery_id,
                repo_id=repo_id,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                default_branch=default_branch,
                installation_id=installation_id,
                review_status=review_status,
                webhook_status=webhook_status,
                result=None,
                tokens_saved_pct=tokens_pct,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )

        def _cleanup_extra() -> None:
            if ai_result is not None:
                ai_result.after_sources_by_file.clear()

        return await self._run_lifecycle_execute(
            payload,
            is_final_attempt=is_final_attempt,
            run_review_fn=_run_review,
            run_publish_fn=_run_publish,
            record_outcome_audit_fn=_record_outcome_audit,
            cleanup_extra_fn=_cleanup_extra,
        )
