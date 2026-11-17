"""GitHub webhook gateway route (<100ms ACK, HMAC-SHA256, Postgres idempotency)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Protocol, TypeGuard, cast

import redis.asyncio as redis_async
import structlog
from celery import Celery
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight.core.config import Settings, get_settings
from codesight.db.models import Repository, WebhookEvent
from codesight.vcs.github_auth import GitHubAuthConfigError, validate_repo_full_name
from codesight.worker.celery_app import celery_app
from codesight.worker.supersede_lock import (
    PR_SUPERSEDE_TTL_SECONDS,
    PR_SYNCHRONIZE_DEBOUNCE_SECONDS,
    AsyncSupersedeLock,
)

MAX_WEBHOOK_BODY_BYTES: Final[int] = 25 * 1024 * 1024  # 25 MiB ceiling for 512m container
ACTIONABLE_PR_ACTIONS: Final[frozenset[str]] = frozenset({"opened", "synchronize", "reopened"})
STALE_RECEIVED_SECONDS: Final[int] = 60
BROKER_PUBLISH_RETRY_DELAY_SECONDS: Final[float] = 0.05
_ZERO_SHA: Final[str] = "0" * 40
_MAX_INT32: Final[int] = (1 << 31) - 1
_MAX_INT64: Final[int] = (1 << 63) - 1

logger = structlog.get_logger("codesight.api.webhooks")
router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


class PRWebhookDispatchPayload(BaseModel):
    """Typed payload enqueued to Celery for actionable pull_request deliveries."""

    delivery_id: str
    repo_id: str
    github_repo_id: int | None
    repo_full_name: str
    default_branch: str
    installation_id: int | None
    pr_number: int
    head_sha: str
    base_sha: str | None = None
    before_sha: str | None = None
    updated_at_ms: int | None = None
    action: str


class PushWebhookDispatchPayload(BaseModel):
    """Typed payload enqueued to Celery for default-branch push deliveries."""

    delivery_id: str
    repo_id: str
    github_repo_id: int | None
    repo_full_name: str
    default_branch: str
    installation_id: int | None
    ref: str
    before_sha: str | None = None
    after_sha: str


class WebhookAckResponse(BaseModel):
    """Structured acknowledgment returned by POST /api/v1/webhooks/github."""

    status: Literal["queued", "duplicate", "ignored", "pong"]
    delivery_id: str
    event: str
    task_id: str | None = None


class WebhookDispatcher(Protocol):
    """Protocol abstracting background dispatch for test injection."""

    async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
        """Enqueue a pull_request review job and return its task ID."""
        ...

    async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
        """Enqueue a default-branch incremental sync job and return its task ID."""
        ...


class CeleryWebhookDispatcher:
    """Dispatches webhook jobs to Celery via asyncio.to_thread to keep ASGI non-blocking."""

    def __init__(
        self,
        app: Celery | None = None,
        *,
        redis_client: redis_async.Redis | None = None,
    ) -> None:
        self._app = app
        self._redis_client = redis_client

    async def _publish_task(
        self,
        target: Celery,
        task_name: str,
        payload_dict: dict[str, Any],
        *,
        countdown: int = 0,
    ) -> str:
        try:
            result = await asyncio.to_thread(
                target.send_task,
                task_name,
                args=(payload_dict,),
                countdown=countdown,
            )
        except Exception:
            await asyncio.sleep(BROKER_PUBLISH_RETRY_DELAY_SECONDS)
            result = await asyncio.to_thread(
                target.send_task,
                task_name,
                args=(payload_dict,),
                countdown=countdown,
            )
        return str(result.id)

    async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
        target = self._app if self._app is not None else celery_app
        countdown = PR_SYNCHRONIZE_DEBOUNCE_SECONDS if payload.action == "synchronize" else 0
        lock = AsyncSupersedeLock(self._redis_client) if self._redis_client is not None else None
        claim = (
            await lock.claim_active_sha(
                payload.repo_id,
                payload.pr_number,
                payload.head_sha,
                ttl_seconds=PR_SUPERSEDE_TTL_SECONDS,
                before_sha=payload.before_sha,
                event_ts_ms=payload.updated_at_ms,
            )
            if lock is not None
            else None
        )
        if claim is not None and claim.stale:
            logger.info(
                "webhook_pr_out_of_order_superseded",
                repo_id=payload.repo_id,
                pr_number=payload.pr_number,
                stale_head_sha=payload.head_sha,
                active_head_sha=claim.prev_sha,
            )
            return f"superseded:{claim.prev_sha or 'stale'}"

        try:
            task_id = await self._publish_task(
                target,
                "codesight.webhook.handle_pr_event",
                payload.model_dump(mode="json"),
                countdown=countdown,
            )
        except Exception:
            if lock is not None and claim is not None and claim.claimed:
                await lock.rollback_active_sha(
                    payload.repo_id,
                    payload.pr_number,
                    attempted_sha=payload.head_sha,
                    prev_sha=claim.prev_sha,
                    ttl_seconds=PR_SUPERSEDE_TTL_SECONDS,
                    before_sha=payload.before_sha,
                    prev_ts_ms=claim.prev_ts_ms,
                )
            raise

        async def _revoke_superseded(superseded_task_id: str) -> None:
            try:
                await asyncio.to_thread(
                    target.control.revoke,
                    superseded_task_id,
                    terminate=False,
                )
            except Exception as exc:
                logger.warning(
                    "webhook_supersede_revoke_failed",
                    superseded_task_id=superseded_task_id,
                    error_type=type(exc).__name__,
                )

        if lock is not None:
            record_coro = lock.record_active_task(
                payload.repo_id,
                payload.pr_number,
                head_sha=payload.head_sha,
                task_id=task_id,
                ttl_seconds=PR_SUPERSEDE_TTL_SECONDS,
            )
            prev_task_id = claim.prev_task_id if claim is not None else None
            if prev_task_id and prev_task_id != task_id:
                await asyncio.gather(record_coro, _revoke_superseded(prev_task_id))
            else:
                await record_coro

        return task_id

    async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
        target = self._app if self._app is not None else celery_app
        return await self._publish_task(
            target,
            "codesight.webhook.handle_push_event",
            payload.model_dump(mode="json"),
        )


@dataclass(frozen=True)
class _IgnoredWebhook:
    ack_status: Literal["ignored", "pong"]
    action: str | None


@dataclass(frozen=True)
class _QueuedPRWebhook:
    action: str
    repo_full_name: str
    github_repo_id: int | None
    default_branch: str
    installation_id: int | None
    pr_number: int
    head_sha: str
    base_sha: str | None
    before_sha: str | None = None
    updated_at_ms: int | None = None


def _parse_iso_timestamp_ms(raw: Any) -> int | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    ts_ms = int(dt.timestamp() * 1000)
    return ts_ms if ts_ms > 0 else None


@dataclass(frozen=True)
class _QueuedPushWebhook:
    action: str | None
    repo_full_name: str
    github_repo_id: int | None
    default_branch: str
    installation_id: int | None
    ref: str
    before_sha: str | None
    after_sha: str


type _ClassifiedWebhook = _IgnoredWebhook | _QueuedPRWebhook | _QueuedPushWebhook


def verify_github_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify GitHub X-Hub-Signature-256 HMAC-SHA256 signature in constant time."""
    if (
        not secret
        or not signature_header
        or not signature_header.startswith("sha256=")
        or not signature_header.isascii()
    ):
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature_header)


