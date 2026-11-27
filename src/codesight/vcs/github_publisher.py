"""Async HTTP/2 GitHub Checks & Batch Pull Request Review Publisher with Zero HTTP 422 Guard."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from codesight.agents.schemas import neutralize_suggestion_fences
from codesight.ast_engine.suggestion_validator import TreeSitterSuggestionValidator
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    is_rate_limited_403,
    parse_retry_after_header,
)
from codesight.diff.file_filter import normalize_diff_path, resolve_after_source_for_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.vcs._http import (
    METHOD_PRESERVING_REDIRECT_STATUSES,
    origin_tuple,
    resolve_same_origin_redirect,
    validate_positive_int,
)
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
MAX_REVIEW_BODY_CHARS: Final[int] = 65_535
_MAX_TRACKED_CHECK_RUNS: Final[int] = 1024
DEFAULT_EMPTY_REVIEW_BODY: Final[str] = "CodeSight-AI review completed with no blocking issues."
DEFAULT_REQUEST_CHANGES_BODY: Final[str] = (
    "CodeSight-AI requested changes based on inline review findings."
)
_TRUNCATION_FOOTER: Final[str] = "\n\n*(truncated to fit GitHub API length limit)*"

_VALID_CONCLUSIONS: Final[frozenset[str]] = frozenset({"success", "neutral", "failure"})
_VALID_REVIEW_EVENTS: Final[frozenset[str]] = frozenset({"COMMENT", "APPROVE", "REQUEST_CHANGES"})
_CODE_FENCE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)
_BACKTICK_RUN_RE: Final[re.Pattern[str]] = re.compile(r"`+")
_CONTROL_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

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


def format_markdown_code_span(raw_text: str) -> str:
    """Wrap an untrusted filename/identifier in a CommonMark-safe inline code span."""
    sanitized = _CONTROL_CHAR_RE.sub("", raw_text)
    max_ticks = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(sanitized)), default=0)
    fence = "`" * (max_ticks + 1)
    if sanitized.startswith(("`", " ")) or sanitized.endswith(("`", " ")):
        return f"{fence} {sanitized} {fence}"
    return f"{fence}{sanitized}{fence}"


def truncate_markdown_body(text: str, limit: int = MAX_REVIEW_BODY_CHARS) -> str:
    """Truncate Markdown text to <= limit chars while closing any dangling code fence."""
    if len(text) <= limit:
        return text

    open_fence_reserve = 8  # room for "\n```\n" if a code fence is left unclosed
    budget = max(0, limit - len(_TRUNCATION_FOOTER) - open_fence_reserve)
    prefix = text[:budget].rstrip()

    open_fence: str | None = None
    for match in _CODE_FENCE_LINE_RE.finditer(prefix):
        marker = match.group(1)
        if open_fence is None:
            open_fence = marker
        elif marker[0] == open_fence[0] and len(marker) >= len(open_fence):
            open_fence = None

    if open_fence is not None:
        closing = f"\n{open_fence[0] * min(len(open_fence), 3)}"
        candidate = f"{prefix}{closing}{_TRUNCATION_FOOTER}"
    else:
        candidate = f"{prefix}{_TRUNCATION_FOOTER}"

    return candidate[:limit]


def _contains_self_review_phrase(text: str) -> bool:
    lowered = text.lower()
    return (
        "own pull request" in lowered
        or "can not request changes" in lowered
        or "cannot request changes" in lowered
    )


def _extract_github_error_meta(resp: httpx.Response) -> dict[str, Any]:
    """Extract NFR-3-safe structural GitHub API error fields (no raw code or request echo)."""
    try:
        parsed = resp.json()
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    meta: dict[str, Any] = {}
    self_review_rejected = False
    msg = parsed.get("message")
    if isinstance(msg, str) and msg.strip():
        clean_msg = msg.strip()[:200]
        meta["github_message"] = clean_msg
        if _contains_self_review_phrase(clean_msg):
            self_review_rejected = True
    raw_errors = parsed.get("errors")
    if isinstance(raw_errors, list):
        compact: list[dict[str, str]] = []
        for entry in raw_errors[:5]:
            if isinstance(entry, str):
                if _contains_self_review_phrase(entry):
                    self_review_rejected = True
            elif isinstance(entry, dict):
                entry_msg = entry.get("message")
                if isinstance(entry_msg, str) and _contains_self_review_phrase(entry_msg):
                    self_review_rejected = True
                item: dict[str, str] = {}
                for field_key in ("resource", "field", "code"):
                    val = entry.get(field_key)
                    if isinstance(val, str) and val.strip():
                        item[field_key] = val.strip()[:64]
                if item:
                    compact.append(item)
        if compact:
            meta["github_errors"] = compact
    if self_review_rejected:
        meta["self_review_rejected"] = True
    return meta


def _format_location_label(draft: ReviewCommentDraft) -> str:
    if draft.line is None:
        return "file-level"
    if draft.start_line is not None and draft.start_line != draft.line:
        return f"L{draft.start_line}-L{draft.line}"
    return f"L{draft.line}"


def _format_downgraded_bullet(item: DowngradedComment) -> str:
    severity_tag = f"**[{item.draft.severity}]** " if item.draft.severity else ""
    loc = _format_location_label(item.draft)
    safe_path = format_markdown_code_span(item.draft.path)
    safe_body = neutralize_suggestion_fences(item.draft.body)
    return f"- {severity_tag}{safe_path} ({loc}) `[{item.reason}]`:\n\n{safe_body}"


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


def partition_review_comments(
    parsed_diff: ParsedPullRequestDiff,
    drafts: Sequence[ReviewCommentDraft],
    *,
    summary_body: str = "",
    after_sources_by_file: Mapping[str, str | bytes] | None = None,
    suggestion_validator: TreeSitterSuggestionValidator | None = None,
) -> PreparedPullRequestReview:
    """Partition comment drafts into valid RIGHT-side inline payloads vs Summary findings."""
    inline_comments: list[dict[str, Any]] = []
    inline_drafts: list[ReviewCommentDraft] = []
    downgraded: list[DowngradedComment] = []
    active_validator = (
        suggestion_validator
        if suggestion_validator is not None
        else (TreeSitterSuggestionValidator() if after_sources_by_file is not None else None)
    )

    def _sanitize_inline_body(
        parsed_file: ParsedFileDiff,
        body: str,
        *,
        start_line: int | None,
        line: int,
    ) -> str:
        if active_validator is None:
            return body
        sanitized, _, _ = active_validator.sanitize_markdown_comment(
            parsed_file.file_path,
            body,
            start_line=start_line,
            line=line,
            after_source=resolve_after_source_for_path(
                after_sources_by_file, parsed_file.file_path
            ),
            parsed_file=parsed_file,
        )
        return sanitized

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
                comment_body = _sanitize_inline_body(
                    parsed_file, draft.body, start_line=None, line=draft.line
                )
                inline_comments.append(
                    {
                        "path": parsed_file.file_path,
                        "line": draft.line,
                        "side": "RIGHT",
                        "body": truncate_markdown_body(comment_body),
                    }
                )
                inline_drafts.append(
                    draft.model_copy(update={"path": parsed_file.file_path, "body": comment_body})
                )
            else:
                downgraded.append(DowngradedComment(draft=draft, reason="line_outside_hunk"))
            continue

        if draft.start_line > draft.line:
            downgraded.append(DowngradedComment(draft=draft, reason="invalid_range"))
            continue

        if parsed_file.is_range_commentable(draft.start_line, draft.line):
            comment_body = _sanitize_inline_body(
                parsed_file, draft.body, start_line=draft.start_line, line=draft.line
            )
            inline_comments.append(
                {
                    "path": parsed_file.file_path,
                    "start_line": draft.start_line,
                    "start_side": "RIGHT",
                    "line": draft.line,
                    "side": "RIGHT",
                    "body": truncate_markdown_body(comment_body),
                }
            )
            inline_drafts.append(
                draft.model_copy(update={"path": parsed_file.file_path, "body": comment_body})
            )
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
    merged_body = truncate_markdown_body(merged_body)

    return PreparedPullRequestReview(
        body=merged_body,
        inline_comments=tuple(inline_comments),
        downgraded_comments=tuple(downgraded),
        inline_drafts=tuple(inline_drafts),
    )


def _is_self_review_422(error_meta: Mapping[str, Any]) -> bool:
    """Return True when GitHub rejects REQUEST_CHANGES because the reviewer owns the PR."""
    if error_meta.get("self_review_rejected") is True:
        return True
    fragments: list[str] = []
    msg = error_meta.get("github_message")
    if isinstance(msg, str):
        fragments.append(msg)
    raw_errors = error_meta.get("github_errors")
    if isinstance(raw_errors, list):
        for entry in raw_errors:
            if isinstance(entry, str):
                fragments.append(entry)
            elif isinstance(entry, Mapping):
                for val in entry.values():
                    if isinstance(val, str):
                        fragments.append(val)
    return _contains_self_review_phrase(" ".join(fragments))


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
        merged = f"{base}\n\n{joined_bullets}".strip()
    elif base:
        merged = f"{base}\n\n{OUT_OF_HUNK_HEADING}\n\n{joined_bullets}".strip()
    else:
        merged = f"{OUT_OF_HUNK_HEADING}\n\n{joined_bullets}".strip()
    return truncate_markdown_body(merged)


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
        self._completed_check_runs: OrderedDict[tuple[str, int], None] = OrderedDict()

    def _mark_check_run_completed(self, slug: str, check_run_id: int) -> None:
        key = (slug, check_run_id)
        self._completed_check_runs[key] = None
        self._completed_check_runs.move_to_end(key)
        while len(self._completed_check_runs) > _MAX_TRACKED_CHECK_RUNS:
            self._completed_check_runs.popitem(last=False)

    def has_completed_check_run(self, repo_full_name: str, check_run_id: int) -> bool:
        """Return True if this publisher already observed check_run_id as completed."""
        slug = validate_repo_full_name(repo_full_name)
        return (slug, check_run_id) in self._completed_check_runs

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
        max_redirects: int = 3,
    ) -> tuple[int, dict[str, Any]]:
        headers = await self._auth.get_auth_headers(
            installation_id=installation_id,
            repo_full_name=slug,
        )
        current_url = f"{self._base_url}{path}"
        expected_origin = origin_tuple(self._base_url)
        client = self._ensure_client()

        resp: httpx.Response | None = None
        for _ in range(max_redirects + 1):
            try:
                # NFR-3: never log json_body or auth headers; emit structural metadata only.
                resp = await client.request(
                    method,
                    current_url,
                    headers=headers,
                    json=json_body,
                    follow_redirects=False,
                )
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

            if resp.status_code in METHOD_PRESERVING_REDIRECT_STATUSES:
                current_url = resolve_same_origin_redirect(
                    current_url,
                    resp,
                    expected_origin,
                    error_factory=lambda msg, sc: GitHubPublishError(msg, status_code=sc),
                )
                continue
            break
        else:
            raise GitHubPublishError(
                f"Exceeded maximum same-origin redirects calling GitHub {method} {path}.",
                status_code=resp.status_code if resp is not None else None,
            )

        code = resp.status_code
        if code == 429 or (code == 403 and is_rate_limited_403(resp.headers)):
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
            if code == 401 and installation_id is not None:
                await self._auth.invalidate_installation_token(installation_id)
            logger.error("github_publisher_auth_error", repo=slug, status_code=code)
            raise GitHubAuthError(
                f"GitHub API returned HTTP {code} on {method} {path}.",
                status_code=code,
            )

        if 500 <= code < 600:
            logger.warning("github_publisher_server_error", repo=slug, status_code=code)
            raise RetryableTaskError(f"GitHub API returned HTTP {code} on {method} {path}.")

        if code == 422 and allow_422:
            return code, _extract_github_error_meta(resp)

        if not (200 <= code < 300):
            err_meta = _extract_github_error_meta(resp)
            logger.error(
                "github_publisher_client_error",
                repo=slug,
                status_code=code,
                **err_meta,
            )
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
        valid_id = validate_positive_int(check_run_id, name="check_run_id")
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
        self._mark_check_run_completed(slug, valid_id)
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
            if check_run_id is not None and not self.has_completed_check_run(
                repo_slug, check_run_id
            ):
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
                except GitHubPublishError as pub_exc:
                    if pub_exc.status_code == 422:
                        self._mark_check_run_completed(repo_slug, check_run_id)
                        logger.info(
                            "github_check_run_already_completed",
                            repo=repo_slug,
                            check_run_id=check_run_id,
                            stage=stage,
                        )
                    else:
                        logger.warning(
                            "github_check_run_supersede_cleanup_failed",
                            repo=repo_slug,
                            check_run_id=check_run_id,
                            stage=stage,
                            error_type=type(pub_exc).__name__,
                        )
                except (RetryableTaskError, GitHubAuthError) as cleanup_exc:
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
        after_sources_by_file: Mapping[str, str | bytes] | None = None,
        suggestion_validator: TreeSitterSuggestionValidator | None = None,
    ) -> PublishedReviewRecord:
        """Publish all review findings in one batch POST while guarding against HTTP 422."""
        slug = validate_repo_full_name(repo_full_name)
        valid_pr = validate_positive_int(pull_number, name="pull_number")
        valid_check_run_id = (
            validate_positive_int(check_run_id, name="check_run_id")
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

        prepared = partition_review_comments(
            parsed_diff,
            drafts,
            summary_body=summary_body,
            after_sources_by_file=after_sources_by_file,
            suggestion_validator=suggestion_validator,
        )
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

        review_body = prepared.body
        effective_event: ReviewEvent = event
        if effective_event == "REQUEST_CHANGES" and not review_body:
            review_body = DEFAULT_REQUEST_CHANGES_BODY

        endpoint = f"/repos/{slug}/pulls/{valid_pr}/reviews"
        payload: dict[str, Any] = {
            "commit_id": clean_sha,
            "body": review_body,
            "event": effective_event,
            "comments": list(prepared.inline_comments),
        }

        status_code, data = await self._send_json_request(
            "POST",
            endpoint,
            slug=slug,
            json_body=payload,
            installation_id=installation_id,
            allow_422=bool(prepared.inline_comments) or effective_event == "REQUEST_CHANGES",
        )

        used_fallback = False
        inline_count = len(prepared.inline_comments)
        downgraded_count = len(prepared.downgraded_comments)

        if (
            status_code == 422
            and effective_event == "REQUEST_CHANGES"
            and _is_self_review_422(data)
        ):
            logger.warning(
                "github_review_self_review_422_fallback",
                repo=slug,
                pull_number=valid_pr,
                **data,
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
            effective_event = "COMMENT"
            payload["event"] = effective_event
            status_code, data = await self._send_json_request(
                "POST",
                endpoint,
                slug=slug,
                json_body=payload,
                installation_id=installation_id,
                allow_422=bool(prepared.inline_comments),
            )

        if status_code == 422 and prepared.inline_comments:
            used_fallback = True
            logger.warning(
                "github_review_422_fallback_to_summary",
                repo=slug,
                pull_number=valid_pr,
                downgraded_count=inline_count,
                **data,
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
                    review_body,
                    prepared.inline_drafts or prepared.inline_comments,
                ),
                "event": effective_event,
                "comments": [],
            }
            fb_status, data = await self._send_json_request(
                "POST",
                endpoint,
                slug=slug,
                json_body=fallback_payload,
                installation_id=installation_id,
                allow_422=(effective_event == "REQUEST_CHANGES"),
            )
            if fb_status == 422:
                if effective_event == "REQUEST_CHANGES" and _is_self_review_422(data):
                    logger.warning(
                        "github_review_self_review_422_fallback",
                        repo=slug,
                        pull_number=valid_pr,
                        **data,
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
                    effective_event = "COMMENT"
                    fallback_payload["event"] = effective_event
                    _, data = await self._send_json_request(
                        "POST",
                        endpoint,
                        slug=slug,
                        json_body=fallback_payload,
                        installation_id=installation_id,
                        allow_422=False,
                    )
                else:
                    raise GitHubPublishError(
                        f"GitHub API rejected POST {endpoint} with HTTP 422.",
                        status_code=422,
                    )
            downgraded_count += inline_count
            inline_count = 0
        elif status_code == 422:
            raise GitHubPublishError(
                f"GitHub API rejected POST {endpoint} with HTTP 422.",
                status_code=422,
            )

        raw_id = data.get("id")
        review_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
        record = PublishedReviewRecord(
            review_id=review_id,
            html_url=str(data["html_url"]) if data.get("html_url") is not None else None,
            state=str(data.get("state") or effective_event),
            inline_count=inline_count,
            downgraded_count=downgraded_count,
            used_422_fallback=used_fallback,
        )
        logger.info(
            "github_review_published",
            repo=slug,
            pull_number=valid_pr,
            review_id=record.review_id,
            review_event=effective_event,
            inline_count=record.inline_count,
            downgraded_count=record.downgraded_count,
            used_422_fallback=record.used_422_fallback,
        )
        return record
