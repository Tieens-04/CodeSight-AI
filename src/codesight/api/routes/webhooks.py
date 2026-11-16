"""GitHub webhook gateway route (<100ms ACK, HMAC-SHA256, Postgres idempotency)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, TypeGuard, cast

import structlog
from celery import Celery
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight.core.config import Settings, get_settings
from codesight.db.models import Repository, WebhookEvent
from codesight.vcs.github_auth import GitHubAuthConfigError, validate_repo_full_name
from codesight.worker.celery_app import celery_app

MAX_WEBHOOK_BODY_BYTES: Final[int] = 25 * 1024 * 1024  # 25 MiB ceiling for 512m container
ACTIONABLE_PR_ACTIONS: Final[frozenset[str]] = frozenset({"opened", "synchronize", "reopened"})
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

    def __init__(self, app: Celery | None = None) -> None:
        self._app = app

    async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
        target = self._app if self._app is not None else celery_app
        result = await asyncio.to_thread(
            target.send_task,
            "codesight.webhook.handle_pr_event",
            args=(payload.model_dump(mode="json"),),
        )
        return str(result.id)

    async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
        target = self._app if self._app is not None else celery_app
        result = await asyncio.to_thread(
            target.send_task,
            "codesight.webhook.handle_push_event",
            args=(payload.model_dump(mode="json"),),
        )
        return str(result.id)


@dataclass(frozen=True)
class _ClassifiedWebhook:
    target_status: Literal["queued", "ignored"]
    ack_status: Literal["queued", "ignored", "pong"]
    action: str | None
    repo_full_name: str | None = None
    github_repo_id: int | None = None
    default_branch: str = "main"
    installation_id: int | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    ref: str | None = None
    before_sha: str | None = None
    after_sha: str | None = None


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
        return _ClassifiedWebhook(target_status="ignored", ack_status="pong", action=action)

    if event_type == "pull_request":
        if action not in ACTIONABLE_PR_ACTIONS:
            return _ClassifiedWebhook(target_status="ignored", ack_status="ignored", action=action)

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
        if (
            not _is_strict_positive_int(pr_number, max_value=_MAX_INT32)
            or not head_sha
            or len(head_sha) > 64
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload missing pull_request number or head SHA.",
            )
        return _ClassifiedWebhook(
            target_status="queued",
            ack_status="queued",
            action=action,
            repo_full_name=full_name,
            github_repo_id=gh_repo_id,
            default_branch=default_branch,
            installation_id=inst_id,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha if base_sha and len(base_sha) <= 64 else None,
        )

    if event_type == "push":
        repo_raw = payload.get("repository")
        if not isinstance(repo_raw, dict):
            return _ClassifiedWebhook(target_status="ignored", ack_status="ignored", action=action)
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
            return _ClassifiedWebhook(
                target_status="queued",
                ack_status="queued",
                action=action,
                repo_full_name=full_name,
                github_repo_id=gh_repo_id,
                default_branch=default_branch,
                installation_id=inst_id,
                ref=ref,
                before_sha=before_sha if before_sha and len(before_sha) <= 64 else None,
                after_sha=after_sha,
            )
        return _ClassifiedWebhook(target_status="ignored", ack_status="ignored", action=action)

    return _ClassifiedWebhook(target_status="ignored", ack_status="ignored", action=action)


async def _upsert_repository(
    session: AsyncSession,
    *,
    github_repo_id: int | None,
    full_name: str,
    default_branch: str,
    installation_id: int | None,
) -> Repository:
    match_expr = (
        or_(Repository.github_repo_id == github_repo_id, Repository.full_name == full_name)
        if github_repo_id is not None
        else (Repository.full_name == full_name)
    )
    existing = (await session.execute(select(Repository).where(match_expr))).scalar_one_or_none()
    if existing is not None:
        if github_repo_id is not None:
            existing.github_repo_id = github_repo_id
        existing.full_name = full_name
        existing.default_branch = default_branch
        if installation_id is not None:
            existing.installation_id = installation_id
        await session.flush()
        return existing

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
        concurrent = (
            await session.execute(select(Repository).where(match_expr))
        ).scalar_one()
        if github_repo_id is not None:
            concurrent.github_repo_id = github_repo_id
        concurrent.full_name = full_name
        concurrent.default_branch = default_branch
        if installation_id is not None:
            concurrent.installation_id = installation_id
        await session.flush()
        return concurrent


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

    async with session_factory() as session:
        insert_stmt = (
            pg_insert(WebhookEvent)
            .values(
                delivery_id=delivery_id,
                event_type=event_type,
                action=classified.action,
                status=classified.target_status,
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

        if classified.target_status == "ignored":
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

        assert classified.repo_full_name is not None
        repo = await _upsert_repository(
            session,
            github_repo_id=classified.github_repo_id,
            full_name=classified.repo_full_name,
            default_branch=classified.default_branch,
            installation_id=classified.installation_id,
        )

        dispatcher = _get_dispatcher(request)
        try:
            if event_type == "pull_request":
                assert classified.pr_number is not None and classified.head_sha is not None
                assert classified.action is not None
                task_id = await dispatcher.dispatch_pull_request(
                    PRWebhookDispatchPayload(
                        delivery_id=delivery_id,
                        repo_id=str(repo.id),
                        github_repo_id=repo.github_repo_id,
                        repo_full_name=repo.full_name,
                        default_branch=repo.default_branch,
                        installation_id=repo.installation_id,
                        pr_number=classified.pr_number,
                        head_sha=classified.head_sha,
                        base_sha=classified.base_sha,
                        action=classified.action,
                    )
                )
            else:
                assert classified.ref is not None and classified.after_sha is not None
                task_id = await dispatcher.dispatch_push(
                    PushWebhookDispatchPayload(
                        delivery_id=delivery_id,
                        repo_id=str(repo.id),
                        github_repo_id=repo.github_repo_id,
                        repo_full_name=repo.full_name,
                        default_branch=repo.default_branch,
                        installation_id=repo.installation_id,
                        ref=classified.ref,
                        before_sha=classified.before_sha,
                        after_sha=classified.after_sha,
                    )
                )
        except Exception as exc:
            await session.rollback()
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

        await session.commit()
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