async def read_bounded_body(
    request: Request,
    *,
    max_bytes: int = MAX_WEBHOOK_BODY_BYTES,
) -> bytes:
    """Read request body up to max_bytes, aborting oversize streams with HTTP 413."""
    content_length_raw = request.headers.get("content-length")
    if content_length_raw is not None:
        try:
            declared_length = int(content_length_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header.",
            ) from exc
        if declared_length < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header.",
            )
        if declared_length > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload exceeds maximum allowed size.",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload exceeds maximum allowed size.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _is_strict_positive_int(value: Any, *, max_value: int) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= max_value


def _extract_repo_meta(payload: dict[str, Any]) -> tuple[str, int | None, str, int | None]:
    repo_raw = payload.get("repository")
    if not isinstance(repo_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload missing 'repository' object.",
        )
    full_name_raw = repo_raw.get("full_name")
    if not isinstance(full_name_raw, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload missing 'repository.full_name'.",
        )
    try:
        full_name = validate_repo_full_name(full_name_raw)
    except GitHubAuthConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid 'repository.full_name'.",
        ) from exc

    gh_repo_id = repo_raw.get("id")
    if gh_repo_id is not None and not _is_strict_positive_int(gh_repo_id, max_value=_MAX_INT64):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid 'repository.id'.",
        )

    default_branch = repo_raw.get("default_branch")
    if default_branch is None:
        branch = "main"
    elif not isinstance(default_branch, str) or len(default_branch.strip()) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid 'repository.default_branch'.",
        )
    else:
        branch = default_branch.strip() or "main"

    inst_raw = payload.get("installation")
    inst_id: int | None = None
    if isinstance(inst_raw, dict) and inst_raw.get("id") is not None:
        raw_inst_id = inst_raw["id"]
        if not _is_strict_positive_int(raw_inst_id, max_value=_MAX_INT64):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid 'installation.id'.",
            )
        inst_id = raw_inst_id
    return full_name, gh_repo_id, branch, inst_id


