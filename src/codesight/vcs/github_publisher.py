"""Async HTTP/2 GitHub Checks & Batch Pull Request Review Publisher with Zero HTTP 422 Guard."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.vcs.github_auth import (
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)


@runtime_checkable
class AsyncSupersedeLockProtocol(Protocol):
    """Structural protocol satisfied by AsyncSupersedeLock for pre-publish SHA checks."""

    async def ensure_active(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        *,
        stage: str = "pre_publish",
    ) -> None: ...


type CheckConclusion = Literal["success", "neutral", "failure"]
type ReviewEvent = Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"]
type FindingSeverity = Literal["CRITICAL", "WARNING", "INFO"]

OUT_OF_HUNK_HEADING: Final[str] = (
    "### Findings Outside Diff Hunks (Out-of-Hunk / File-level Findings)"
)
MAX_CHECK_TEXT_CHARS: Final[int] = 65_535
DEFAULT_EMPTY_REVIEW_BODY: Final[str] = "CodeSight-AI review completed with no blocking issues."

_VALID_CONCLUSIONS: Final[frozenset[str]] = frozenset({"success", "neutral", "failure"})
_VALID_REVIEW_EVENTS: Final[frozenset[str]] = frozenset({"COMMENT", "APPROVE", "REQUEST_CHANGES"})
_SUGGESTION_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^([ \t]*(?:`{3,}|~{3,}))suggestion\b",
    re.MULTILINE | re.IGNORECASE,
)

logger = structlog.get_logger("codesight.vcs.publisher")


class GitHubPublishError(RuntimeError):
    """Raised on non-retryable GitHub Checks or Pull Request Review API failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ReviewCommentDraft(BaseModel):
    """Candidate PR review comment validated and partitioned before GitHub submission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    body: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    severity: FindingSeverity | None = None

    @field_validator("path", "body", mode="before")
    @classmethod
    def _validate_non_empty_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Field must be a string.")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be empty.")
        return cleaned

    @field_validator("line", "start_line", mode="before")
    @classmethod
    def _reject_bool_lines(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Line number must be a strict integer.")
        return value


@dataclass(frozen=True, slots=True)
class DowngradedComment:
    """Finding moved out of inline `comments[]` into the PR Summary to avoid HTTP 422."""

    draft: ReviewCommentDraft
    reason: str


@dataclass(frozen=True, slots=True)
class PreparedPullRequestReview:
    """Validated GitHub Review payload ready for a single batch POST."""

    body: str
    inline_comments: tuple[dict[str, Any], ...]
    downgraded_comments: tuple[DowngradedComment, ...]
    inline_drafts: tuple[ReviewCommentDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckRunRecord:
    """Metadata returned by GitHub Checks API on creation or completion."""

    check_run_id: int
    name: str
    status: str
    conclusion: str | None = None
    html_url: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedReviewRecord:
    """Result metadata returned after publishing a batch PR review."""

    review_id: int | None
    html_url: str | None
    state: str
    inline_count: int
    downgraded_count: int
    used_422_fallback: bool = False


def _neutralize_suggestion_fences(text: str) -> str:
    return _SUGGESTION_FENCE_RE.sub(r"\1", text)


def _format_location_label(draft: ReviewCommentDraft) -> str:
    if draft.line is None:
        return "file-level"
    if draft.start_line is not None and draft.start_line != draft.line:
        return f"L{draft.start_line}-L{draft.line}"
    return f"L{draft.line}"


def _format_downgraded_bullet(item: DowngradedComment) -> str:
    severity_tag = f"**[{item.draft.severity}]** " if item.draft.severity else ""
    loc = _format_location_label(item.draft)
    safe_body = _neutralize_suggestion_fences(item.draft.body)
    return f"- {severity_tag}`{item.draft.path}` ({loc}) `[{item.reason}]`:\n\n{safe_body}"


def _lookup_parsed_file(
    parsed_diff: ParsedPullRequestDiff,
    raw_path: str,
) -> ParsedFileDiff | None:
    direct = parsed_diff.get_file(raw_path)
    if direct is not None:
        return direct
    normalized = normalize_diff_path(raw_path)
    if normalized != raw_path:
        return parsed_diff.get_file(normalized)
    return None


def _truncate_check_field(text: str, limit: int = MAX_CHECK_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _is_rate_limited_403(headers: httpx.Headers) -> bool:
    return bool(headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0")


def _validate_positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def partition_review_comments(
    parsed_diff: ParsedPullRequestDiff,
    drafts: Sequence[ReviewCommentDraft],
    *,
    summary_body: str = "",
) -> PreparedPullRequestReview:
    """Partition comment drafts into valid RIGHT-side inline payloads vs Summary findings."""
    inline_comments: list[dict[str, Any]] = []
    inline_drafts: list[ReviewCommentDraft] = []
    downgraded: list[DowngradedComment] = []

    for draft in drafts:
        parsed_file = _lookup_parsed_file(parsed_diff, draft.path)
        if parsed_file is None:
            downgraded.append(DowngradedComment(draft=draft, reason="file_not_in_diff"))
            continue

        if parsed_file.change_type == "deleted":
            downgraded.append(DowngradedComment(draft=draft, reason="deleted_file"))
            continue

        if draft.line is None:
            downgraded.append(DowngradedComment(draft=draft, reason="file_level"))
            continue

        if draft.start_line is None or draft.start_line == draft.line:
            if parsed_file.is_line_commentable(draft.line):
                inline_comments.append(
                    {
                        "path": parsed_file.file_path,
                        "line": draft.line,
                        "side": "RIGHT",
                        "body": draft.body,
                    }
                )
                inline_drafts.append(draft.model_copy(update={"path": parsed_file.file_path}))
            else:
                downgraded.append(DowngradedComment(draft=draft, reason="line_outside_hunk"))
            continue

        if draft.start_line > draft.line:
            downgraded.append(DowngradedComment(draft=draft, reason="invalid_range"))
            continue

        if parsed_file.is_range_commentable(draft.start_line, draft.line):
            inline_comments.append(
                {
                    "path": parsed_file.file_path,
                    "start_line": draft.start_line,
                    "start_side": "RIGHT",
                    "line": draft.line,
                    "side": "RIGHT",
                    "body": draft.body,
                }
            )
            inline_drafts.append(draft.model_copy(update={"path": parsed_file.file_path}))
            continue

        both_in_diff = parsed_file.is_line_commentable(
            draft.start_line
        ) and parsed_file.is_line_commentable(draft.line)
        reason = "cross_hunk_range" if both_in_diff else "line_outside_hunk"
        downgraded.append(DowngradedComment(draft=draft, reason=reason))

    sections: list[str] = []
    cleaned_summary = summary_body.strip()
    if cleaned_summary:
        sections.append(cleaned_summary)

    if downgraded:
        bullets = "\n\n".join(_format_downgraded_bullet(item) for item in downgraded)
        sections.append(f"{OUT_OF_HUNK_HEADING}\n\n{bullets}")

    merged_body = "\n\n".join(sections).strip()
    if not merged_body and not inline_comments:
        merged_body = DEFAULT_EMPTY_REVIEW_BODY

    return PreparedPullRequestReview(
        body=merged_body,
        inline_comments=tuple(inline_comments),
        downgraded_comments=tuple(downgraded),
        inline_drafts=tuple(inline_drafts),
    )


def _fold_remaining_into_summary(
    existing_body: str,
    remaining_inline: Sequence[ReviewCommentDraft | dict[str, Any]],
) -> str:
    fallback_bullets: list[str] = []
    for item in remaining_inline:
        if isinstance(item, ReviewCommentDraft):
            draft = item
        else:
            draft = ReviewCommentDraft(
                path=str(item["path"]),
                body=str(item["body"]),
                line=item.get("line"),
                start_line=item.get("start_line"),
                severity=item.get("severity"),
            )
        fallback_bullets.append(
            _format_downgraded_bullet(
                DowngradedComment(draft=draft, reason="reactive_422_fallback")
            )
        )

    joined_bullets = "\n\n".join(fallback_bullets)
    base = existing_body.strip()
    if OUT_OF_HUNK_HEADING in base:
        return f"{base}\n\n{joined_bullets}".strip()
    if base:
        return f"{base}\n\n{OUT_OF_HUNK_HEADING}\n\n{joined_bullets}".strip()
    return f"{OUT_OF_HUNK_HEADING}\n\n{joined_bullets}".strip()


class GitHubReviewPublisher:
    """Publishes GitHub Check Runs and batch Pull Request Reviews over an HTTP/2 pool."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._auth = auth_provider
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._owns_client and self._client.is_closed:
            raise RuntimeError("Injected HTTP client has been closed externally.")
        if self._client is None or (self._owns_client and self._client.is_closed):
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=self._timeout_seconds,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def __aenter__(self) -> Self:
        self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally managed HTTP/2 client pool without closing injected clients."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _send_json_request(
        self,
        method: str,
        path: str,
        *,
        slug: str,
        json_body: dict[str, Any],
        installation_id: int | None,
        allow_422: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        headers = await self._auth.get_auth_headers(
            installation_id=installation_id,
            repo_full_name=slug,
        )
        url = f"{self._base_url}{path}"
        client = self._ensure_client()

        try:
            # NFR-3: never log json_body or auth headers; emit structural metadata only.
            resp = await client.request(method, url, headers=headers, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "github_publisher_transport_error",
                repo=slug,
                method=method,
                error_type=type(exc).__name__,
            )
            raise RetryableTaskError(
                f"Transient network failure calling GitHub {method} {path}."
            ) from exc

        code = resp.status_code
        if code == 429 or (code == 403 and _is_rate_limited_403(resp.headers)):
            delay = parse_retry_after_header(resp.headers)
            logger.warning(
                "github_publisher_rate_limited",
                repo=slug,
                status_code=code,
                retry_after_seconds=delay,
            )
            raise RateLimitTaskError(
                f"GitHub rate limit ({code}) on {method} {path}.",
                retry_after_seconds=delay,
            )

        if code in {401, 403}:
            logger.error("github_publisher_auth_error", repo=slug, status_code=code)
            raise GitHubAuthError(
                f"GitHub API returned HTTP {code} on {method} {path}.",
                status_code=code,
            )

        if 500 <= code < 600:
            logger.warning("github_publisher_server_error", repo=slug, status_code=code)
            raise RetryableTaskError(f"GitHub API returned HTTP {code} on {method} {path}.")

        if code == 422 and allow_422:
            return code, {}

        if code >= 400:
            logger.error("github_publisher_client_error", repo=slug, status_code=code)
            raise GitHubPublishError(
                f"GitHub API rejected {method} {path} with HTTP {code}.",
                status_code=code,
            )

        try:
            parsed = resp.json()
        except ValueError as exc:
            raise GitHubPublishError(
                f"GitHub API returned invalid JSON on {method} {path}.",
                status_code=code,
            ) from exc

        if not isinstance(parsed, dict):
            raise GitHubPublishError(
                f"GitHub API returned unexpected payload shape on {method} {path}.",
                status_code=code,
            )
        return code, parsed

    async def create_check_run(
        self,
        repo_full_name: str,
        *,
        head_sha: str,
        name: str = "CodeSight-AI Review",
        status: Literal["queued", "in_progress"] = "in_progress",
        external_id: str | None = None,
        details_url: str | None = None,
        output_title: str | None = None,
        output_summary: str | None = None,
        installation_id: int | None = None,
    ) -> CheckRunRecord:
        """Create an in-progress or queued GitHub Check Run for head_sha."""
        slug = validate_repo_full_name(repo_full_name)
        clean_sha = head_sha.strip()
        if not clean_sha:
            raise ValueError("head_sha must not be empty.")

        payload: dict[str, Any] = {
            "name": name,
            "head_sha": clean_sha,
            "status": status,
            "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if external_id is not None:
            payload["external_id"] = external_id
        if details_url is not None:
            payload["details_url"] = details_url
        if output_title is not None or output_summary is not None:
            payload["output"] = {
                "title": _truncate_check_field(output_title or name),
                "summary": _truncate_check_field(output_summary or "CodeSight-AI review running."),
            }

        _, data = await self._send_json_request(
            "POST",
            f"/repos/{slug}/check-runs",
            slug=slug,
            json_body=payload,
            installation_id=installation_id,
        )
        run_id = data.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            raise GitHubPublishError("GitHub create check-run response missing integer 'id'.")

        record = CheckRunRecord(
            check_run_id=run_id,
            name=str(data.get("name") or name),
            status=str(data.get("status") or status),
            conclusion=str(data["conclusion"]) if data.get("conclusion") is not None else None,
            html_url=str(data["html_url"]) if data.get("html_url") is not None else None,
        )
        logger.info(
            "github_check_run_created",
            repo=slug,
            check_run_id=record.check_run_id,
            status=record.status,
        )
        return record

    async def complete_check_run(
        self,
        repo_full_name: str,
        check_run_id: int,
        *,
        conclusion: CheckConclusion,
        title: str,
        summary: str,
        text: str | None = None,
        details_url: str | None = None,
        installation_id: int | None = None,
    ) -> CheckRunRecord:
        """Finalize an existing GitHub Check Run with success, neutral, or failure conclusion."""
        slug = validate_repo_full_name(repo_full_name)
        valid_id = _validate_positive_int(check_run_id, name="check_run_id")
        if conclusion not in _VALID_CONCLUSIONS:
            raise ValueError(
                f"Invalid check run conclusion {conclusion!r}; "
                "expected 'success', 'neutral', or 'failure'."
            )

        output: dict[str, str] = {
            "title": _truncate_check_field(title),
            "summary": _truncate_check_field(summary),
        }
        if text is not None:
            output["text"] = _truncate_check_field(text)

        payload: dict[str, Any] = {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "output": output,
        }
        if details_url is not None:
            payload["details_url"] = details_url

        _, data = await self._send_json_request(
            "PATCH",
            f"/repos/{slug}/check-runs/{valid_id}",
            slug=slug,
            json_body=payload,
            installation_id=installation_id,
        )
        record = CheckRunRecord(
            check_run_id=valid_id,
            name=str(data.get("name") or "CodeSight-AI Review"),
            status=str(data.get("status") or "completed"),
            conclusion=str(data.get("conclusion") or conclusion),
            html_url=str(data["html_url"]) if data.get("html_url") is not None else None,
        )
        logger.info(
            "github_check_run_completed",
            repo=slug,
            check_run_id=record.check_run_id,
            conclusion=record.conclusion,
        )
        return record

    async def _ensure_not_superseded(
        self,
        supersede_lock: AsyncSupersedeLockProtocol,
        *,
        lock_repo_id: str,
        repo_slug: str,
        pull_number: int,
        head_sha: str,
        stage: str,
        check_run_id: int | None,
        installation_id: int | None,
    ) -> None:
        from codesight.worker.supersede_lock import PRSupersededError

        try:
            await supersede_lock.ensure_active(
                lock_repo_id,
                pull_number,
                head_sha,
                stage=stage,
            )
        except PRSupersededError:
            if check_run_id is not None:
                try:
                    await self.complete_check_run(
                        repo_slug,
                        check_run_id,
                        conclusion="neutral",
                        title="CodeSight-AI Review Superseded",
                        summary=(
                            "Skipped review publication because a newer commit superseded "
                            f"{head_sha[:12]}."
                        ),
                        installation_id=installation_id,
                    )
                except (RetryableTaskError, GitHubPublishError, GitHubAuthError) as cleanup_exc:
                    logger.warning(
                        "github_check_run_supersede_cleanup_failed",
                        repo=repo_slug,
                        check_run_id=check_run_id,
                        stage=stage,
                        error_type=type(cleanup_exc).__name__,
                    )
            raise

    async def publish_review(
        self,
        repo_full_name: str,
        pull_number: int,
        *,
        head_sha: str,
        parsed_diff: ParsedPullRequestDiff,
        drafts: Sequence[ReviewCommentDraft] = (),
        summary_body: str = "",
        event: ReviewEvent = "COMMENT",
        supersede_lock: AsyncSupersedeLockProtocol | None = None,
        repo_id: str | None = None,
        check_run_id: int | None = None,
        installation_id: int | None = None,
    ) -> PublishedReviewRecord:
        """Publish all review findings in one batch POST while guarding against HTTP 422."""
        slug = validate_repo_full_name(repo_full_name)
        valid_pr = _validate_positive_int(pull_number, name="pull_number")
        valid_check_run_id = (
            _validate_positive_int(check_run_id, name="check_run_id")
            if check_run_id is not None
            else None
        )
        clean_sha = head_sha.strip()
        if not clean_sha:
            raise ValueError("head_sha must not be empty.")
        if event not in _VALID_REVIEW_EVENTS:
            raise ValueError(
                f"Invalid review event {event!r}; "
                "expected 'COMMENT', 'APPROVE', or 'REQUEST_CHANGES'."
            )
        if supersede_lock is not None and not isinstance(
            supersede_lock, AsyncSupersedeLockProtocol
        ):
            raise TypeError("supersede_lock must implement AsyncSupersedeLockProtocol.")

        prepared = partition_review_comments(parsed_diff, drafts, summary_body=summary_body)
        lock_repo_id = repo_id if repo_id else slug
        if supersede_lock is None:
            logger.warning(
                "github_review_publish_without_supersede_lock",
                repo=slug,
                pull_number=valid_pr,
            )
        else:
            await self._ensure_not_superseded(
                supersede_lock,
                lock_repo_id=lock_repo_id,
                repo_slug=slug,
                pull_number=valid_pr,
                head_sha=clean_sha,
                stage="pre_publish",
                check_run_id=valid_check_run_id,
                installation_id=installation_id,
            )

        endpoint = f"/repos/{slug}/pulls/{valid_pr}/reviews"
        payload: dict[str, Any] = {
            "commit_id": clean_sha,
            "body": prepared.body,
            "event": event,
            "comments": list(prepared.inline_comments),
        }

        status_code, data = await self._send_json_request(
            "POST",
            endpoint,
            slug=slug,
            json_body=payload,
            installation_id=installation_id,
            allow_422=bool(prepared.inline_comments),
        )

        used_fallback = False
        inline_count = len(prepared.inline_comments)
        downgraded_count = len(prepared.downgraded_comments)

        if status_code == 422 and prepared.inline_comments:
            used_fallback = True
            logger.warning(
                "github_review_422_fallback_to_summary",
                repo=slug,
                pull_number=valid_pr,
                downgraded_count=inline_count,
            )
            if supersede_lock is not None:
                await self._ensure_not_superseded(
                    supersede_lock,
                    lock_repo_id=lock_repo_id,
                    repo_slug=slug,
                    pull_number=valid_pr,
                    head_sha=clean_sha,
                    stage="pre_422_fallback",
                    check_run_id=valid_check_run_id,
                    installation_id=installation_id,
                )
            fallback_payload = {
                "commit_id": clean_sha,
                "body": _fold_remaining_into_summary(
                    prepared.body,
                    prepared.inline_drafts or prepared.inline_comments,
                ),
                "event": event,
                "comments": [],
            }
            _, data = await self._send_json_request(
                "POST",
                endpoint,
                slug=slug,
                json_body=fallback_payload,
                installation_id=installation_id,
                allow_422=False,
            )
            downgraded_count += inline_count
            inline_count = 0

        raw_id = data.get("id")
        review_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
        record = PublishedReviewRecord(
            review_id=review_id,
            html_url=str(data["html_url"]) if data.get("html_url") is not None else None,
            state=str(data.get("state") or event),
            inline_count=inline_count,
            downgraded_count=downgraded_count,
            used_422_fallback=used_fallback,
        )
        logger.info(
            "github_review_published",
            repo=slug,
            pull_number=valid_pr,
            review_id=record.review_id,
            review_event=event,
            inline_count=record.inline_count,
            downgraded_count=record.downgraded_count,
            used_422_fallback=record.used_422_fallback,
        )
        return record
