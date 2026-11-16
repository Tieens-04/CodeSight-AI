"""GitHub App RS256 authentication, 50-minute Redis token cache, and CLI PAT fallback."""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
GITHUB_API_VERSION: Final[str] = "2022-11-28"
GITHUB_ACCEPT_HEADER: Final[str] = "application/vnd.github+json"
_REPO_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")

logger = structlog.get_logger("codesight.vcs.auth")


class GitHubAuthError(RuntimeError):
    """Raised when GitHub authentication fails or credentials are missing."""


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
        try:
            decoded = base64.b64decode(cleaned.encode("ascii"), validate=True).decode("utf-8")
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


def _validate_repo_full_name(repo_full_name: str) -> str:
    parts = repo_full_name.split("/")
    if len(parts) != 2:
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

    def _has_app_credentials(self) -> bool:
        app_id = self._settings.github_app_id
        priv = self._settings.github_private_key
        return bool(
            app_id is not None
            and app_id.strip()
            and priv is not None
            and priv.get_secret_value().strip()
        )

    def _pat_token(self) -> SecretStr | None:
        token = self._settings.github_token
        if token is None or not token.get_secret_value().strip():
            return None
        return token

    @asynccontextmanager
    async def _borrow_http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            yield client

    async def _read_cached_token(self, installation_id: int) -> str | None:
        cache_key = f"gh_inst_token:{installation_id}"
        owns_client = self._redis_client is None
        client = (
            self._redis_client
            if self._redis_client is not None
            else redis_async.from_url(
                self._settings.redis_url,
                decode_responses=True,
                socket_timeout=self._settings.health_timeout_seconds,
            )
        )
        try:
            raw = await client.get(cache_key)
            return str(raw) if raw else None
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("github_token_cache_unavailable", error_type=type(exc).__name__)
            return None
        finally:
            if owns_client:
                try:
                    await client.aclose()
                except (redis.exceptions.RedisError, OSError):
                    pass

    async def _write_cached_token(self, installation_id: int, token: str) -> None:
        cache_key = f"gh_inst_token:{installation_id}"
        owns_client = self._redis_client is None
        client = (
            self._redis_client
            if self._redis_client is not None
            else redis_async.from_url(
                self._settings.redis_url,
                decode_responses=True,
                socket_timeout=self._settings.health_timeout_seconds,
            )
        )
        try:
            await client.set(cache_key, token, ex=INSTALLATION_TOKEN_TTL_SECONDS)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning("github_token_cache_unavailable", error_type=type(exc).__name__)
        finally:
            if owns_client:
                try:
                    await client.aclose()
                except (redis.exceptions.RedisError, OSError):
                    pass

    def _mint_jwt(self) -> str:
        assert self._settings.github_app_id is not None
        assert self._settings.github_private_key is not None
        pem = normalize_private_key(self._settings.github_private_key.get_secret_value())
        return create_app_jwt(self._settings.github_app_id, pem)

    @staticmethod
    def _app_headers(app_jwt: str) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT_HEADER,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Authorization": f"Bearer {app_jwt}",
        }

    async def _exchange_installation_token(
        self,
        client: httpx.AsyncClient,
        app_jwt: str,
        installation_id: int,
    ) -> str:
        url = f"{self._base_url}/app/installations/{installation_id}/access_tokens"
        try:
            resp = await client.post(url, headers=self._app_headers(app_jwt))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubAuthError(
                f"GitHub App token exchange failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubAuthError(
                f"GitHub App token exchange request failed: {type(exc).__name__}."
            ) from exc

        data = resp.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAuthError("GitHub App token exchange response missing 'token'.")
        return token

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
            raise GitHubAuthError(
                f"GitHub installation lookup failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubAuthError(
                f"GitHub installation lookup request failed: {type(exc).__name__}."
            ) from exc

        data = resp.json()
        inst_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(inst_id, int):
            raise GitHubAuthError("GitHub installation lookup response missing integer 'id'.")
        return inst_id

    async def _get_or_exchange_for_installation(self, installation_id: int) -> SecretStr:
        cached = await self._read_cached_token(installation_id)
        if cached is not None:
            return SecretStr(cached)

        app_jwt = self._mint_jwt()
        async with self._borrow_http_client() as client:
            token = await self._exchange_installation_token(client, app_jwt, installation_id)

        await self._write_cached_token(installation_id, token)
        return SecretStr(token)

    async def get_token(
        self,
        *,
        installation_id: int | None = None,
        repo_full_name: str | None = None,
    ) -> SecretStr:
        """Resolve a GitHub bearer token (App Installation token first, PAT fallback)."""
        validated_repo = (
            _validate_repo_full_name(repo_full_name) if repo_full_name is not None else None
        )

        if self._has_app_credentials():
            if installation_id is not None:
                return await self._get_or_exchange_for_installation(installation_id)

            if validated_repo is not None:
                app_jwt = self._mint_jwt()
                async with self._borrow_http_client() as client:
                    found_id = await self._lookup_repo_installation_id(
                        client, app_jwt, validated_repo
                    )
                if found_id is not None:
                    return await self._get_or_exchange_for_installation(found_id)
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