def _classify_webhook(event_type: str, payload: dict[str, Any]) -> _ClassifiedWebhook:
    raw_action = payload.get("action")
    action: str | None = None
    if raw_action is not None:
        if not isinstance(raw_action, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook 'action'.",
            )
        cleaned_action = raw_action.strip()
        if len(cleaned_action) > 64:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook 'action' exceeds 64 characters.",
            )
        action = cleaned_action or None

    if event_type == "ping":
        return _IgnoredWebhook(ack_status="pong", action=action)

    if event_type == "pull_request":
        if action is None or action not in ACTIONABLE_PR_ACTIONS:
            return _IgnoredWebhook(ack_status="ignored", action=action)

        full_name, gh_repo_id, default_branch, inst_id = _extract_repo_meta(payload)
        pr_raw = payload.get("pull_request")
        if not isinstance(pr_raw, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload missing 'pull_request' object.",
            )
        pr_number = pr_raw.get("number")
        head_raw = pr_raw.get("head")
        head_sha_raw = head_raw.get("sha") if isinstance(head_raw, dict) else None
        base_raw = pr_raw.get("base")
        base_sha_raw = base_raw.get("sha") if isinstance(base_raw, dict) else None
        head_sha = head_sha_raw.strip() if isinstance(head_sha_raw, str) else ""
        base_sha = base_sha_raw.strip() if isinstance(base_sha_raw, str) else None
        before_raw = payload.get("before")
        before_sha_clean = before_raw.strip() if isinstance(before_raw, str) else ""
        before_sha = (
            before_sha_clean
            if before_sha_clean and len(before_sha_clean) <= 64 and before_sha_clean != _ZERO_SHA
            else None
        )
        updated_at_ms = _parse_iso_timestamp_ms(pr_raw.get("updated_at"))
        if (
            not _is_strict_positive_int(pr_number, max_value=_MAX_INT32)
            or not head_sha
            or len(head_sha) > 64
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload missing pull_request number or head SHA.",
            )
        return _QueuedPRWebhook(
            action=action,
            repo_full_name=full_name,
            github_repo_id=gh_repo_id,
            default_branch=default_branch,
            installation_id=inst_id,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha if base_sha and len(base_sha) <= 64 else None,
            before_sha=before_sha,
            updated_at_ms=updated_at_ms,
        )

    if event_type == "push":
        repo_raw = payload.get("repository")
        if not isinstance(repo_raw, dict):
            return _IgnoredWebhook(ack_status="ignored", action=action)
        full_name, gh_repo_id, default_branch, inst_id = _extract_repo_meta(payload)
        ref = payload.get("ref")
        after_raw = payload.get("after")
        before_raw = payload.get("before")
        after_sha = after_raw.strip() if isinstance(after_raw, str) else ""
        before_sha = before_raw.strip() if isinstance(before_raw, str) else None
        deleted = payload.get("deleted") is True
        expected_ref = f"refs/heads/{default_branch}"
        if (
            not deleted
            and isinstance(ref, str)
            and ref == expected_ref
            and bool(after_sha)
            and len(after_sha) <= 64
            and after_sha != _ZERO_SHA
        ):
            return _QueuedPushWebhook(
                action=action,
                repo_full_name=full_name,
                github_repo_id=gh_repo_id,
                default_branch=default_branch,
                installation_id=inst_id,
                ref=ref,
                before_sha=before_sha if before_sha and len(before_sha) <= 64 else None,
                after_sha=after_sha,
            )
        return _IgnoredWebhook(ack_status="ignored", action=action)

    return _IgnoredWebhook(ack_status="ignored", action=action)


def _stale_repo_name(full_name: str, suffix: str) -> str:
    tag = f"__stale_{suffix}"
    return f"{full_name[: max(0, 255 - len(tag))]}{tag}"


