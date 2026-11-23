"""GitHub App RS256 authentication, 50-minute Redis token cache, and CLI PAT fallback."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final

import httpx
import jwt
import redis.asyncio as redis_async
import redis.exceptions
import structlog
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import SecretStr

from codesight.core.config import Settings

INSTALLATION_TOKEN_TTL_SECONDS: Final[int] = 3000  # 50 minutes (GitHub tokens expire at 60m)
REPO_INSTALLATION_TTL_SECONDS: Final[int] = 3600  # 1 hour cache for repo -> installation_id
GITHUB_API_VERSION: Final[str] = "2022-11-28"
GITHUB_ACCEPT_HEADER: Final[str] = "application/vnd.github+json"
_REPO_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")

logger = structlog.get_logger("codesight.vcs.auth")


class GitHubAuthError(RuntimeError):
    """Raised when GitHub authentication fails or credentials are missing."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAuthConfigError(GitHubAuthError):
    """Raised when GitHub App private key or repository identifier configuration is invalid."""


def normalize_private_key(raw_key: str) -> str:
    """Normalize multiline, escaped-newline, or Base64-encoded PEM private keys."""
    cleaned = raw_key.strip()
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (
        cleaned.startswith("'") and cleaned.endswith("'")
    ):
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.replace("\\n", "\n")

    if "-----BEGIN " not in cleaned:
        compact_b64 = "".join(cleaned.split())
        try:
            decoded = base64.b64decode(compact_b64.encode("ascii"), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise GitHubAuthConfigError("Invalid GitHub App private key format.") from exc
        cleaned = decoded.strip().replace("\\n", "\n")

    try:
        load_pem_private_key(cleaned.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise GitHubAuthConfigError("Invalid GitHub App PEM private key.") from exc

    return cleaned


def create_app_jwt(
    app_id: str,
    private_key_pem: str,
    *,
    now: int | None = None,
) -> str:
    """Sign a short-lived RS256 GitHub App JWT (iat=now-60s, exp=now+540s)."""
    cleaned_app_id = app_id.strip()
    if not cleaned_app_id:
        raise GitHubAuthConfigError("GITHUB_APP_ID must not be empty.")
    ts = int(time.time()) if now is None else now
    payload: dict[str, str | int] = {
        "iat": ts - 60,
        "exp": ts + 540,
        "iss": cleaned_app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def validate_repo_full_name(repo_full_name: str) -> str:
    """Validate GitHub 'owner/repo' slug format and 255-character DB column bound."""
    cleaned = repo_full_name.strip()
    parts = cleaned.split("/")
    if len(parts) != 2 or len(cleaned) > 255:
        raise GitHubAuthConfigError("Invalid repository slug; expected 'owner/repo'.")
    owner, repo = parts
    if (
        owner in {".", ".."}
        or repo in {".", ".."}
        or not _REPO_SEGMENT_RE.fullmatch(owner)
        or not _REPO_SEGMENT_RE.fullmatch(repo)
    ):
        raise GitHubAuthConfigError("Invalid repository slug; expected 'owner/repo'.")
    return f"{owner}/{repo}"


class GitHubAuthProvider:
    """Provides GitHub API bearer tokens via GitHub App installation exchange or PAT fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: redis_async.Redis | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._settings = settings
        self._redis_client = redis_client
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._normalized_pem: str | None = None
        self._installation_locks: dict[int, asyncio.Lock] = {}

    def _has_app_credentials(self) -> bool:
        return self._settings.has_github_app_credentials

    def _pat_token(self) -> SecretStr | None:
        if not self._settings.has_github_token:
            return None
        return self._settings.github_token

    @asynccontextmanager
    async def _borrow_http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            yield client

    @asynccontextmanager
    async def _borrow_redis_client(self) -> AsyncIterator[redis_async.Redis]:
        if self._redis_client is not None:
            yield self._redis_client
            return
        client = redis_async.from_url(
            self._settings.redis_url,
            decode_responses=True,
            socket_timeout=self._settings.health_timeout_seconds,
            socket_connect_timeout=self._settings.health_timeout_seconds,
        )
        try:
            yield client
        finally:
            try:
                await client.aclose()
            except (redis.exceptions.RedisError, OSError):
                pass

    async def _read_cache(
        self,
        cache_key: str,
        *,
        redis_client: redis_async.Redis | None = None,
    ) -> str | None:
        if redis_client is not None:
            try:
                raw = await redis_client.get(cache_key)
                if isinstance(raw, (bytes, bytearray)):
                    return raw.decode("utf-8")
                return str(raw) if raw else None
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning("github_token_cache_unavailable", error_type=type(exc).__name__)
                return None
        async with self._borrow_redis_client() as client:
            return await self._read_cache(cache_key, redis_client=client)

    async def _write_cache(
        self,
        cache_key: str,
        value: str,
        *,
        ttl_seconds: int,
        redis_client: redis_async.Redis | None = None,
    ) -> None:
        if redis_client is not None:
            try:
                await redis_client.set(cache_key, value, ex=ttl_seconds)
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning("github_token_cache_unavailable", error_type=type(exc).__name__)
            return
        async with self._borrow_redis_client() as client:
            await self._write_cache(
                cache_key,
                value,
                ttl_seconds=ttl_seconds,
                redis_client=client,
            )

    async def _delete_cache(
        self,
        cache_key: str,
        *,
        redis_client: redis_async.Redis | None = None,
    ) -> None:
        if redis_client is not None:
            try:
                await redis_client.delete(cache_key)
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning("github_token_cache_unavailable", error_type=type(exc).__name__)
            return
        async with self._borrow_redis_client() as client:
            await self._delete_cache(cache_key, redis_client=client)

    async def invalidate_installation_token(self, installation_id: int) -> None:
        """Evict a cached installation access token (e.g., after an unexpected HTTP 401)."""
        await self._delete_cache(f"gh_inst_token:{installation_id}")

    async def _read_cached_token(
        self,
        installation_id: int,
        *,
        redis_client: redis_async.Redis | None = None,
    ) -> str | None:
        return await self._read_cache(
            f"gh_inst_token:{installation_id}",
            redis_client=redis_client,
        )

    async def _write_cached_token(
        self,
        installation_id: int,
        token: str,
        *,
        ttl_seconds: int = INSTALLATION_TOKEN_TTL_SECONDS,
        redis_client: redis_async.Redis | None = None,
    ) -> None:
        await self._write_cache(
            f"gh_inst_token:{installation_id}",
            token,
            ttl_seconds=ttl_seconds,
            redis_client=redis_client,
        )

    def _mint_jwt(self) -> str:
        app_id = self._settings.github_app_id
        private_key = self._settings.github_private_key
        if app_id is None or private_key is None:
            raise GitHubAuthConfigError("GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be configured.")
        if self._normalized_pem is None:
            self._normalized_pem = normalize_private_key(private_key.get_secret_value())
        return create_app_jwt(app_id, self._normalized_pem)

    @staticmethod
    def _app_headers(app_jwt: str) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT_HEADER,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {app_jwt}",
        }

    @staticmethod
    def _compute_token_ttl(raw_expires_at: object) -> int:
        if not isinstance(raw_expires_at, str) or not raw_expires_at.strip():
            return INSTALLATION_TOKEN_TTL_SECONDS
        cleaned = raw_expires_at.strip()
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            return INSTALLATION_TOKEN_TTL_SECONDS
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        remaining = int(dt.timestamp() - time.time()) - 300
        if remaining <= 0:
            return 60
        return min(INSTALLATION_TOKEN_TTL_SECONDS, max(60, remaining))

    async def _exchange_installation_token(
        self,
        client: httpx.AsyncClient,
        app_jwt: str,
        installation_id: int,
    ) -> tuple[str, int]:
        url = f"{self._base_url}/app/installations/{installation_id}/access_tokens"
        try:
            resp = await client.post(url, headers=self._app_headers(app_jwt))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise GitHubAuthError(
                f"GitHub App token exchange failed with HTTP {code}.",
                status_code=code,
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubAuthError(
                f"GitHub App token exchange request failed: {type(exc).__name__}."
            ) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise GitHubAuthError(
                "GitHub App token exchange returned invalid JSON.",
                status_code=resp.status_code,
            ) from exc
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAuthError("GitHub App token exchange response missing 'token'.")
        raw_expires_at = data.get("expires_at") if isinstance(data, dict) else None
        ttl_seconds = self._compute_token_ttl(raw_expires_at)
        return token, ttl_seconds

    async def _lookup_repo_installation_id(
        self,
        client: httpx.AsyncClient,
        app_jwt: str,
        repo_slug: str,
    ) -> int | None:
        url = f"{self._base_url}/repos/{repo_slug}/installation"
        try:
            resp = await client.get(url, headers=self._app_headers(app_jwt))
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise GitHubAuthError(
                f"GitHub installation lookup failed with HTTP {code}.",
                status_code=code,
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubAuthError(
                f"GitHub installation lookup request failed: {type(exc).__name__}."
            ) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise GitHubAuthError(
                "GitHub installation lookup returned invalid JSON.",
                status_code=resp.status_code,
            ) from exc
        inst_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(inst_id, int) or isinstance(inst_id, bool) or inst_id < 1:
            raise GitHubAuthError("GitHub installation lookup response missing integer 'id'.")
        return inst_id

    async def _get_or_exchange_for_installation(
        self,
        installation_id: int,
        *,
        app_jwt: str | None = None,
        redis_client: redis_async.Redis | None = None,
    ) -> SecretStr:
        cached = await self._read_cached_token(installation_id, redis_client=redis_client)
        if cached is not None:
            return SecretStr(cached)

        lock = self._installation_locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            cached_again = await self._read_cached_token(installation_id, redis_client=redis_client)
            if cached_again is not None:
                return SecretStr(cached_again)

            active_jwt = app_jwt if app_jwt is not None else self._mint_jwt()
            async with self._borrow_http_client() as client:
                token, ttl_seconds = await self._exchange_installation_token(
                    client, active_jwt, installation_id
                )

            await self._write_cached_token(
                installation_id,
                token,
                ttl_seconds=ttl_seconds,
                redis_client=redis_client,
            )
            return SecretStr(token)

    async def get_token(
        self,
        *,
        installation_id: int | None = None,
        repo_full_name: str | None = None,
    ) -> SecretStr:
        """Resolve a GitHub bearer token (App Installation token first, PAT fallback)."""
        validated_repo = (
            validate_repo_full_name(repo_full_name) if repo_full_name is not None else None
        )

        if self._has_app_credentials():
            if installation_id is not None:
                async with self._borrow_redis_client() as redis_client:
                    return await self._get_or_exchange_for_installation(
                        installation_id,
                        redis_client=redis_client,
                    )

            if validated_repo is not None:
                repo_cache_key = f"gh_repo_inst:{validated_repo}"
                async with self._borrow_redis_client() as redis_client:
                    cached_inst = await self._read_cache(repo_cache_key, redis_client=redis_client)
                    if cached_inst and cached_inst.isdigit() and int(cached_inst) >= 1:
                        try:
                            return await self._get_or_exchange_for_installation(
                                int(cached_inst),
                                redis_client=redis_client,
                            )
                        except GitHubAuthError as exc:
                            if exc.status_code != 404:
                                raise
                            await self._delete_cache(repo_cache_key, redis_client=redis_client)

                    app_jwt = self._mint_jwt()
                    async with self._borrow_http_client() as client:
                        found_id = await self._lookup_repo_installation_id(
                            client, app_jwt, validated_repo
                        )
                    if found_id is not None:
                        await self._write_cache(
                            repo_cache_key,
                            str(found_id),
                            ttl_seconds=REPO_INSTALLATION_TTL_SECONDS,
                            redis_client=redis_client,
                        )
                        return await self._get_or_exchange_for_installation(
                            found_id,
                            app_jwt=app_jwt,
                            redis_client=redis_client,
                        )
                    logger.info(
                        "github_app_not_installed_falling_back",
                        repo_full_name=validated_repo,
                    )

        pat = self._pat_token()
        if pat is not None:
            return pat

        raise GitHubAuthError(
            "GitHub authentication unavailable: configure GITHUB_APP_ID + GITHUB_PRIVATE_KEY "
            "or fallback GITHUB_TOKEN."
        )

    async def get_auth_headers(
        self,
        *,
        installation_id: int | None = None,
        repo_full_name: str | None = None,
    ) -> dict[str, str]:
        """Build standard GitHub REST API headers with resolved bearer token."""
        token = await self.get_token(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
        )
        return {
            "Accept": GITHUB_ACCEPT_HEADER,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {token.get_secret_value()}",
        }
