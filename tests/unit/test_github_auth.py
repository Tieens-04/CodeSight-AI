"""Unit tests for GitHub App RS256 auth, 50m Redis cache, and CLI PAT fallback."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import jwt
import pytest
import redis.exceptions
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from codesight.core.config import Settings
from codesight.core.logging import configure_logging
from codesight.vcs.github_auth import (
    INSTALLATION_TOKEN_TTL_SECONDS,
    REPO_INSTALLATION_TTL_SECONDS,
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
    create_app_jwt,
    normalize_private_key,
)


def _generate_rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_private = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pem_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return pem_private, pem_public


class _FakeAsyncRedis:
    def __init__(self, initial: dict[str, str] | None = None, fail: bool = False) -> None:
        self.store: dict[str, str] = dict(initial or {})
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.fail = fail
        self.closed = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise redis.exceptions.ConnectionError("redis offline")
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        if self.fail:
            raise redis.exceptions.ConnectionError("redis offline")
        self.store[key] = value
        self.set_calls.append((key, value, ex))
        return True

    async def delete(self, key: str) -> int:
        if self.fail:
            raise redis.exceptions.ConnectionError("redis offline")
        return 1 if self.store.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        self.closed = True


def test_normalize_private_key_supports_multiline_escaped_and_base64() -> None:
    pem_private, _ = _generate_rsa_keypair()
    escaped = '"' + pem_private.replace("\n", "\\n") + '"'
    b64 = base64.b64encode(pem_private.encode("utf-8")).decode("ascii")

    assert normalize_private_key(pem_private).strip() == pem_private.strip()
    assert normalize_private_key(escaped).strip() == pem_private.strip()
    assert normalize_private_key(b64).strip() == pem_private.strip()

    with pytest.raises(GitHubAuthConfigError) as exc_info:
        normalize_private_key("not-a-valid-key-secret-value")
    assert "not-a-valid-key-secret-value" not in str(exc_info.value)

    with pytest.raises(GitHubAuthConfigError):
        normalize_private_key("-----BEGIN PRIVATE KEY-----\nbad\n-----END PRIVATE KEY-----")


def test_create_app_jwt_claims_and_rs256_signature() -> None:
    pem_private, pem_public = _generate_rsa_keypair()
    token = create_app_jwt("98765", pem_private, now=1_700_000_000)

    decoded = jwt.decode(
        token,
        pem_public,
        algorithms=["RS256"],
        options={"verify_exp": False},
    )
    assert decoded["iss"] == "98765"
    assert decoded["iat"] == 1_699_999_940
    assert decoded["exp"] == 1_700_000_540

    with pytest.raises(GitHubAuthConfigError):
        create_app_jwt("   ", pem_private)


@pytest.mark.asyncio
async def test_installation_token_redis_hit_and_miss_with_50m_ttl() -> None:
    pem_private, pem_public = _generate_rsa_keypair()
    settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private.replace("\n", "\\n")),
    )

    # 1. Cache hit -> 0 HTTP requests
    fake_redis_hit = _FakeAsyncRedis({"gh_inst_token:42": "ghs_cached_token_42"})
    http_calls: list[str] = []

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        http_calls.append(str(request.url))
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_fail_if_called)) as client:
        provider_hit = GitHubAuthProvider(
            settings,
            redis_client=fake_redis_hit,  # type: ignore[arg-type]
            http_client=client,
        )
        token_hit = await provider_hit.get_token(installation_id=42)

    assert token_hit.get_secret_value() == "ghs_cached_token_42"
    assert http_calls == []

    # 2. Cache miss -> exchanges RS256 JWT and caches for 3000s (50m)
    fake_redis_miss = _FakeAsyncRedis()
    captured_auth_headers: list[str] = []

    def _exchange_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/app/installations/42/access_tokens":
            auth = request.headers["Authorization"]
            assert auth.startswith("Bearer ")
            captured_auth_headers.append(auth.removeprefix("Bearer "))
            return httpx.Response(201, json={"token": "ghs_fresh_inst_42"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_exchange_handler)) as client:
        provider_miss = GitHubAuthProvider(
            settings,
            redis_client=fake_redis_miss,  # type: ignore[arg-type]
            http_client=client,
        )
        token_miss = await provider_miss.get_token(installation_id=42)

    assert token_miss.get_secret_value() == "ghs_fresh_inst_42"
    assert "ghs_fresh_inst_42" not in repr(token_miss)
    assert fake_redis_miss.set_calls == [
        ("gh_inst_token:42", "ghs_fresh_inst_42", INSTALLATION_TOKEN_TTL_SECONDS)
    ]
    assert INSTALLATION_TOKEN_TTL_SECONDS == 3000
    decoded_jwt = jwt.decode(
        captured_auth_headers[0],
        pem_public,
        algorithms=["RS256"],
        options={"verify_exp": False},
    )
    assert decoded_jwt["iss"] == "12345"


@pytest.mark.asyncio
async def test_installation_token_fails_open_when_redis_unavailable() -> None:
    pem_private, _ = _generate_rsa_keypair()
    buf = io.StringIO()
    configure_logging(level="WARNING", service_name="codesight-test", stream=buf)

    settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private),
    )
    failing_redis = _FakeAsyncRedis(fail=True)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(201, json={"token": "ghs_fail_open_token"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAuthProvider(
            settings,
            redis_client=failing_redis,  # type: ignore[arg-type]
            http_client=client,
        )
        token = await provider.get_token(installation_id=99)

    assert token.get_secret_value() == "ghs_fail_open_token"
    raw_logs = buf.getvalue()
    assert "ghs_fail_open_token" not in raw_logs
    events = [json.loads(line)["event"] for line in raw_logs.strip().splitlines()]
    assert "github_token_cache_unavailable" in events


@pytest.mark.asyncio
async def test_repo_lookup_and_cli_fallback_paths() -> None:
    pem_private, _ = _generate_rsa_keypair()
    settings_with_fallback = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private),
        github_token=SecretStr("ghp_cli_fallback"),
    )
    fake_redis = _FakeAsyncRedis()

    def _repo_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/repos/org/installed/installation":
            return httpx.Response(200, json={"id": 777})
        if request.method == "POST" and request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(201, json={"token": "ghs_repo_777"})
        if request.method == "GET" and request.url.path == "/repos/ext/uninstalled/installation":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_repo_handler)) as client:
        provider = GitHubAuthProvider(
            settings_with_fallback,
            redis_client=fake_redis,  # type: ignore[arg-type]
            http_client=client,
        )
        installed_token = await provider.get_token(repo_full_name="org/installed")
        fallback_token = await provider.get_token(repo_full_name="ext/uninstalled")
        with pytest.raises(GitHubAuthConfigError):
            await provider.get_token(repo_full_name="../evil/path")

    assert installed_token.get_secret_value() == "ghs_repo_777"
    assert fallback_token.get_secret_value() == "ghp_cli_fallback"

    # 404 without GITHUB_TOKEN raises GitHubAuthError
    settings_no_pat = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_repo_handler)) as client:
        provider_strict = GitHubAuthProvider(
            settings_no_pat,
            redis_client=fake_redis,  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError):
            await provider_strict.get_token(repo_full_name="ext/uninstalled")

    # Pure CLI mode with only GITHUB_TOKEN
    cli_settings = Settings(_env_file=None, github_token=SecretStr("ghp_only_cli"))
    cli_provider = GitHubAuthProvider(cli_settings)
    headers = await cli_provider.get_auth_headers()
    assert headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": "Bearer ghp_only_cli",
    }

    # Unconfigured fails cleanly with actionable message
    empty_provider = GitHubAuthProvider(Settings(_env_file=None))
    with pytest.raises(GitHubAuthError) as exc_info:
        await empty_provider.get_token()
    assert "GITHUB_TOKEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_creates_and_closes_transient_clients_when_uninjected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem_private, _ = _generate_rsa_keypair()
    settings = Settings(
        _env_file=None,
        github_app_id="555",
        github_private_key=SecretStr(pem_private),
    )
    fake_redis = _FakeAsyncRedis()
    closed_flags: dict[str, Any] = {"http_closed": False}

    def _mock_redis_from_url(*_: Any, **__: Any) -> _FakeAsyncRedis:
        return fake_redis

    class _MockAsyncClient:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> _MockAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            closed_flags["http_closed"] = True

        async def post(self, url: str, **__: Any) -> httpx.Response:
            assert url.endswith("/app/installations/88/access_tokens")
            return httpx.Response(
                201,
                json={"token": "ghs_transient_88"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("codesight.vcs.github_auth.redis_async.from_url", _mock_redis_from_url)
    monkeypatch.setattr("codesight.vcs.github_auth.httpx.AsyncClient", _MockAsyncClient)

    provider = GitHubAuthProvider(settings)
    tok = await provider.get_token(installation_id=88)
    assert tok.get_secret_value() == "ghs_transient_88"
    assert fake_redis.closed is True
    assert closed_flags["http_closed"] is True


@pytest.mark.asyncio
async def test_redis_bytes_cache_and_http_error_and_bool_id_guards() -> None:
    pem_private, _ = _generate_rsa_keypair()
    settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private),
    )

    class _BytesRedis:
        async def get(self, key: str) -> bytes:
            assert key == "gh_inst_token:42"
            return b"ghs_raw_bytes_42"

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            return True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        provider_bytes = GitHubAuthProvider(
            settings,
            redis_client=_BytesRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        tok = await provider_bytes.get_token(installation_id=42)
    assert tok.get_secret_value() == "ghs_raw_bytes_42"

    # Bool ID in installation lookup response must be rejected cleanly
    def _bool_id_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_bool_id_handler)) as client:
        provider_bool = GitHubAuthProvider(
            settings,
            redis_client=_FakeAsyncRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError):
            await provider_bool.get_token(repo_full_name="org/bool-id")

    # HTTP 500 & RequestError wrap into GitHubAuthError cleanly
    def _http_500_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"message": "Bad Gateway"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_http_500_handler)) as client:
        provider_502 = GitHubAuthProvider(
            settings,
            redis_client=_FakeAsyncRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError):
            await provider_502.get_token(installation_id=42)

    def _conn_err_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_conn_err_handler)) as client:
        provider_net = GitHubAuthProvider(
            settings,
            redis_client=_FakeAsyncRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError):
            await provider_net.get_token(installation_id=42)
        with pytest.raises(GitHubAuthError):
            await provider_net.get_token(repo_full_name="org/net-fail")

    # Installation lookup HTTP 500 preserves status_code=500
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"message": "err"}))
    ) as client:
        provider_500 = GitHubAuthProvider(
            settings,
            redis_client=_FakeAsyncRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError) as lookup_err:
            await provider_500.get_token(repo_full_name="org/server-err")
    assert lookup_err.value.status_code == 500

    # Token exchange 201 missing 'token' key raises GitHubAuthError
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={}))
    ) as client:
        provider_empty_tok = GitHubAuthProvider(
            settings,
            redis_client=_FakeAsyncRedis(),  # type: ignore[arg-type]
            http_client=client,
        )
        with pytest.raises(GitHubAuthError):
            await provider_empty_tok.get_token(installation_id=42)


@pytest.mark.asyncio
async def test_repo_installation_id_cached_and_invalidated_on_404() -> None:
    pem_private, _ = _generate_rsa_keypair()
    settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(pem_private),
    )
    fake_redis = _FakeAsyncRedis()
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/org/cached/installation":
            return httpx.Response(200, json={"id": 888})
        if request.url.path == "/app/installations/888/access_tokens":
            return httpx.Response(201, json={"token": "ghs_cached_repo_888"})
        if request.url.path == "/app/installations/502/access_tokens":
            return httpx.Response(502, json={"message": "Bad Gateway"})
        if request.url.path == "/app/installations/111/access_tokens":
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path == "/repos/org/reinstalled/installation":
            return httpx.Response(200, json={"id": 222})
        if request.url.path == "/app/installations/222/access_tokens":
            return httpx.Response(201, json={"token": "ghs_fresh_222"})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAuthProvider(
            settings,
            redis_client=fake_redis,  # type: ignore[arg-type]
            http_client=client,
        )
        # First call resolves installation_id=888 and caches both repo->888 & token
        tok1 = await provider.get_token(repo_full_name="org/cached")
        assert tok1.get_secret_value() == "ghs_cached_repo_888"
        assert fake_redis.store["gh_repo_inst:org/cached"] == "888"
        assert (
            "gh_repo_inst:org/cached",
            "888",
            REPO_INSTALLATION_TTL_SECONDS,
        ) in fake_redis.set_calls

        # Drop token cache only: second call uses cached ID without GET /installation
        fake_redis.store.pop("gh_inst_token:888", None)
        calls.clear()
        tok2 = await provider.get_token(repo_full_name="org/cached")
        assert tok2.get_secret_value() == "ghs_cached_repo_888"
        assert calls == ["POST /app/installations/888/access_tokens"]

        # Stale cached repo installation (111 -> 404) is invalidated and refreshed to 222
        fake_redis.store["gh_repo_inst:org/reinstalled"] = "111"
        calls.clear()
        tok3 = await provider.get_token(repo_full_name="org/reinstalled")
        assert tok3.get_secret_value() == "ghs_fresh_222"
        assert fake_redis.store["gh_repo_inst:org/reinstalled"] == "222"
        assert calls == [
            "POST /app/installations/111/access_tokens",
            "GET /repos/org/reinstalled/installation",
            "POST /app/installations/222/access_tokens",
        ]

        # Non-404 HTTP 502 from cached repo installation preserves cache and propagates status_code
        fake_redis.store["gh_repo_inst:org/cached"] = "502"
        calls.clear()
        with pytest.raises(GitHubAuthError) as cached_502_err:
            await provider.get_token(repo_full_name="org/cached")
        assert cached_502_err.value.status_code == 502
        assert fake_redis.store["gh_repo_inst:org/cached"] == "502"
        assert calls == ["POST /app/installations/502/access_tokens"]


@pytest.mark.asyncio
async def test_auth_single_flight_wrapped_b64_invalid_json_and_401_invalidation() -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    pem_private, _ = _generate_rsa_keypair()
    b64_raw = base64.b64encode(pem_private.encode("utf-8")).decode("ascii")
    # Simulate 76-char line-wrapped Base64 output from `base64 key.pem`
    wrapped_b64 = "\n".join(b64_raw[i : i + 76] for i in range(0, len(b64_raw), 76))
    assert normalize_private_key(wrapped_b64).strip() == pem_private.strip()

    settings = Settings(
        _env_file=None,
        github_app_id="12345",
        github_private_key=SecretStr(wrapped_b64),
    )
    fake_redis = _FakeAsyncRedis()
    exchange_calls = 0
    mode = "single_flight"
    short_expiry = (datetime.now(UTC) + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchange_calls
        if mode == "bad_json":
            return httpx.Response(201, content=b"<html>proxy error</html>")
        if request.url.path == "/app/installations/77/access_tokens":
            exchange_calls += 1
            await asyncio.sleep(0.02)
            return httpx.Response(
                201,
                json={"token": f"ghs_sf_{exchange_calls}", "expires_at": short_expiry},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAuthProvider(
            settings,
            redis_client=fake_redis,  # type: ignore[arg-type]
            http_client=client,
        )
        tokens = await asyncio.gather(*(provider.get_token(installation_id=77) for _ in range(5)))
        assert exchange_calls == 1
        assert all(t.get_secret_value() == "ghs_sf_1" for t in tokens)
        # expires_at (20m) minus 5m buffer clamps TTL well below 3000s
        _, _, cached_ttl = fake_redis.set_calls[-1]
        assert cached_ttl is not None and 600 <= cached_ttl <= 1000

        # Invalidating token evicts cached entry so next call exchanges a fresh token
        await provider.invalidate_installation_token(77)
        assert "gh_inst_token:77" not in fake_redis.store
        refreshed = await provider.get_token(installation_id=77)
        assert refreshed.get_secret_value() == "ghs_sf_2"
        assert exchange_calls == 2

        # Non-JSON 201 body wraps cleanly into GitHubAuthError
        await provider.invalidate_installation_token(77)
        mode = "bad_json"
        with pytest.raises(GitHubAuthError, match="invalid JSON"):
            await provider.get_token(installation_id=77)