async def _apply_repo_upsert(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
) -> Repository | None:
    if github_repo_id is None:
        existing = (
            await session.execute(select(Repository).where(Repository.full_name == full_name))
        ).scalar_one_or_none()
        if existing is None:
            return None
        needs_update = existing.default_branch != default_branch or (
            installation_id is not None and existing.installation_id != installation_id
        )
        if needs_update:
            existing.default_branch = default_branch
            if installation_id is not None:
                existing.installation_id = installation_id
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
            by_name.full_name = _stale_repo_name(by_name.full_name, suffix)
            await session.flush()
        needs_update = (
            by_id.full_name != full_name
            or by_id.default_branch != default_branch
            or (installation_id is not None and by_id.installation_id != installation_id)
        )
        if needs_update:
            by_id.full_name = full_name
            by_id.default_branch = default_branch
            if installation_id is not None:
                by_id.installation_id = installation_id
            await session.flush()
        return by_id

    if by_name is not None:
        if by_name.github_repo_id is None:
            by_name.github_repo_id = github_repo_id
            by_name.default_branch = default_branch
            if installation_id is not None:
                by_name.installation_id = installation_id
            await session.flush()
            return by_name
        by_name.full_name = _stale_repo_name(by_name.full_name, str(by_name.github_repo_id))
        await session.flush()
        return None

    return None


async def _upsert_repository(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
) -> Repository:
    for _ in range(2):
        updated = await _apply_repo_upsert(
            session,
            github_repo_id=github_repo_id,
            full_name=full_name,
            default_branch=default_branch,
            installation_id=installation_id,
        )
        if updated is not None:
            return updated

        repo = Repository(
            github_repo_id=github_repo_id,
            full_name=full_name,
            default_branch=default_branch,
            installation_id=installation_id,
        )
        try:
            async with session.begin_nested():
                session.add(repo)
                await session.flush()
                return repo
        except IntegrityError:
            continue

    resolved = await _apply_repo_upsert(
        session,
        github_repo_id=github_repo_id,
        full_name=full_name,
        default_branch=default_branch,
        installation_id=installation_id,
    )
    if resolved is None:
        raise RuntimeError(f"Failed to upsert repository '{full_name}'.")
    return resolved


def _get_runtime_settings(request: Request) -> Settings:
    app_settings = getattr(request.app.state, "settings", None)
    if isinstance(app_settings, Settings):
        return app_settings
    return get_settings()


def _get_dispatcher(request: Request) -> WebhookDispatcher:
    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    if dispatcher is not None:
        return cast(WebhookDispatcher, dispatcher)
    return CeleryWebhookDispatcher()


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized.",
        )
    return cast(async_sessionmaker[AsyncSession], factory)


