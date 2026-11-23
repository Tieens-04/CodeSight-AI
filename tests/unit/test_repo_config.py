"""Unit tests for .codesight.yaml RepoConfig schema, config_hash, and GitHub Contents loader."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from codesight.config_reader.repo_config import (
    MAX_REPO_CONFIG_BYTES,
    RepoConfig,
    RepoConfigLoader,
    parse_repo_config_yaml,
)
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import configure_logging
from codesight.vcs.github_auth import GitHubAuthError, GitHubAuthProvider


def test_parse_valid_empty_unknown_keys_and_deterministic_config_hash() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", service_name="test-repo-config", stream=stream)

    default_cfg = parse_repo_config_yaml("# comment-only empty config\n")
    assert default_cfg == RepoConfig()
    assert len(default_cfg.config_hash) == 16
    assert default_cfg.config_hash == RepoConfig().config_hash

    cfg = parse_repo_config_yaml(
        """
        min_debt_score: 75
        review_action: request_changes
        ignore_paths:
          - 'generated\\**'
          - 'docs/*.md'
        custom_rules: '  Enforce repository pattern.  '
        unknown_future_key: 123
        """
    )
    assert cfg.min_debt_score == 75
    assert cfg.review_action == "REQUEST_CHANGES"
    assert cfg.ignore_paths == ("generated/**", "docs/*.md")
    assert cfg.custom_rules == "Enforce repository pattern."
    assert cfg.config_hash != default_cfg.config_hash

    logs = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert any(
        entry["event"] == "repo_config_unknown_keys_ignored"
        and entry["unknown_keys"] == ["unknown_future_key"]
        for entry in logs
    )


@pytest.mark.parametrize(
    "bad_yaml",
    [
        "min_debt_score: [unclosed",
        "- list_root_not_dict",
        "min_debt_score: true",
        "min_debt_score: -1",
        "min_debt_score: 105",
        "review_action: APPROVE",
        "ignore_paths: 'not-a-list'",
        "ignore_paths: ['']",
        'ignore_paths: ["bad\\x00glob"]',
        "ignore_paths:\n" + "".join(f"  - 'p{i}.py'\n" for i in range(101)),
        f"custom_rules: '{'x' * 4001}'",
        b"x" * (MAX_REPO_CONFIG_BYTES + 1),
    ],
)
def test_invalid_yaml_or_schema_falls_back_atomically_without_logging_raw_content(
    bad_yaml: str | bytes,
) -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test-repo-config", stream=stream)
    sentinel = "SUPER_SECRET_INLINE_SNIPPET_DO_NOT_LOG"
    payload = (
        f"min_debt_score: [broken # {sentinel}"
        if isinstance(bad_yaml, str) and "[unclosed" in bad_yaml
        else bad_yaml
    )
    cfg = parse_repo_config_yaml(payload)
    assert cfg == RepoConfig()
    raw_logs = stream.getvalue()
    assert "repo_config_invalid_fallback" in raw_logs
    assert sentinel not in raw_logs


@pytest.mark.asyncio
async def test_loader_trusts_default_branch_falls_back_yml_and_handles_malformed_blobs() -> None:
    requested_urls: list[str] = []
    yml_raw = b"min_debt_score: 82\nreview_action: REQUEST_CHANGES\n"
    yml_b64 = base64.b64encode(yml_raw).decode("ascii")

    def _handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        path = request.url.path
        if path.endswith("/org/yml-repo/contents/.codesight.yaml"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/org/yml-repo/contents/.codesight.yml"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": len(yml_raw),
                    "encoding": "base64",
                    "content": yml_b64,
                },
            )
        if path.endswith("/org/missing/contents/.codesight.yaml") or path.endswith(
            "/org/missing/contents/.codesight.yml"
        ):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/org/dir-entry/contents/.codesight.yaml"):
            return httpx.Response(200, json={"type": "dir", "size": 0})
        if path.endswith("/org/oversize/contents/.codesight.yaml"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": MAX_REPO_CONFIG_BYTES + 10,
                    "encoding": "base64",
                    "content": "",
                },
            )
        if path.endswith("/org/bad-b64/contents/.codesight.yaml"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": 10,
                    "encoding": "base64",
                    "content": "%%%not-base64%%%",
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        loader = RepoConfigLoader(auth, http_client=http)

        # Default branch ("develop") used when ref is omitted; .codesight.yml fallback works
        loaded = await loader.load("org/yml-repo", default_branch="develop")
        assert loaded.min_debt_score == 82
        assert loaded.review_action == "REQUEST_CHANGES"
        assert all("ref=develop" in u for u in requested_urls[:2])

        # Explicit ref override for CLI debugging
        await loader.load("org/missing", default_branch="main", ref="feat/custom-ref")
        assert any("ref=feat%2Fcustom-ref" in u for u in requested_urls)

        # Missing / dir / oversize / corrupt Base64 all return RepoConfig() safely
        assert await loader.load("org/missing", default_branch="main") == RepoConfig()
        assert await loader.load("org/dir-entry") == RepoConfig()
        assert await loader.load("org/oversize") == RepoConfig()
        assert await loader.load("org/bad-b64") == RepoConfig()


@pytest.mark.asyncio
async def test_loader_propagates_429_secondary_403_5xx_timeout_and_auth_errors() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/org/rl-429/contents/.codesight.yaml"):
            return httpx.Response(429, headers={"Retry-After": "9"}, json={"message": "too many"})
        if path.endswith("/org/rl-403/contents/.codesight.yaml"):
            return httpx.Response(403, headers={"Retry-After": "14"}, json={"message": "secondary"})
        if path.endswith("/org/rl-rem0/contents/.codesight.yaml"):
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"},
                json={"message": "primary exhausted"},
            )
        if path.endswith("/org/forbidden/contents/.codesight.yaml"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path.endswith("/org/unauth/contents/.codesight.yaml"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path.endswith("/org/timeout/contents/.codesight.yaml"):
            raise httpx.ReadTimeout("github timed out", request=request)
        return httpx.Response(502, json={"message": "Bad Gateway"})

    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        loader = RepoConfigLoader(auth, http_client=http)

        with pytest.raises(RateLimitTaskError) as e429:
            await loader.load("org/rl-429")
        assert e429.value.retry_after_seconds == 9.0

        with pytest.raises(RateLimitTaskError) as e403:
            await loader.load("org/rl-403")
        assert e403.value.retry_after_seconds == 14.0

        with pytest.raises(RateLimitTaskError) as erem:
            await loader.load("org/rl-rem0")
        assert erem.value.retry_after_seconds == 60.0

        with pytest.raises(GitHubAuthError) as auth_403:
            await loader.load("org/forbidden")
        assert auth_403.value.status_code == 403

        with pytest.raises(GitHubAuthError) as auth_401:
            await loader.load("org/unauth")
        assert auth_401.value.status_code == 401

        with pytest.raises(RetryableTaskError):
            await loader.load("org/timeout")

        with pytest.raises(RetryableTaskError):
            await loader.load("org/server-down")


def test_partial_invalid_fields_preserve_valid_sibling_settings() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test-repo-config", stream=stream)
    secret_comment = "DO_NOT_LEAK_SIBLING_TEST_RAW_YAML"
    cfg = parse_repo_config_yaml(
        f"""
        # {secret_comment}
        min_debt_score: 999
        review_action: request_changes
        ignore_paths:
          - "vendor/**"
        custom_rules: "Use AsyncSession."
        """,
        source=".codesight.yaml",
    )
    assert cfg.min_debt_score == 60
    assert cfg.review_action == "REQUEST_CHANGES"
    assert cfg.ignore_paths == ("vendor/**",)
    assert cfg.custom_rules == "Use AsyncSession."

    logs = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert any(
        entry["event"] == "repo_config_invalid_fallback"
        and entry["reason"] == "validation_error"
        and entry["invalid_fields"] == ["min_debt_score"]
        for entry in logs
    )
    assert secret_comment not in stream.getvalue()


def test_example_codesight_yaml_parses_cleanly() -> None:
    example_bytes = Path(".codesight.example.yaml").read_bytes()
    parsed = parse_repo_config_yaml(example_bytes, source=".codesight.example.yaml")
    assert 0 <= parsed.min_debt_score <= 100


@pytest.mark.parametrize("bad_glob", ["[z-a].py", "/", "./"])
def test_invalid_glob_syntax_in_ignore_paths_rejected_and_salvages_siblings(
    bad_glob: str,
) -> None:
    with pytest.raises(ValueError):
        RepoConfig(ignore_paths=(bad_glob,))

    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test-repo-config-glob", stream=stream)
    cfg = parse_repo_config_yaml(
        f"min_debt_score: 75\nignore_paths:\n  - '{bad_glob}'\n",
        source=".codesight.yaml",
    )
    assert cfg.min_debt_score == 75
    assert cfg.ignore_paths == ()
    raw_logs = stream.getvalue()
    assert bad_glob not in raw_logs
    logs = [json.loads(line) for line in raw_logs.splitlines() if line.strip()]
    assert any(
        entry["event"] == "repo_config_invalid_fallback"
        and entry["invalid_fields"] == ["ignore_paths"]
        for entry in logs
    )


@pytest.mark.asyncio
async def test_yaml_alias_bomb_rejected_and_same_origin_redirect_handled() -> None:
    billion_laughs = "a: &a ['x', 'x', 'x', 'x']\nb: &b [*a, *a, *a, *a]\nignore_paths: *b\n"
    cfg = parse_repo_config_yaml(billion_laughs, source=".codesight.yaml")
    assert cfg == RepoConfig()

    ok_b64 = base64.b64encode(b"min_debt_score: 91\n").decode("ascii")

    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/org/moved/contents/.codesight.yaml":
            return httpx.Response(
                301,
                headers={"Location": "/repositories/55/contents/.codesight.yaml?ref=main"},
            )
        if request.url.path == "/repositories/55/contents/.codesight.yaml":
            return httpx.Response(
                200,
                json={"type": "file", "size": 32, "encoding": "base64", "content": ok_b64},
            )
        if request.url.path == "/repos/org/evil/contents/.codesight.yaml":
            return httpx.Response(302, headers={"Location": "https://evil.example/cfg"})
        return httpx.Response(404)

    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_redirect_handler)) as http:
        loader = RepoConfigLoader(GitHubAuthProvider(settings, http_client=http), http_client=http)
        moved_cfg = await loader.load("org/moved")
        assert moved_cfg.min_debt_score == 91
        evil_cfg = await loader.load("org/evil")
        assert evil_cfg == RepoConfig()
