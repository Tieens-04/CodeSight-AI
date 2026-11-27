"""Unit tests for RedisBlobSHACache (`file_review:{repo_id}:{blob_sha}:{config_hash}`, CA-21)."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import redis.exceptions
from pydantic import SecretStr

from codesight.agents.llm_factory import LLMFactory, StructuredInvocationResult
from codesight.agents.schemas import (
    CodeSuggestion,
    LLMAuditorDraftOutput,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
    VerifiedFinding,
)
from codesight.config_reader.repo_config import RepoConfig
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import HybridContextRetriever
from codesight.vcs.blob_fetcher import GitHubBlobFetcher
from codesight.vcs.github_auth import GitHubAuthProvider
from codesight.vcs.github_pr_client import GitHubPullRequestClient
from codesight.vcs.github_publisher import GitHubReviewPublisher
from codesight.worker.ai_review_pipeline import execute_ai_pr_review
from codesight.worker.sha_cache import (
    DEFAULT_SHA_CACHE_TTL_SECONDS,
    CachedFileReview,
    RedisBlobSHACache,
    build_sha_cache_key,
    compute_combined_tokens_saved_pct,
    compute_patch_fingerprint,
)


class _FakePipeline:
    def __init__(self, store: dict[str, tuple[str, int]]) -> None:
        self._store = store
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._ops.append((key, value, ttl))

    async def execute(self) -> list[bool]:
        for key, value, ttl in self._ops:
            self._store[key] = (value, ttl)
        return [True] * len(self._ops)


class _FakeAsyncRedis:
    def __init__(self, *, fail_mget: bool = False, fail_set: bool = False) -> None:
        self.store: dict[str, tuple[str, int]] = {}
        self.mget_calls = 0
        self.fail_mget = fail_mget
        self.fail_set = fail_set

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.mget_calls += 1
        if self.fail_mget:
            raise redis.exceptions.TimeoutError("Redis MGET timed out")
        return [self.store[k][0] if k in self.store else None for k in keys]

    def pipeline(self, *, transaction: bool = False) -> _FakePipeline:
        _ = transaction
        if self.fail_set:
            raise redis.exceptions.ConnectionError("Redis unavailable")
        return _FakePipeline(self.store)


class _CountingFakeLLMFactory(LLMFactory):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings, structured_invoker=self._fake_invoke)
        self.calls: list[tuple[str, str]] = []
        self.prompt_chars: int = 0

    async def _fake_invoke(
        self,
        role: Any,
        model_name: str,
        schema: type[Any],
        messages: list[Any],
        config: Any,
    ) -> StructuredInvocationResult[Any]:
        _ = config
        user_text = str(messages[-1].content)
        self.calls.append((role, user_text))
        self.prompt_chars += len(user_text)

        if role == "security_auditor":
            findings: list[LLMFindingDraft] = []
            if "src/file_00.py" in user_text:
                findings.append(
                    LLMFindingDraft(
                        file_path="src/file_00.py",
                        start_line=2,
                        end_line=2,
                        severity="CRITICAL",
                        category="sql_injection",
                        title="Unsafely interpolated SQL query",
                        evidence="execute_query uses f-string user_input on L2",
                        confidence=0.92,
                    )
                )
            parsed: Any = LLMAuditorDraftOutput(findings=findings)
        elif role == "logic_performance_auditor":
            parsed = LLMAuditorDraftOutput(findings=[])
        else:
            cand_ids = re.findall(r"b\d{3}-sec-\d{3}", user_text)
            target_id = cand_ids[0] if cand_ids else "b000-sec-000"
            parsed = LLMCriticDraftOutput(
                verdicts=[
                    LLMCriticVerdictDraft(
                        candidate_id=target_id,
                        decision="APPROVE",
                        rationale="Direct SQL interpolation verified in src/file_00.py.",
                        confidence=0.92,
                    )
                ]
            )
        return StructuredInvocationResult(
            parsed=parsed,
            model_name=model_name,
            input_tokens=max(1, len(user_text) // 4),
            output_tokens=64,
            total_tokens=max(1, len(user_text) // 4) + 64,
            estimated_cost_usd=0.001,
            latency_ms=12.0,
            attempts=1,
        )


def _build_15_file_pr(
    *,
    file0_sha: str,
    file0_line: str,
) -> tuple[Any, dict[str, str]]:
    entries: list[PatchFileEntry] = []
    sources: dict[str, str] = {}
    for idx in range(15):
        path = f"src/file_{idx:02d}.py"
        if idx == 0:
            code_line = file0_line
            sha = file0_sha
        else:
            padding = " + ".join(f"x_{j}" for j in range(12))
            code_line = f"    val_{idx} = ({padding})"
            sha = f"{idx:040x}"
        source = f"def fn_{idx}(x_0=1):\n{code_line}\n    return 1\n"
        patch = f"@@ -1,2 +1,3 @@\n def fn_{idx}(x_0=1):\n+{code_line}\n     return 1\n"
        entries.append(
            PatchFileEntry(
                filename=path,
                status="modified",
                blob_sha=sha,
                patch=patch,
            )
        )
        sources[path] = source
    return SmartDiffParser().parse_patch_entries(entries), sources


def test_sha_cache_key_format_and_smart_diff_parser_index_extraction() -> None:
    cfg = RepoConfig(min_debt_score=70)
    key = build_sha_cache_key("repo-42", "AB" * 20, cfg.config_hash)
    assert key == f"file_review:repo-42:{'ab' * 20}:{cfg.config_hash}"

    with pytest.raises(ValueError):
        build_sha_cache_key("repo:bad", "a" * 40, cfg.config_hash)

    raw_diff = "\n".join(
        [
            "diff --git a/src/app.py b/src/app.py",
            f"index {'1' * 40}..{'2' * 40} 100644",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -1 +1,2 @@",
            " def ok():",
            "+    return 1",
            "diff --git a/src/deleted.py b/src/deleted.py",
            "deleted file mode 100644",
            f"index {'3' * 40}..{'0' * 40}",
            "--- a/src/deleted.py",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-x = 1",
        ]
    )
    parsed = SmartDiffParser().parse_diff(raw_diff)
    app_file = parsed.get_file("src/app.py")
    del_file = parsed.get_file("src/deleted.py")
    assert app_file is not None and app_file.blob_sha == "2" * 40
    assert del_file is not None and del_file.blob_sha is None


async def test_15_file_pr_second_commit_hits_sha_cache_and_saves_tokens() -> None:
    fake_redis = _FakeAsyncRedis()
    sha_cache = RedisBlobSHACache(fake_redis)  # type: ignore[arg-type]
    repo_config = RepoConfig(min_debt_score=60)
    settings = Settings(_env_file=None, gemini_api_key=SecretStr("fake-gemini-key"))

    # Commit 1: 15 files, all cache miss
    diff_v1, sources_v1 = _build_15_file_pr(
        file0_sha="a" * 40,
        file0_line='    execute_query(f"SELECT * FROM users WHERE id={user_input}")',
    )
    llm_v1 = _CountingFakeLLMFactory(settings)
    res_v1 = await execute_ai_pr_review(
        repo_full_name="acme/platform",
        repo_id="repo-101",
        pr_number=7,
        head_sha="111111111111",
        parsed_diff=diff_v1,
        repo_config=repo_config,
        settings=settings,
        trace_id="trace-v1",
        llm_factory=llm_v1,
        sha_cache=sha_cache,
        in_memory_after_sources=sources_v1,
    )
    assert res_v1.cache_hits == 0
    assert res_v1.cache_misses == 15
    assert len(fake_redis.store) == 15
    for cached_key, (raw_json, ttl) in fake_redis.store.items():
        assert cached_key.startswith("file_review:repo-101:")
        assert cached_key.endswith(f":{repo_config.config_hash}")
        assert ttl == DEFAULT_SHA_CACHE_TTL_SECONDS
        # NFR-3: never persist raw source code inside Redis cache payloads
        assert "def fn_" not in raw_json
        assert "SELECT * FROM users" not in raw_json

    # Commit 2: only src/file_00.py changes; 14 other files keep exact same blob_sha
    diff_v2, sources_v2 = _build_15_file_pr(
        file0_sha="f" * 40,
        file0_line='    execute_query(f"SELECT * FROM users WHERE id={user_input}")',
    )
    fake_redis.mget_calls = 0
    lookup_v2 = await sha_cache.get_many(
        repo_id="repo-101",
        config_hash=repo_config.config_hash,
        files=diff_v2.files,
    )
    assert fake_redis.mget_calls == 1
    assert lookup_v2.hit_count == 14
    assert lookup_v2.miss_count == 1
    assert lookup_v2.elapsed_ms < 10.0

    llm_v2 = _CountingFakeLLMFactory(settings)
    res_v2 = await execute_ai_pr_review(
        repo_full_name="acme/platform",
        repo_id="repo-101",
        pr_number=7,
        head_sha="222222222222",
        parsed_diff=diff_v2,
        repo_config=repo_config,
        settings=settings,
        trace_id="trace-v2",
        llm_factory=llm_v2,
        sha_cache=sha_cache,
        in_memory_after_sources=sources_v2,
    )
    assert res_v2.cache_hits == 14
    assert res_v2.cache_misses == 1
    assert 80.0 <= res_v2.tokens_saved_pct < 100.0
    assert res_v2.breakdown.score == res_v1.breakdown.score
    assert llm_v2.prompt_chars < llm_v1.prompt_chars * 0.25


async def test_sha_cache_fail_open_corrupt_entries_and_hunk_revalidation() -> None:
    # 1. Fail-open on Redis timeout/connection error
    broken_redis = _FakeAsyncRedis(fail_mget=True, fail_set=True)
    broken_cache = RedisBlobSHACache(broken_redis)  # type: ignore[arg-type]
    diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/a.py",
                status="modified",
                blob_sha="c" * 40,
                patch="@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
            )
        ]
    )
    degraded = await broken_cache.get_many(
        repo_id="repo-1",
        config_hash="cfg1",
        files=diff.files,
    )
    assert degraded.degraded is True
    assert degraded.hit_count == 0
    assert degraded.miss_count == 1
    written = await broken_cache.set_many(
        repo_id="repo-1",
        config_hash="cfg1",
        entries=[
            CachedFileReview(
                file_path="src/a.py",
                blob_sha="c" * 40,
                config_hash="cfg1",
            )
        ],
    )
    assert written == 0

    # 2. Corrupt JSON entry & stale out-of-hunk suggestion stripping + rename path remap
    good_redis = _FakeAsyncRedis()
    cache = RedisBlobSHACache(good_redis)  # type: ignore[arg-type]
    bad_key = build_sha_cache_key("repo-1", "d" * 40, "cfg1")
    good_redis.store[bad_key] = ("{not-valid-json", 60)
    stale_finding = VerifiedFinding(
        candidate_id="sec-001",
        role="security_auditor",
        batch_index=0,
        file_path="src/old_name.py",
        start_line=99,
        end_line=99,
        severity="WARNING",
        category="bug",
        title="Old finding",
        evidence="L99",
        confidence=0.85,
        rationale="Verified",
        suggestion=CodeSuggestion(
            start_line=99,
            end_line=99,
            replacement_code="pass",
            explanation="Replace stale line",
        ),
    )
    valid_key = build_sha_cache_key("repo-1", "e" * 40, "cfg1")
    good_redis.store[valid_key] = (
        CachedFileReview(
            file_path="src/old_name.py",
            blob_sha="e" * 40,
            config_hash="cfg1",
            findings=(stale_finding,),
        ).model_dump_json(),
        60,
    )

    renamed_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/corrupt.py",
                status="modified",
                blob_sha="d" * 40,
                patch="@@ -1 +1,2 @@\n x = 1\n+y = 2\n",
            ),
            PatchFileEntry(
                filename="src/new_name.py",
                previous_filename="src/old_name.py",
                status="renamed",
                blob_sha="e" * 40,
                patch="@@ -1 +1,2 @@\n x = 1\n+y = 2\n",
            ),
        ]
    )
    res = await cache.get_many(
        repo_id="repo-1",
        config_hash="cfg1",
        files=renamed_diff.files,
    )
    assert res.hit_count == 1
    assert res.miss_count == 1
    hit = res.hits["src/new_name.py"]
    assert hit.findings[0].file_path == "src/new_name.py"
    assert hit.findings[0].suggestion is None
    assert hit.findings[0].suggestion_stripped_reason == "cached_outside_current_hunk"
    assert hit.findings[0].suggestion_explanation == "Replace stale line"
    # config_hash mismatch causes cache miss
    miss_cfg = await cache.get_many(
        repo_id="repo-1",
        config_hash="cfg2",
        files=renamed_diff.files,
    )
    assert miss_cfg.hit_count == 0


async def test_github_pr_client_enriches_short_blob_sha_to_40_hex() -> None:
    raw_diff = (
        "diff --git a/src/service.py b/src/service.py\n"
        "index 1234567..89abcde 100644\n"
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -1 +1,2 @@\n"
        " def run():\n"
        "+    return 1\n"
    )
    parsed = SmartDiffParser().parse_diff(raw_diff)
    assert parsed.get_file("src/service.py") is not None
    assert parsed.get_file("src/service.py").blob_sha == "89abcde"  # type: ignore[union-attr]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/pulls/5/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/service.py",
                        "status": "modified",
                        "sha": "89abcdef" * 5,
                        "patch": "@@ -1 +1,2 @@\n def run():\n+    return 1\n",
                    }
                ],
            )
        return httpx.Response(404)

    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        auth = GitHubAuthProvider(settings, http_client=http_client)
        client = GitHubPullRequestClient(auth, http_client=http_client)
        enriched = await client.enrich_missing_blob_shas("acme/app", 5, parsed)
        svc = enriched.get_file("src/service.py")
        assert svc is not None
        assert svc.blob_sha == "89abcdef" * 5


async def test_sha_cache_short_sha_guard_token_units_fingerprint_and_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Unit-consistent token savings: 1 of 2 equal files cached -> ~50% (never inflated to 100%)
    equal_diff, _ = _build_15_file_pr(file0_sha="0" * 40, file0_line="    val_0 = 1")
    two_file_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/f1.py",
                status="modified",
                blob_sha="1" * 40,
                patch="@@ -1,2 +1,3 @@\n def f1():\n+    return 1\n     pass\n",
            ),
            PatchFileEntry(
                filename="src/f2.py",
                status="modified",
                blob_sha="2" * 40,
                patch="@@ -1,2 +1,3 @@\n def f2():\n+    return 2\n     pass\n",
            ),
        ]
    )
    _ = equal_diff
    pct_half = compute_combined_tokens_saved_pct(
        two_file_diff, cached_files=[two_file_diff.files[0]]
    )
    assert 45.0 <= pct_half <= 55.0

    # 2. Short 7-hex blob_sha is ignored by RedisBlobSHACache and falls back to /contents/?ref=
    fake_redis = _FakeAsyncRedis()
    cache = RedisBlobSHACache(fake_redis)  # type: ignore[arg-type]
    short_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/short.py",
                status="modified",
                blob_sha="89abcde",
                patch="@@ -1 +1,2 @@\n def run():\n+    return 1\n",
            )
        ]
    )
    short_written = await cache.set_many(
        repo_id="repo-1",
        config_hash="cfg1",
        entries=[
            CachedFileReview(
                file_path="src/short.py",
                blob_sha="89abcde",
                config_hash="cfg1",
            )
        ],
    )
    assert short_written == 0
    short_lookup = await cache.get_many(
        repo_id="repo-1",
        config_hash="cfg1",
        files=short_diff.files,
    )
    assert short_lookup.hit_count == 0
    assert fake_redis.mget_calls == 0

    requested_urls: list[str] = []

    def blob_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url.path)
        if request.url.path == "/repos/acme/app/contents/src/short.py":
            import base64

            b64 = base64.b64encode(b"def run():\n    return 1\n").decode("ascii")
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": "8" * 40,
                    "size": 24,
                    "encoding": "base64",
                    "content": b64,
                },
            )
        return httpx.Response(404)

    settings = Settings(_env_file=None, gemini_api_key=SecretStr("fake-gemini"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(blob_handler)) as http_client:
        fetcher = GitHubBlobFetcher(None, http_client=http_client, allow_public_fallback=True)
        res = await execute_ai_pr_review(
            repo_full_name="acme/app",
            repo_id="repo-1",
            pr_number=5,
            head_sha="headsha123",
            parsed_diff=short_diff,
            repo_config=RepoConfig(),
            settings=settings,
            trace_id="trace-short",
            blob_fetcher=fetcher,
            llm_factory=_CountingFakeLLMFactory(settings),
            sha_cache=cache,
        )
        assert res.cache_hits == 0
    assert any(u == "/repos/acme/app/contents/src/short.py" for u in requested_urls)
    assert not any("/git/blobs/" in u for u in requested_urls)

    # 3. Pure 0-diff rename is skipped and patch_fingerprint mismatch causes a cache miss
    diff_hunk_a = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/fp.py",
                status="modified",
                blob_sha="a" * 40,
                patch="@@ -1,2 +1,3 @@\n def f():\n+    return 1\n     pass\n",
            )
        ]
    )
    diff_hunk_b = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/fp.py",
                status="modified",
                blob_sha="a" * 40,
                patch="@@ -10,2 +10,3 @@\n def g():\n+    return 2\n     pass\n",
            )
        ]
    )
    pure_rename = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/moved.py",
                previous_filename="src/fp.py",
                status="renamed",
                blob_sha="a" * 40,
                patch=None,
            )
        ]
    )
    await cache.set_many(
        repo_id="repo-1",
        config_hash="cfg1",
        entries=[
            CachedFileReview(
                file_path="src/fp.py",
                blob_sha="a" * 40,
                config_hash="cfg1",
                patch_fingerprint=compute_patch_fingerprint(diff_hunk_a.files[0]),
            )
        ],
    )
    assert (
        await cache.get_many(repo_id="repo-1", config_hash="cfg1", files=diff_hunk_a.files)
    ).hit_count == 1
    assert (
        await cache.get_many(repo_id="repo-1", config_hash="cfg1", files=diff_hunk_b.files)
    ).hit_count == 0
    assert (
        await cache.get_many(repo_id="repo-1", config_hash="cfg1", files=pure_rename.files)
    ).hit_count == 0

    # 4. Degraded RAG & truncated/unreviewable batches must NOT write to Redis SHA cache
    class _DummyQdrant(QdrantZeroCodeStore):
        def __init__(self) -> None:
            pass

    async def _failing_retrieve_and_pack(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Qdrant down")

    monkeypatch.setattr(HybridContextRetriever, "retrieve_and_pack", _failing_retrieve_and_pack)

    rag_redis = _FakeAsyncRedis()
    rag_cache = RedisBlobSHACache(rag_redis)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.MockTransport(blob_handler)) as http_client:
        fetcher = GitHubBlobFetcher(None, http_client=http_client, allow_public_fallback=True)
        await execute_ai_pr_review(
            repo_full_name="acme/app",
            repo_id="repo-1",
            pr_number=5,
            head_sha="headsha123",
            parsed_diff=diff_hunk_a,
            repo_config=RepoConfig(),
            settings=settings,
            trace_id="trace-rag-degraded",
            blob_fetcher=fetcher,
            llm_factory=_CountingFakeLLMFactory(settings),
            sha_cache=rag_cache,
            qdrant_store=_DummyQdrant(),
            in_memory_after_sources={"src/fp.py": "def f():\n    return 1\n    pass\n"},
        )
    assert len(rag_redis.store) == 0

    # 5. Out-of-hunk cached finding is downgraded to PR Summary on publish (Zero 422)
    out_of_hunk_finding = VerifiedFinding(
        candidate_id="warn-ooh",
        role="logic_performance_auditor",
        batch_index=0,
        file_path="src/fp.py",
        start_line=99,
        end_line=99,
        severity="WARNING",
        category="bug",
        title="Out of hunk warning",
        evidence="L99 issue",
        confidence=0.85,
        rationale="Verified",
    )
    posted_reviews: list[dict[str, Any]] = []

    def pub_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/pulls/5/reviews":
            import json

            posted_reviews.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"id": 88, "state": "COMMENTED"})
        return httpx.Response(404)

    pub_redis = _FakeAsyncRedis()
    pub_cache = RedisBlobSHACache(pub_redis)  # type: ignore[arg-type]
    await pub_cache.set_many(
        repo_id="repo-1",
        config_hash=RepoConfig().config_hash,
        entries=[
            CachedFileReview(
                file_path="src/fp.py",
                blob_sha="a" * 40,
                config_hash=RepoConfig().config_hash,
                patch_fingerprint=compute_patch_fingerprint(diff_hunk_a.files[0]),
                findings=(out_of_hunk_finding,),
            )
        ],
    )
    res_ooh = await execute_ai_pr_review(
        repo_full_name="acme/app",
        repo_id="repo-1",
        pr_number=5,
        head_sha="headsha123",
        parsed_diff=diff_hunk_a,
        repo_config=RepoConfig(),
        settings=settings,
        trace_id="trace-ooh",
        llm_factory=_CountingFakeLLMFactory(settings),
        sha_cache=pub_cache,
    )
    gh_settings = Settings(_env_file=None, github_token=SecretStr("ghp_test"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(pub_handler)) as http_client:
        pub = GitHubReviewPublisher(
            GitHubAuthProvider(gh_settings, http_client=http_client), http_client=http_client
        )
        pub_rec = await pub.publish_review(
            "acme/app",
            5,
            head_sha="headsha123",
            parsed_diff=res_ooh.parsed_diff,
            drafts=res_ooh.inline_drafts,
            summary_body=res_ooh.summary_markdown,
            event=res_ooh.gate.review_event,
            after_sources_by_file=res_ooh.after_sources_by_file,
        )
    assert pub_rec.inline_count == 0
    assert pub_rec.downgraded_count == 1
    assert "Out of hunk warning" in posted_reviews[0]["body"]

    # 6. RateLimitTaskError propagates out of enrich_missing_blob_shas
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rate_limit_handler)) as http_client:
        auth = GitHubAuthProvider(gh_settings, http_client=http_client)
        client = GitHubPullRequestClient(auth, http_client=http_client)
        with pytest.raises(RateLimitTaskError):
            await client.enrich_missing_blob_shas("acme/app", 5, short_diff)
