"""Resilient .codesight.yaml configuration schema, SHA-256 config_hash, and GitHub loader."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final, Literal, cast
from urllib.parse import quote

import httpx
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)
from codesight.vcs.github_auth import (
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

MAX_REPO_CONFIG_BYTES: Final[int] = 65_536  # 64 KiB ceiling
MAX_IGNORE_PATHS: Final[int] = 100
MAX_IGNORE_PATTERN_CHARS: Final[int] = 256
MAX_CUSTOM_RULES_CHARS: Final[int] = 4_000
CONFIG_FILENAMES: Final[tuple[str, str]] = (".codesight.yaml", ".codesight.yml")

type ReviewAction = Literal["COMMENT", "REQUEST_CHANGES"]

logger = structlog.get_logger("codesight.config_reader")


class RepoConfig(BaseModel):
    """Validated repository review policy loaded from .codesight.yaml (or safe defaults)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    min_debt_score: int = Field(default=60, ge=0, le=100)
    review_action: ReviewAction = "COMMENT"
    ignore_paths: tuple[str, ...] = ()
    custom_rules: str = ""

    @field_validator("min_debt_score", mode="before")
    @classmethod
    def _validate_strict_int(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("min_debt_score must be a strict integer between 0 and 100.")
        return value

    @field_validator("review_action", mode="before")
    @classmethod
    def _normalize_review_action(cls, value: Any) -> ReviewAction:
        if not isinstance(value, str):
            raise ValueError("review_action must be 'COMMENT' or 'REQUEST_CHANGES'.")
        normalized = value.strip().upper()
        if normalized not in {"COMMENT", "REQUEST_CHANGES"}:
            raise ValueError("review_action must be 'COMMENT' or 'REQUEST_CHANGES'.")
        return cast(ReviewAction, normalized)

    @field_validator("ignore_paths", mode="before")
    @classmethod
    def _validate_ignore_paths(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("ignore_paths must be a list of glob strings.")
        if len(value) > MAX_IGNORE_PATHS:
            raise ValueError(f"ignore_paths must contain at most {MAX_IGNORE_PATHS} patterns.")

        cleaned_patterns: list[str] = []
        for item in value:
            if not isinstance(item, str) or "\x00" in item:
                raise ValueError("Each ignore_paths entry must be a valid string without NUL.")
            normalized = item.strip().replace("\\", "/")
            if not normalized or len(normalized) > MAX_IGNORE_PATTERN_CHARS:
                raise ValueError(
                    f"Each ignore_paths entry must be 1..{MAX_IGNORE_PATTERN_CHARS} characters."
                )
            cleaned_patterns.append(normalized)
        return tuple(cleaned_patterns)

    @field_validator("custom_rules", mode="before")
    @classmethod
    def _validate_custom_rules(cls, value: Any) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("custom_rules must be a UTF-8 string without NUL bytes.")
        cleaned = value.strip()
        if len(cleaned) > MAX_CUSTOM_RULES_CHARS:
            raise ValueError(
                f"custom_rules must not exceed {MAX_CUSTOM_RULES_CHARS} characters."
            )
        return cleaned

    @property
    def config_hash(self) -> str:
        """Return deterministic 16-char SHA-256 hex digest for Redis blob cache keys (CA-21)."""
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def parse_repo_config_yaml(
    raw_yaml: str | bytes,
    *,
    source: str = ".codesight.yaml",
) -> RepoConfig:
    """Parse raw .codesight.yaml safely, returning atomic RepoConfig() defaults on any error."""
    if isinstance(raw_yaml, bytes):
        if len(raw_yaml) > MAX_REPO_CONFIG_BYTES:
            logger.warning("repo_config_invalid_fallback", source=source, reason="oversize")
            return RepoConfig()
        try:
            text = raw_yaml.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("repo_config_invalid_fallback", source=source, reason="invalid_utf8")
            return RepoConfig()
    else:
        if len(raw_yaml.encode("utf-8")) > MAX_REPO_CONFIG_BYTES:
            logger.warning("repo_config_invalid_fallback", source=source, reason="oversize")
            return RepoConfig()
        text = raw_yaml

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(
            "repo_config_invalid_fallback",
            source=source,
            reason="yaml_syntax_error",
            error_type=type(exc).__name__,
        )
        return RepoConfig()

    if parsed is None:
        return RepoConfig()

    if not isinstance(parsed, dict) or any(not isinstance(k, str) for k in parsed):
        logger.warning("repo_config_invalid_fallback", source=source, reason="non_mapping_root")
        return RepoConfig()

    known_fields = set(RepoConfig.model_fields.keys())
    unknown_keys = sorted(k[:64] for k in parsed if k not in known_fields)[:20]
    if unknown_keys:
        logger.warning(
            "repo_config_unknown_keys_ignored",
            source=source,
            unknown_keys=unknown_keys,
        )

    try:
        return RepoConfig.model_validate(parsed)
    except ValidationError as exc:
        invalid_fields = sorted(
            {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
        )
        logger.warning(
            "repo_config_invalid_fallback",
            source=source,
            reason="validation_error",
            invalid_fields=invalid_fields,
        )
        return RepoConfig()


def _is_rate_limited_403(headers: httpx.Headers) -> bool:
    return bool(
        headers.get("retry-after")
        or headers.get("x-ratelimit-remaining") == "0"
    )


class RepoConfigLoader:
    """Loads .codesight.yaml (.yml fallback) from GitHub Contents API on default_branch."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._auth = auth_provider
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")

    @asynccontextmanager
    async def _borrow_http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            yield client

    async def load(
        self,
        repo_full_name: str,
        *,
        default_branch: str = "main",
        ref: str | None = None,
        installation_id: int | None = None,
    ) -> RepoConfig:
        """Fetch and validate repository config (trusting default_branch unless ref is passed)."""
        slug = validate_repo_full_name(repo_full_name)
        chosen_ref = ref.strip() if ref is not None and ref.strip() else default_branch.strip()
        effective_ref = chosen_ref or "main"
        encoded_ref = quote(effective_ref, safe="")

        headers = await self._auth.get_auth_headers(
            installation_id=installation_id,
            repo_full_name=slug,
        )

        async with self._borrow_http_client() as client:
            for filename in CONFIG_FILENAMES:
                url = f"{self._base_url}/repos/{slug}/contents/{filename}?ref={encoded_ref}"
                try:
                    resp = await client.get(url, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    logger.warning(
                        "repo_config_transient_error",
                        repo=slug,
                        error_type=type(exc).__name__,
                    )
                    raise RetryableTaskError(
                        f"Transient network failure reading {filename} from '{slug}'."
                    ) from exc

                code = resp.status_code
                if code == 404:
                    continue

                if code == 429 or (code == 403 and _is_rate_limited_403(resp.headers)):
                    delay = parse_retry_after_header(resp.headers)
                    logger.warning(
                        "repo_config_rate_limited",
                        repo=slug,
                        status_code=code,
                        retry_after_seconds=delay,
                    )
                    raise RateLimitTaskError(
                        f"GitHub rate limit ({code}) while reading {filename} for '{slug}'.",
                        retry_after_seconds=delay,
                    )

                if code in {401, 403}:
                    logger.error("repo_config_auth_error", repo=slug, status_code=code)
                    raise GitHubAuthError(
                        f"GitHub Contents API returned HTTP {code} for '{slug}'.",
                        status_code=code,
                    )

                if 500 <= code < 600:
                    logger.warning("repo_config_transient_error", repo=slug, status_code=code)
                    raise RetryableTaskError(
                        f"GitHub Contents API returned HTTP {code} for '{slug}'."
                    )

                if code != 200:
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason=f"unexpected_http_{code}",
                    )
                    return RepoConfig()

                try:
                    body = resp.json()
                except ValueError:
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason="invalid_json_envelope",
                    )
                    return RepoConfig()

                if not isinstance(body, dict) or body.get("type") != "file":
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason="non_file_entry",
                    )
                    return RepoConfig()

                declared_size = body.get("size")
                if isinstance(declared_size, int) and declared_size > MAX_REPO_CONFIG_BYTES:
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason="oversize",
                    )
                    return RepoConfig()

                encoding = body.get("encoding")
                raw_b64 = body.get("content")
                if encoding != "base64" or not isinstance(raw_b64, str):
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason="unsupported_encoding",
                    )
                    return RepoConfig()

                compact_b64 = "".join(raw_b64.split())
                try:
                    decoded_bytes = base64.b64decode(compact_b64.encode("ascii"), validate=True)
                except (binascii.Error, UnicodeEncodeError, ValueError):
                    logger.warning(
                        "repo_config_invalid_fallback",
                        source=filename,
                        reason="invalid_base64",
                    )
                    return RepoConfig()

                cfg = parse_repo_config_yaml(decoded_bytes, source=filename)
                logger.info(
                    "repo_config_loaded",
                    repo=slug,
                    source=filename,
                    config_hash=cfg.config_hash,
                )
                return cfg

        logger.info("repo_config_not_found", repo=slug, ref=effective_ref)
        return RepoConfig()