@router.post(
    "/github",
    response_model=WebhookAckResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_github_webhook(
    request: Request,
    response: Response,
) -> WebhookAckResponse:
    """Ingest GitHub webhooks with constant-time HMAC, atomic idempotency, and <100ms ACK."""
    active_settings = _get_runtime_settings(request)
    secret_obj = active_settings.github_webhook_secret
    secret = secret_obj.get_secret_value() if secret_obj is not None else ""
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub webhook secret is not configured.",
        )

    raw_body = await read_bounded_body(request)

    sig_header = request.headers.get("x-hub-signature-256")
    if not verify_github_signature(raw_body, sig_header, secret):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature.",
        )

    delivery_id = (request.headers.get("x-github-delivery") or "").strip()
    event_type = (request.headers.get("x-github-event") or "").strip()
    if not delivery_id or len(delivery_id) > 128 or not event_type or len(event_type) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid X-GitHub-Delivery / X-GitHub-Event headers.",
        )

    try:
        parsed_body = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be valid JSON.",
        ) from exc
    if not isinstance(parsed_body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object.",
        )

    classified = _classify_webhook(event_type, parsed_body)
    session_factory = _get_session_factory(request)

    if isinstance(classified, _IgnoredWebhook):
        async with session_factory() as session:
            insert_stmt = (
                pg_insert(WebhookEvent)
                .values(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    action=classified.action,
                    status="ignored",
                )
                .on_conflict_do_nothing(index_elements=[WebhookEvent.delivery_id])
                .returning(WebhookEvent.delivery_id)
            )
            inserted_delivery_id = (await session.execute(insert_stmt)).scalar_one_or_none()
            if inserted_delivery_id is None:
                await session.rollback()
                response.status_code = status.HTTP_200_OK
                logger.info(
                    "webhook_duplicate_ignored",
                    delivery_id=delivery_id,
                    github_event=event_type,
                )
                return WebhookAckResponse(
                    status="duplicate",
                    delivery_id=delivery_id,
                    event=event_type,
                )

            await session.commit()
            response.status_code = status.HTTP_200_OK
            logger.info(
                "webhook_event_ignored",
                delivery_id=delivery_id,
                github_event=event_type,
                action=classified.action,
            )
            return WebhookAckResponse(
                status=classified.ack_status,
                delivery_id=delivery_id,
                event=event_type,
            )

    # Phase 1: Claim delivery ('received', reclaim 'failed' or stale 'received') + commit Repo
    stale_cutoff = func.now() - timedelta(seconds=STALE_RECEIVED_SECONDS)
    async with session_factory() as session:
        claim_stmt = (
            pg_insert(WebhookEvent)
            .values(
                delivery_id=delivery_id,
                event_type=event_type,
                action=classified.action,
                status="received",
            )
            .on_conflict_do_update(
                index_elements=[WebhookEvent.delivery_id],
                set_={
                    "status": "received",
                    "event_type": event_type,
                    "action": classified.action,
                    "received_at": func.now(),
                },
                where=or_(
                    WebhookEvent.status == "failed",
                    and_(
                        WebhookEvent.status == "received",
                        WebhookEvent.received_at < stale_cutoff,
                    ),
                ),
            )
            .returning(WebhookEvent.delivery_id)
        )
        claimed_id = (await session.execute(claim_stmt)).scalar_one_or_none()
        if claimed_id is None:
            await session.rollback()
            response.status_code = status.HTTP_200_OK
            logger.info(
                "webhook_duplicate_ignored",
                delivery_id=delivery_id,
                github_event=event_type,
            )
            return WebhookAckResponse(
                status="duplicate",
                delivery_id=delivery_id,
                event=event_type,
            )

        repo = await _upsert_repository(
            session,
            github_repo_id=classified.github_repo_id,
            full_name=classified.repo_full_name,
            default_branch=classified.default_branch,
            installation_id=classified.installation_id,
        )
        repo_id_str = str(repo.id)
        repo_gh_id = repo.github_repo_id
        repo_full_name = repo.full_name
        repo_default_branch = repo.default_branch
        repo_inst_id = repo.installation_id
        await session.commit()

    # Phase 2: Dispatch to Celery outside the DB transaction so workers see committed state
    dispatcher = _get_dispatcher(request)
    try:
        if isinstance(classified, _QueuedPRWebhook):
            task_id = await dispatcher.dispatch_pull_request(
                PRWebhookDispatchPayload(
                    delivery_id=delivery_id,
                    repo_id=repo_id_str,
                    github_repo_id=repo_gh_id,
                    repo_full_name=repo_full_name,
                    default_branch=repo_default_branch,
                    installation_id=repo_inst_id,
                    pr_number=classified.pr_number,
                    head_sha=classified.head_sha,
                    base_sha=classified.base_sha,
                    before_sha=classified.before_sha,
                    updated_at_ms=classified.updated_at_ms,
                    action=classified.action,
                )
            )
        else:
            task_id = await dispatcher.dispatch_push(
                PushWebhookDispatchPayload(
                    delivery_id=delivery_id,
                    repo_id=repo_id_str,
                    github_repo_id=repo_gh_id,
                    repo_full_name=repo_full_name,
                    default_branch=repo_default_branch,
                    installation_id=repo_inst_id,
                    ref=classified.ref,
                    before_sha=classified.before_sha,
                    after_sha=classified.after_sha,
                )
            )
    except Exception as exc:
        try:
            async with session_factory() as fail_session:
                await fail_session.execute(
                    update(WebhookEvent)
                    .where(WebhookEvent.delivery_id == delivery_id)
                    .values(status="failed")
                )
                await fail_session.commit()
        except Exception as cleanup_exc:
            logger.warning(
                "webhook_mark_failed_error",
                delivery_id=delivery_id,
                error_type=type(cleanup_exc).__name__,
            )
        logger.error(
            "webhook_dispatch_failed",
            delivery_id=delivery_id,
            github_event=event_type,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background task broker unavailable; please retry delivery.",
        ) from exc

    # Phase 3: Mark event 'queued' after dispatch succeeds (best-effort; 'received' blocks dups)
    try:
        async with session_factory() as complete_session:
            await complete_session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.delivery_id == delivery_id)
                .values(status="queued")
            )
            await complete_session.commit()
    except Exception as exc:
        logger.warning(
            "webhook_mark_queued_failed",
            delivery_id=delivery_id,
            error_type=type(exc).__name__,
        )

    response.status_code = status.HTTP_202_ACCEPTED
    logger.info(
        "webhook_event_queued",
        delivery_id=delivery_id,
        github_event=event_type,
        action=classified.action,
        task_id=task_id,
    )
    return WebhookAckResponse(
        status="queued",
        delivery_id=delivery_id,
        event=event_type,
        task_id=task_id,
    )
