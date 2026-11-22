"""Unit tests for GitHubBlobFetcher (RAM slicing) and HybridContextRetriever (CA-15)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.diff.file_filter import estimate_tokens
from codesight.vcs.blob_fetcher import (
    BlobSliceRequest,
    GitHubBlobFetcher,
    GitHubBlobFetchError,
    decode_github_base64_blob,
)
from codesight.vcs.github_auth import GitHubAuthError, GitHubAuthProvider


def _sha1_blob(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
    ).hexdigest()


def _blob_json(content: bytes, *, sha: str | None = None) -> dict[str, Any]:
    return {
        "sha": sha or _sha1_blob(content),
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _contents_json(path: str, content: bytes, *, sha: str | None = None) -> dict[str, Any]:
    return {
        "type": "file",
        "path": path,
        "sha": sha or _sha1_blob(content),
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _make_auth(token: str = "ghp_test_token") -> GitHubAuthProvider:
    settings = Settings(github_token=SecretStr(token))
    return GitHubAuthProvider(settings)


@pytest.mark.asyncio
async def test_blob_fetcher_deduplicates_unique_sha_and_slices_concurrently() -> None:
    """Six slice requests referencing two unique blobs trigger only two concurrent HTTP GETs."""
    file_a = "\n".join(f"line_a_{i}" for i in range(1, 31)).encode("utf-8")
    file_b = "\n".join(f"line_b_{i}" for i in range(1, 21)).encode("utf-8")
    sha_a = _sha1_blob(file_a)
    sha_b = _sha1_blob(file_b)

    requested_urls: list[str] = []
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        requested_urls.append(str(request.url))
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            if request.url.path.endswith(f"/git/blobs/{sha_a}"):
                return httpx.Response(200, json=_blob_json(file_a, sha=sha_a))
            if request.url.path.endswith(f"/git/blobs/{sha_b}"):
                return httpx.Response(200, json=_blob_json(file_b, sha=sha_b))
            return httpx.Response(404)
        finally:
            in_flight -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        requests = [
            BlobSliceRequest("acme/app", "src/a.py", 1, 3, blob_sha=sha_a, slice_id="a1"),
            BlobSliceRequest("acme/app", "src/a.py", 10, 12, blob_sha=sha_a, slice_id="a2"),
            BlobSliceRequest("acme/app", "src/a.py", 28, 40, blob_sha=sha_a, slice_id="a3"),
            BlobSliceRequest("acme/app", "src/b.py", 5, 6, blob_sha=sha_b, slice_id="b1"),
            BlobSliceRequest("acme/app", "src/b.py", 1, 2, blob_sha=sha_b, slice_id="b2"),
            BlobSliceRequest("acme/app", "src/b.py", 19, 20, blob_sha=sha_b, slice_id="b3"),
        ]
        report = await fetcher.fetch_slices(requests)

    assert len(requested_urls) == 2
    assert max_in_flight == 2
    assert report.unique_blobs_requested == 2
    assert report.unique_blobs_fetched == 2
    assert report.skipped_blobs == 0
    assert len(report.slices) == 6

    # Order matches input requests 1-to-1 and lines are 1-based inclusive with clamping
    assert report.slices[0].status == "ok"
    assert report.slices[0].content == "line_a_1\nline_a_2\nline_a_3"
    assert report.slices[0].annotated_content == "L1: line_a_1\nL2: line_a_2\nL3: line_a_3"
    assert report.slices[0].full_file_tokens == estimate_tokens(file_a)
    assert report.slices[0].total_file_lines == 30

    # Out-of-bounds end_line=40 is safely clamped to total_file_lines=30
    assert report.slices[2].start_line == 28
    assert report.slices[2].end_line == 30
    assert report.slices[2].content == "line_a_28\nline_a_29\nline_a_30"

    assert report.slices[3].content == "line_b_5\nline_b_6"
    assert report.slices[3].annotated_content == "L5: line_b_5\nL6: line_b_6"
    assert report.full_file_tokens_by_path["src/a.py"] == estimate_tokens(file_a)
    assert report.full_file_tokens_by_path["src/b.py"] == estimate_tokens(file_b)


@pytest.mark.asyncio
async def test_blob_fetcher_supports_contents_api_fallback_when_blob_sha_is_none() -> None:
    """PR head requests without blob_sha deduplicate by (repo, ref, file_path) via Contents API."""
    source = b"def verify(token: str) -> bool:\n    return bool(token)\n"
    sha = _sha1_blob(source)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/repos/acme/app/contents/src/auth.py":
            assert request.url.params.get("ref") == "head123456"
            return httpx.Response(200, json=_contents_json("src/auth.py", source, sha=sha))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        report = await fetcher.fetch_slices(
            [
                BlobSliceRequest("acme/app", "./src/auth.py", 1, 1, ref="head123456"),
                BlobSliceRequest("acme/app", "src/auth.py", 1, 2, ref="head123456"),
            ]
        )

    assert len(calls) == 1
    assert report.unique_blobs_requested == 1
    assert report.unique_blobs_fetched == 1
    assert report.slices[0].resolved_blob_sha == sha
    assert report.slices[0].content == "def verify(token: str) -> bool:"
    assert report.slices[1].annotated_content == (
        "L1: def verify(token: str) -> bool:\nL2:     return bool(token)"
    )


@pytest.mark.asyncio
async def test_blob_fetcher_uses_in_memory_sources_only_for_head_or_matching_sha() -> None:
    """PR head in-memory source avoids HTTP without shadowing a Qdrant main-branch blob_sha."""
    head_src = "def fn():\n    return 'pr-head'\n"
    main_src = b"def fn():\n    return 'main-branch'\n"
    main_sha = _sha1_blob(main_src)
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        if request.url.path.endswith(f"/git/blobs/{main_sha}"):
            return httpx.Response(200, json=_blob_json(main_src, sha=main_sha))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        report = await fetcher.fetch_slices(
            [
                BlobSliceRequest(
                    "acme/app",
                    "src/service.py",
                    1,
                    2,
                    blob_sha="pr-head-sha-0000",
                    prefer_in_memory=True,
                ),
                BlobSliceRequest("acme/app", "src/service.py", 1, 2, blob_sha=main_sha),
            ],
            in_memory_sources={"src/service.py": head_src},
        )

    assert http_calls == 1
    assert "pr-head" in report.slices[0].content
    assert "main-branch" in report.slices[1].content


@pytest.mark.asyncio
async def test_blob_fetcher_handles_404_binary_oversize_and_empty_gracefully() -> None:
    """Per-blob 404, NUL binary, >max_blob_bytes, and empty files skip without aborting batch."""
    ok_src = b"alpha\nbeta\n"
    bin_src = b"\x89PNG\r\n\x00binary"
    big_src = b"x" * 2048
    empty_src = b""

    sha_ok = _sha1_blob(ok_src)
    sha_bin = _sha1_blob(bin_src)
    sha_big = _sha1_blob(big_src)
    sha_empty = _sha1_blob(empty_src)
    sha_missing = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(sha_ok):
            return httpx.Response(200, json=_blob_json(ok_src, sha=sha_ok))
        if path.endswith(sha_bin):
            return httpx.Response(200, json=_blob_json(bin_src, sha=sha_bin))
        if path.endswith(sha_big):
            return httpx.Response(200, json=_blob_json(big_src, sha=sha_big))
        if path.endswith(sha_empty):
            return httpx.Response(200, json=_blob_json(empty_src, sha=sha_empty))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client, max_blob_bytes=1024)
        report = await fetcher.fetch_slices(
            [
                BlobSliceRequest("acme/app", "ok.py", 1, 2, blob_sha=sha_ok),
                BlobSliceRequest("acme/app", "img.bin", 1, 1, blob_sha=sha_bin),
                BlobSliceRequest("acme/app", "huge.py", 1, 10, blob_sha=sha_big),
                BlobSliceRequest("acme/app", "empty.py", 1, 5, blob_sha=sha_empty),
                BlobSliceRequest("acme/app", "gone.py", 1, 5, blob_sha=sha_missing),
            ]
        )

    assert [s.status for s in report.slices] == ["ok", "binary", "oversize", "empty", "not_found"]
    assert report.slices[0].content == "alpha\nbeta"
    assert report.unique_blobs_fetched == 2  # ok + empty decoded successfully
    assert report.skipped_blobs == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "headers", "expected_exc"),
    [
        (429, {"Retry-After": "12"}, RateLimitTaskError),
        (403, {"X-RateLimit-Remaining": "0", "Retry-After": "5"}, RateLimitTaskError),
        (403, {}, GitHubAuthError),
        (502, {}, RetryableTaskError),
    ],
)
async def test_blob_fetcher_maps_github_error_statuses(
    status_code: int,
    headers: dict[str, str],
    expected_exc: type[Exception],
) -> None:
    """Auth, rate-limit, and server errors propagate with exact CodeSight exception types."""
    sha = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        with pytest.raises(expected_exc) as exc_info:
            await fetcher.fetch_slices([BlobSliceRequest("acme/app", "a.py", 1, 2, blob_sha=sha)])

    if expected_exc is RateLimitTaskError:
        err = exc_info.value
        assert isinstance(err, RateLimitTaskError)
        assert err.retry_after_seconds is not None and err.retry_after_seconds >= 5.0


@pytest.mark.asyncio
async def test_blob_fetcher_invalidates_installation_token_and_retries_once_on_401() -> None:
    """HTTP 401 with installation_id invalidates token cache and retries once with fresh token."""
    src = b"x = 1\n"
    sha = _sha1_blob(src)
    attempts = 0
    invalidated: list[int] = []

    class _FakeAuth(GitHubAuthProvider):
        def __init__(self) -> None:
            super().__init__(Settings(github_token=SecretStr("tok-1")))
            self.calls = 0

        async def get_auth_headers(
            self,
            *,
            installation_id: int | None = None,
            repo_full_name: str | None = None,
        ) -> dict[str, str]:
            self.calls += 1
            return {"Authorization": f"Bearer tok-{self.calls}"}

        async def invalidate_installation_token(self, installation_id: int) -> None:
            invalidated.append(installation_id)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if request.headers.get("Authorization") == "Bearer tok-1":
            return httpx.Response(401)
        return httpx.Response(200, json=_blob_json(src, sha=sha))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_FakeAuth(), http_client=client)
        report = await fetcher.fetch_slices(
            [BlobSliceRequest("acme/app", "a.py", 1, 1, blob_sha=sha)],
            installation_id=99,
        )

    assert attempts == 2
    assert invalidated == [99]
    assert report.slices[0].status == "ok"
    assert report.slices[0].content == "x = 1"


@pytest.mark.asyncio
async def test_blob_fetcher_blocks_cross_origin_redirect_and_handles_transport_error() -> None:
    """Cross-origin redirects raise GitHubBlobFetchError and transport errors raise Retryable."""
    sha = "b" * 40

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/blob"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        with pytest.raises(GitHubBlobFetchError):
            await fetcher.fetch_slices([BlobSliceRequest("acme/app", "a.py", 1, 1, blob_sha=sha)])

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        with pytest.raises(RetryableTaskError):
            await fetcher.fetch_slices([BlobSliceRequest("acme/app", "a.py", 1, 1, blob_sha=sha)])


def test_decode_github_base64_blob_rejects_corrupt_and_non_file_payloads() -> None:
    """Corrupt JSON, invalid base64, and non-file Contents entries return ('invalid', None)."""
    raw_bad_json = b"not-json"
    assert decode_github_base64_blob(raw_bad_json)[:2] == (None, "invalid")

    raw_dir = json.dumps({"type": "dir", "encoding": "base64", "content": "YWJj"}).encode("utf-8")
    assert decode_github_base64_blob(raw_dir)[:2] == (None, "invalid")

    raw_bad_b64 = json.dumps({"encoding": "base64", "content": "!!!"}).encode("utf-8")
    assert decode_github_base64_blob(raw_bad_b64)[:2] == (None, "invalid")

    with pytest.raises(ValueError):
        BlobSliceRequest("acme/app", "a.py", 5, 3, blob_sha="a" * 40)
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/app", "a.py", 1, 2)  # missing both blob_sha and ref


# ---------------------------------------------------------------------------
# HybridContextRetriever & retrieve_and_pack() unit tests (Cycle 3 TDD)
# ---------------------------------------------------------------------------


def _make_qdrant_point(
    *,
    point_id: str,
    file_path: str,
    blob_sha: str,
    symbol_name: str,
    symbol_kind: str = "function",
    start_line: int = 1,
    end_line: int = 10,
    signature: str = "def fn()",
    called_symbols: list[str] | None = None,
    imports: list[str] | None = None,
    chunk_type: str = "code",
    language: str = "python",
    cyclomatic_complexity: int = 2,
    score: float | None = None,
) -> Any:
    from codesight.rag.models import QdrantStoredPoint

    return QdrantStoredPoint(
        point_id=point_id,
        score=score,
        payload={
            "repo_full_name": "acme/app",
            "file_path": file_path,
            "blob_sha": blob_sha,
            "chunk_type": chunk_type,
            "language": language,
            "symbol_name": symbol_name,
            "symbol_kind": symbol_kind,
            "start_line": start_line,
            "end_line": end_line,
            "signature": signature,
            "called_symbols": called_symbols or [],
            "imports": imports or [],
            "cyclomatic_complexity": cyclomatic_complexity,
        },
    )


class _StubQdrantStore:
    def __init__(
        self,
        *,
        callers: tuple[Any, ...] = (),
        callees: tuple[Any, ...] = (),
        docs: tuple[Any, ...] = (),
    ) -> None:
        self._callers = callers
        self._callees = callees
        self._docs = docs
        self.called_symbols_queries: list[list[str]] = []
        self.symbol_names_queries: list[list[str]] = []
        self.similar_queries: list[dict[str, Any]] = []

    async def find_by_called_symbols(
        self,
        called_symbols: list[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Any, ...]:
        self.called_symbols_queries.append(list(called_symbols))
        return self._callers[:limit]

    async def find_by_symbol_names(
        self,
        symbol_names: list[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Any, ...]:
        self.symbol_names_queries.append(list(symbol_names))
        return self._callees[:limit]

    async def search_similar(
        self,
        query_vector: tuple[float, ...],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        chunk_type: str | None = None,
        score_threshold: float | None = None,
        limit: int = 10,
    ) -> tuple[Any, ...]:
        self.similar_queries.append(
            {
                "dim": len(query_vector),
                "chunk_type": chunk_type,
                "score_threshold": score_threshold,
                "limit": limit,
            }
        )
        return self._docs[:limit]


class _StubEmbedder:
    def __init__(self, *, fail_exc: Exception | None = None) -> None:
        self.queries: list[str] = []
        self._fail_exc = fail_exc

    async def embed_query(self, query_text: str) -> tuple[float, ...]:
        self.queries.append(query_text)
        if self._fail_exc is not None:
            raise self._fail_exc
        return (1.0,) + (0.0,) * 767


@pytest.mark.asyncio
async def test_retriever_batches_single_embed_query_filters_noise_and_ranks_import_affinity() -> (
    None
):
    """Batching emits 1 embed_query, excludes noise/self-hits, and boosts imported callers."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.rag.retriever import HybridContextRetriever

    # Modified PR has 3 symbols: 1 ubiquitous dunder '__init__', 2 real domain functions
    chunks = (
        CodeSymbolChunk(
            file_path="src/services/auth.py",
            language="python",
            symbol_name="__init__",
            qualified_name="AuthService.__init__",
            symbol_kind="method",
            start_line=5,
            end_line=10,
            signature="def __init__(self)",
            imports=("src.crypto.jwt_utils.decode_token",),
            called_symbols=("get", "append", "decode_token"),
        ),
        CodeSymbolChunk(
            file_path="src/services/auth.py",
            language="python",
            symbol_name="verify_session",
            qualified_name="AuthService.verify_session",
            symbol_kind="method",
            start_line=12,
            end_line=25,
            signature="def verify_session(self, raw: str) -> bool",
            docstring="Verify user session JWT and revocation state.",
            imports=("src.crypto.jwt_utils.decode_token",),
            called_symbols=("decode_token", "map", "check_revocation"),
        ),
        CodeSymbolChunk(
            file_path="src/services/auth.py",
            language="python",
            symbol_name="issue_session",
            qualified_name="AuthService.issue_session",
            symbol_kind="method",
            start_line=27,
            end_line=36,
            signature="def issue_session(self, user_id: str) -> str",
            imports=("src.crypto.jwt_utils.sign_token",),
            called_symbols=("sign_token", "format"),
        ),
    )
    pr_ctx = PRFileContext(
        file_path="src/services/auth.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/services/auth.py",
            language="python",
            imports=("src.crypto.jwt_utils.decode_token", "src.crypto.jwt_utils.sign_token"),
            chunks=chunks,
        ),
        touched_chunks=chunks,
    )

    # Callers include: 1 self-hit, 1 unrelated homonym caller, 1 explicitly imported caller
    caller_self = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000001",
        file_path="src/services/auth.py",
        blob_sha="1" * 40,
        symbol_name="verify_session",
        called_symbols=["verify_session"],
    )
    caller_unrelated = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000002",
        file_path="other_pkg/misc.py",
        blob_sha="2" * 40,
        symbol_name="unrelated_handler",
        called_symbols=["verify_session"],
        imports=["other_pkg.legacy"],
    )
    caller_imported = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000003",
        file_path="src/api/users_router.py",
        blob_sha="3" * 40,
        symbol_name="get_profile",
        called_symbols=["verify_session"],
        imports=["src.services.auth.AuthService"],
    )

    callee_imported = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000004",
        file_path="src/crypto/jwt_utils.py",
        blob_sha="4" * 40,
        symbol_name="decode_token",
    )
    callee_unrelated = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000005",
        file_path="scripts/old_cli.py",
        blob_sha="5" * 40,
        symbol_name="decode_token",
    )
    doc_hit = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000006",
        file_path="docs/adr/0001-auth.md",
        blob_sha="6" * 40,
        symbol_name="JWT Verification Rules",
        symbol_kind="section",
        chunk_type="architecture_doc",
        language="markdown",
        score=0.82,
    )

    store = _StubQdrantStore(
        callers=(caller_self, caller_unrelated, caller_imported),
        callees=(callee_unrelated, callee_imported),
        docs=(doc_hit,),
    )
    embedder = _StubEmbedder()
    retriever = HybridContextRetriever(store=store, embedder=embedder)  # type: ignore[arg-type]

    result = await retriever.retrieve(
        [pr_ctx],
        repo_full_name="acme/app",
        head_ref="head-sha-999",
    )

    # Single batched embed_query call regardless of multiple modified symbols
    assert len(embedder.queries) == 1
    assert store.similar_queries == [
        {"dim": 768, "chunk_type": "architecture_doc", "score_threshold": 0.65, "limit": 6}
    ]
    # Builtin noise dropped; class-qualified priority & split ambiguous lookup
    assert store.called_symbols_queries == [
        [
            "AuthService.verify_session",
            "verify_session",
            "AuthService.issue_session",
            "issue_session",
        ]
    ]
    assert set(store.symbol_names_queries[0]) == {
        "decode_token",
        "check_revocation",
        "sign_token",
    }
    assert set(store.symbol_names_queries[1]) == {"get", "format"}
    # Self-hit removed, explicitly imported caller & callee ranked first
    assert [c.symbol_name for c in result.callers] == ["get_profile", "unrelated_handler"]
    assert result.callers[0].relevance_score > result.callers[1].relevance_score
    assert [c.file_path for c in result.callees] == [
        "src/crypto/jwt_utils.py",
        "scripts/old_cli.py",
    ]
    assert result.callees[0].relevance_score > result.callees[1].relevance_score
    assert len(result.architecture_docs) == 1
    assert result.architecture_docs[0].relevance_score == pytest.approx(0.82)
    assert result.doc_search_degraded is False


@pytest.mark.asyncio
async def test_retriever_degrades_gracefully_when_embed_query_fails_transiently() -> None:
    """Transient Gemini embedding error degrades doc search and keeps exact Callers/Callees."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.rag.retriever import HybridContextRetriever

    chunk = CodeSymbolChunk(
        file_path="src/orders.py",
        language="python",
        symbol_name="submit_order",
        qualified_name="submit_order",
        symbol_kind="function",
        start_line=1,
        end_line=8,
        signature="def submit_order(order_id: str)",
        called_symbols=("charge_wallet",),
    )
    pr_ctx = PRFileContext(
        file_path="src/orders.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/orders.py",
            language="python",
            imports=(),
            chunks=(chunk,),
        ),
        touched_chunks=(chunk,),
    )
    caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000010",
        file_path="src/routes/orders.py",
        blob_sha="7" * 40,
        symbol_name="post_order",
        called_symbols=["submit_order"],
    )
    store = _StubQdrantStore(callers=(caller_pt,))
    embedder = _StubEmbedder(fail_exc=RateLimitTaskError("429 quota", retry_after_seconds=5.0))
    retriever = HybridContextRetriever(store=store, embedder=embedder)  # type: ignore[arg-type]

    result = await retriever.retrieve([pr_ctx], repo_full_name="acme/app")

    assert result.doc_search_degraded is True
    assert result.architecture_docs == ()
    assert [c.symbol_name for c in result.callers] == ["post_order"]


@pytest.mark.asyncio
async def test_retrieve_and_pack_orchestrates_end_to_end_with_split_latency_telemetry() -> None:
    """retrieve_and_pack chains retrieve -> fetch_slices -> pack and returns split timings & KPI."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.diff.smart_diff_parser import SmartDiffParser
    from codesight.rag.retriever import HybridContextRetriever
    from codesight.rag.token_packer import TokenBudgetPacker

    auth_lines = [
        "from src.crypto import decode_jwt",
        *[f"# helper padding line {i} {'p' * 30}" for i in range(2, 60)],
        "def verify_token(token: str) -> bool:",
        "    claims = decode_jwt(token)",
        "    return bool(claims.get('sub'))",
        *[f"# trailing padding line {i} {'p' * 30}" for i in range(63, 120)],
    ]
    auth_src = ("\n".join(auth_lines) + "\n").encode("utf-8")

    router_lines = [
        "from src.auth import verify_token",
        *[f"# router filler {i} {'r' * 30}" for i in range(2, 50)],
        "def get_account(token: str) -> dict:",
        "    if not verify_token(token):",
        "        raise PermissionError('denied')",
        "    return {'ok': True}",
        *[f"# router tail {i} {'r' * 30}" for i in range(54, 110)],
    ]
    router_src = ("\n".join(router_lines) + "\n").encode("utf-8")
    router_sha = _sha1_blob(router_src)

    adr_src = (
        "# Auth Architecture\n\n"
        "## Token Validation\n"
        "Every endpoint MUST validate JWT signatures via verify_token.\n"
        + ("Extra ADR notes for context baseline sizing.\n" * 40)
    ).encode("utf-8")
    adr_sha = _sha1_blob(adr_src)

    raw_diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -60,2 +60,3 @@ def verify_token\n"
        " def verify_token(token: str) -> bool:\n"
        "+    claims = decode_jwt(token)\n"
        "     return bool(claims.get('sub'))\n"
    )
    parsed_diff = SmartDiffParser().parse_diff(raw_diff)

    chunk = CodeSymbolChunk(
        file_path="src/auth.py",
        language="python",
        symbol_name="verify_token",
        qualified_name="verify_token",
        symbol_kind="function",
        start_line=60,
        end_line=62,
        signature="def verify_token(token: str) -> bool",
        imports=("src.crypto.decode_jwt",),
        called_symbols=("decode_jwt",),
    )
    pr_ctx = PRFileContext(
        file_path="src/auth.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/auth.py",
            language="python",
            imports=("src.crypto.decode_jwt",),
            chunks=(chunk,),
        ),
        touched_chunks=(chunk,),
    )

    caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000021",
        file_path="src/router.py",
        blob_sha=router_sha,
        symbol_name="get_account",
        start_line=50,
        end_line=53,
        signature="def get_account(token: str) -> dict",
        called_symbols=["verify_token"],
        imports=["src.auth.verify_token"],
    )
    doc_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000022",
        file_path="docs/adr/0001-auth.md",
        blob_sha=adr_sha,
        symbol_name="Token Validation",
        symbol_kind="section",
        start_line=3,
        end_line=4,
        signature="Token Validation",
        chunk_type="architecture_doc",
        language="markdown",
        score=0.88,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/git/blobs/{router_sha}"):
            return httpx.Response(200, json=_blob_json(router_src, sha=router_sha))
        if request.url.path.endswith(f"/git/blobs/{adr_sha}"):
            return httpx.Response(200, json=_blob_json(adr_src, sha=adr_sha))
        if request.url.path == "/repos/acme/app/contents/src/auth.py":
            return httpx.Response(200, json=_contents_json("src/auth.py", auth_src))
        return httpx.Response(404)

    store = _StubQdrantStore(callers=(caller_pt,), docs=(doc_pt,))
    embedder = _StubEmbedder()
    retriever = HybridContextRetriever(store=store, embedder=embedder)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        assembled = await retriever.retrieve_and_pack(
            [pr_ctx],
            repo_full_name="acme/app",
            blob_fetcher=fetcher,
            packer=TokenBudgetPacker(budget_tokens=3500),
            parsed_diff=parsed_diff,
            head_ref="pr-head-sha",
        )

    assert [b.tier for b in assembled.packed_bundle.blocks] == [
        "enclosing_symbol",
        "caller",
        "architecture_doc",
    ]
    assert assembled.qdrant_ms >= 0.0
    assert assembled.blob_fetch_ms >= 0.0
    assert assembled.pack_ms >= 0.0
    assert assembled.total_ms >= 0.0
    assert assembled.packed_bundle.metrics.packed_aux_tokens <= 3500
    assert assembled.packed_bundle.metrics.tokens_saved_pct >= 45.0
    assert "L60: def verify_token" in assembled.packed_bundle.rendered_context
    assert "L50: def get_account" in assembled.packed_bundle.rendered_context
    assert "Token Validation" in assembled.packed_bundle.rendered_context


@pytest.mark.asyncio
async def test_blob_fetcher_edge_cases_empty_public_fallback_and_in_memory_guards() -> None:
    """Empty requests, public fallback, owned client lifecycle, and binary in-memory guards."""
    src = b"hello\nworld\n"
    sha = _sha1_blob(src)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=_blob_json(src, sha=sha))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(None, http_client=client, allow_public_fallback=True)
        empty_report = await fetcher.fetch_slices([])
        assert empty_report.slices == ()
        assert empty_report.unique_blobs_requested == 0

        pub_report = await fetcher.fetch_slices(
            [
                BlobSliceRequest("acme/app", "a.py", 1, 2, blob_sha=sha),
                BlobSliceRequest("acme/app", "bin.py", 1, 1, prefer_in_memory=True),
                BlobSliceRequest("acme/app", "big.py", 1, 1, prefer_in_memory=True),
                BlobSliceRequest("acme/app", "absent.py", 1, 1, prefer_in_memory=True),
            ],
            in_memory_sources={
                "bin.py": b"abc\x00def",
                "big.py": b"x" * (600 * 1024),
            },
        )

    assert [s.status for s in pub_report.slices] == ["ok", "binary", "oversize", "not_found"]
    assert pub_report.slices[0].content == "hello\nworld"

    # Owned client opens & closes cleanly via async context manager
    async with GitHubBlobFetcher(None, allow_public_fallback=True) as owned:
        assert await owned.fetch_slices([]) is not None


@pytest.mark.asyncio
async def test_retriever_hunk_fallback_no_embedder_and_validation() -> None:
    """Hunk-fallback PR files without blob_sha still assemble context, and bad thresholds raise."""
    from codesight.ast_engine.models import HunkContextSlice, PRFileContext
    from codesight.rag.retriever import HybridContextRetriever

    store = _StubQdrantStore()
    with pytest.raises(ValueError):
        HybridContextRetriever(store=store, doc_score_threshold=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HybridContextRetriever(store=store, caller_limit=0)  # type: ignore[arg-type]

    hunk_ctx = PRFileContext(
        file_path="deploy/config.yaml",
        mode="hunk_fallback",
        language=None,
        hunk_slices=(
            HunkContextSlice(
                file_path="deploy/config.yaml",
                hunk_id=1,
                start_line=10,
                end_line=14,
                section_header="timeout_settings",
                annotated_text="L10: timeout: 30\nL11: retries: 3",
            ),
        ),
        fallback_reason="unsupported_extension",
    )

    retriever = HybridContextRetriever(store=store, embedder=None)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(404))
    ) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        assembled = await retriever.retrieve_and_pack(
            [hunk_ctx],
            repo_full_name="acme/app",
            blob_fetcher=fetcher,
        )

    assert assembled.retrieval.doc_search_degraded is False
    assert assembled.retrieval.architecture_docs == ()
    assert len(assembled.packed_bundle.blocks) == 1
    assert "timeout: 30" in assembled.packed_bundle.rendered_context


# ---------------------------------------------------------------------------
# Regression tests for CA-15 review-01 findings (F-01 .. F-11)
# ---------------------------------------------------------------------------


def test_blob_slice_request_rejects_dot_segment_path_traversal_and_unsafe_sha() -> None:
    """[F-01] Reject '../', '.', NUL/control chars in file_path and unsafe chars in blob_sha."""
    with pytest.raises(ValueError):
        BlobSliceRequest(
            "acme/public",
            "../../../../repos/acme/private/contents/.env",
            1,
            20,
            ref="main",
        )
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/public", "src/../secret.py", 1, 10, ref="main")
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/public", "src/./auth.py", 1, 10, ref="main")
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/public", "src/auth.py\x00.txt", 1, 10, ref="main")
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/public", "src/auth.py", 1, 10, blob_sha="..")
    with pytest.raises(ValueError):
        BlobSliceRequest("acme/public", "src/auth.py", 1, 10, blob_sha="abc/../def")


@pytest.mark.asyncio
async def test_blob_fetcher_treats_contents_api_403_too_large_as_oversize() -> None:
    """[F-02] Contents API HTTP 403 'too_large' (>1 MiB) marks slice oversize without failing PR."""
    ok_src = b"def small():\n    return 1\n"
    ok_sha = _sha1_blob(ok_src)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/contents/data/huge.sql":
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "4999"},
                json={
                    "message": "This API returns blobs up to 1 MB in size.",
                    "errors": [{"resource": "Blob", "field": "data", "code": "too_large"}],
                },
            )
        if request.url.path.endswith(f"/git/blobs/{ok_sha}"):
            return httpx.Response(200, json=_blob_json(ok_src, sha=ok_sha))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        report = await fetcher.fetch_slices(
            [
                BlobSliceRequest("acme/app", "data/huge.sql", 1, 10, ref="head-sha"),
                BlobSliceRequest("acme/app", "src/small.py", 1, 2, blob_sha=ok_sha),
            ]
        )

    assert [s.status for s in report.slices] == ["oversize", "ok"]
    assert report.slices[1].content == "def small():\n    return 1"
    assert report.skipped_blobs == 1


@pytest.mark.asyncio
async def test_blob_fetcher_preserves_ref_isolation_and_rejects_past_eof_start_line() -> None:
    """[F-03] prefer_in_memory=False fetches target ref; start_line > total returns not_found."""
    head_src = "line_1_head\nline_2_head\n"
    main_src = b"line_1_main\nline_2_main\nline_3_main\n"
    main_sha = _sha1_blob(main_src)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/repos/acme/app/contents/src/service.py":
            assert request.url.params.get("ref") == "main"
            return httpx.Response(
                200, json=_contents_json("src/service.py", main_src, sha=main_sha)
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        report = await fetcher.fetch_slices(
            [
                # Explicit ref="main" with prefer_in_memory=False MUST NOT be shadowed by head_src
                BlobSliceRequest(
                    "acme/app",
                    "src/service.py",
                    1,
                    2,
                    ref="main",
                    prefer_in_memory=False,
                ),
                # Past-EOF start_line=50 on a 3-line file MUST return not_found, never line 3
                BlobSliceRequest(
                    "acme/app",
                    "src/service.py",
                    50,
                    60,
                    ref="main",
                    prefer_in_memory=False,
                ),
            ],
            in_memory_sources={"src/service.py": head_src},
        )

    assert len(calls) == 1
    assert report.slices[0].status == "ok"
    assert report.slices[0].content == "line_1_main\nline_2_main"
    assert report.slices[1].status == "not_found"
    assert report.slices[1].content == ""
    assert report.slices[1].start_line == 0


@pytest.mark.asyncio
async def test_blob_fetcher_deduplicates_concurrent_401_token_invalidation() -> None:
    """[F-06] Six concurrent 401 responses invalidate & refresh installation token strictly ONCE."""
    blobs = {f"src/f{i}.py": f"val_{i} = {i}\n".encode() for i in range(6)}
    shas = {path: _sha1_blob(content) for path, content in blobs.items()}
    by_sha = {shas[path]: content for path, content in blobs.items()}

    invalidated: list[int] = []
    auth_calls = 0

    class _ConcurrentAuth(GitHubAuthProvider):
        def __init__(self) -> None:
            super().__init__(Settings(github_token=SecretStr("tok-1")))
            self._version = 1

        async def get_auth_headers(
            self,
            *,
            installation_id: int | None = None,
            repo_full_name: str | None = None,
        ) -> dict[str, str]:
            nonlocal auth_calls
            auth_calls += 1
            return {"Authorization": f"Bearer tok-{self._version}"}

        async def invalidate_installation_token(self, installation_id: int) -> None:
            invalidated.append(installation_id)
            self._version += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.015)
        if request.headers.get("Authorization") == "Bearer tok-1":
            return httpx.Response(401)
        sha = request.url.path.rsplit("/", 1)[-1]
        if sha in by_sha:
            return httpx.Response(200, json=_blob_json(by_sha[sha], sha=sha))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_ConcurrentAuth(), http_client=client)
        report = await fetcher.fetch_slices(
            [BlobSliceRequest("acme/app", path, 1, 1, blob_sha=sha) for path, sha in shas.items()],
            installation_id=77,
        )

    assert all(s.status == "ok" for s in report.slices)
    assert invalidated == [77]
    assert auth_calls == 2  # initial fetch + exactly 1 shared refresh


@pytest.mark.asyncio
async def test_retriever_runs_3_qdrant_branches_concurrently_and_forwards_repo_id() -> None:
    """[F-10] retrieve() runs Callers, Callees & Docs concurrently (max_in_flight==3) + repo_id."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.rag.retriever import HybridContextRetriever

    in_flight = 0
    max_in_flight = 0
    seen_repo_ids: list[str | None] = []
    seen_limits: list[int] = []

    class _ConcurrentTrackingStore(_StubQdrantStore):
        async def find_by_called_symbols(
            self,
            called_symbols: list[str],
            *,
            repo_full_name: str | None = None,
            repo_id: str | None = None,
            limit: int = 50,
        ) -> tuple[Any, ...]:
            nonlocal in_flight, max_in_flight
            seen_repo_ids.append(repo_id)
            seen_limits.append(limit)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.03)
                return ()
            finally:
                in_flight -= 1

        async def find_by_symbol_names(
            self,
            symbol_names: list[str],
            *,
            repo_full_name: str | None = None,
            repo_id: str | None = None,
            limit: int = 50,
        ) -> tuple[Any, ...]:
            nonlocal in_flight, max_in_flight
            seen_repo_ids.append(repo_id)
            seen_limits.append(limit)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.03)
                return ()
            finally:
                in_flight -= 1

        async def search_similar(
            self,
            query_vector: tuple[float, ...],
            *,
            repo_full_name: str | None = None,
            repo_id: str | None = None,
            chunk_type: str | None = None,
            score_threshold: float | None = None,
            limit: int = 10,
        ) -> tuple[Any, ...]:
            seen_repo_ids.append(repo_id)
            return ()

    class _SlowEmbedder(_StubEmbedder):
        async def embed_query(self, query_text: str) -> tuple[float, ...]:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.03)
                return await super().embed_query(query_text)
            finally:
                in_flight -= 1

    chunk = CodeSymbolChunk(
        file_path="src/payments.py",
        language="python",
        symbol_name="charge_card",
        qualified_name="charge_card",
        symbol_kind="function",
        start_line=1,
        end_line=10,
        signature="def charge_card()",
        called_symbols=("debit_ledger",),
    )
    ctx = PRFileContext(
        file_path="src/payments.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/payments.py",
            language="python",
            imports=(),
            chunks=(chunk,),
        ),
        touched_chunks=(chunk,),
    )

    retriever = HybridContextRetriever(
        store=_ConcurrentTrackingStore(),  # type: ignore[arg-type]
        embedder=_SlowEmbedder(),  # type: ignore[arg-type]
    )
    await retriever.retrieve([ctx], repo_full_name="acme/app", repo_id="repo-uuid-123")

    assert max_in_flight == 3
    assert seen_repo_ids == ["repo-uuid-123", "repo-uuid-123", "repo-uuid-123"]
    # [F-08] Scroll window covers at least 1 full Qdrant page (256) to prevent hub starvation
    assert all(lim >= 256 for lim in seen_limits)


@pytest.mark.asyncio
async def test_retriever_handles_deleted_renamed_oversize_hunk_module_edit_and_same_file_ast() -> (
    None
):
    """[F-04/F-11] Deleted/renamed isolation, hunk_fallback recovery, module edit & live AST."""
    from codesight.ast_engine.registry import ASTParserRegistry
    from codesight.diff.models import PatchFileEntry
    from codesight.diff.smart_diff_parser import SmartDiffParser
    from codesight.rag.retriever import HybridContextRetriever
    from codesight.rag.token_packer import TokenBudgetPacker

    registry = ASTParserRegistry()

    # 1. Modified Python class file (verify_session modified; sibling refresh calls it)
    auth_src = (
        "class AuthService:\n"
        "    def verify_session(self, raw: str) -> bool:\n"
        "        return bool(raw.strip())\n\n"
        "    def refresh_session(self, raw: str) -> bool:\n"
        "        return self.verify_session(raw)\n\n"
        "def standalone_caller(svc: AuthService, raw: str) -> bool:\n"
        "    return svc.verify_session(raw)\n"
        + ("\n# padding line for realistic token sizing " + "p" * 40) * 60
        + "\n"
    ).encode("utf-8")

    # 2. Module-level Python edit outside any function/class
    settings_src = (
        "DEFAULT_TIMEOUT_SECONDS = 45\n"
        "MAX_RETRIES = 3\n" + ("\n# settings docs padding " + "s" * 40) * 60 + "\n"
    ).encode("utf-8")

    # Parse PR files (deleted zero-SHA file, renamed file, oversize yaml hunk_fallback)
    entries = [
        PatchFileEntry(
            filename="src/new_auth.py",
            previous_filename="src/old_auth.py",
            status="renamed",
            blob_sha="a" * 40,
            patch=(
                "@@ -1,4 +1,4 @@ class AuthService\n"
                " class AuthService:\n"
                "     def verify_session(self, raw: str) -> bool:\n"
                "-        return bool(raw)\n"
                "+        return bool(raw.strip())\n"
            ),
        ),
        PatchFileEntry(
            filename="src/settings.py",
            status="modified",
            blob_sha="b" * 40,
            patch=(
                "@@ -1,2 +1,2 @@\n"
                "-DEFAULT_TIMEOUT_SECONDS = 30\n"
                "+DEFAULT_TIMEOUT_SECONDS = 45\n"
                " MAX_RETRIES = 3\n"
            ),
        ),
        PatchFileEntry(
            filename="src/removed.py",
            status="removed",
            blob_sha="0" * 40,
            patch="@@ -1,2 +0,0 @@\n-def gone():\n-    return 0\n",
        ),
        PatchFileEntry(
            filename="deploy/huge.yaml",
            status="modified",
            blob_sha=None,
            patch="@@ -10,2 +10,3 @@ timeout_cfg\n timeout: 30\n+retries: 5\n enabled: true\n",
        ),
    ]
    parsed_diff = SmartDiffParser().parse_patch_entries(entries)
    by_path = {f.file_path: f for f in parsed_diff.files}

    ctxs = [
        registry.resolve_pr_file(by_path["src/new_auth.py"], after_source=auth_src),
        registry.resolve_pr_file(by_path["src/settings.py"], after_source=settings_src),
        registry.resolve_pr_file(by_path["src/removed.py"], after_source=None),
        registry.resolve_pr_file(by_path["deploy/huge.yaml"], after_source=None),
    ]

    # Qdrant returns: stale point on deleted file, stale point on old renamed path,
    # stale main_sha point on modified src/new_auth.py, and valid external caller importing old_auth
    ext_caller_src = (
        "from src.old_auth import AuthService\n"
        "def ext_route(svc, tok):\n"
        "    return svc.verify_session(tok)\n" + ("\n# router filler " + "r" * 40) * 50 + "\n"
    ).encode("utf-8")
    ext_sha = _sha1_blob(ext_caller_src)

    stale_deleted_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000091",
        file_path="src/removed.py",
        blob_sha="9" * 40,
        symbol_name="gone",
        called_symbols=["verify_session"],
    )
    stale_old_rename_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000092",
        file_path="src/old_auth.py",
        blob_sha="8" * 40,
        symbol_name="old_caller",
        called_symbols=["verify_session"],
    )
    stale_same_file_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000093",
        file_path="src/new_auth.py",
        blob_sha="7" * 40,
        symbol_name="stale_main_sibling",
        called_symbols=["verify_session"],
    )
    ext_caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000094",
        file_path="src/ext_router.py",
        blob_sha=ext_sha,
        symbol_name="ext_route",
        start_line=2,
        end_line=3,
        called_symbols=["verify_session"],
        imports=["src.old_auth.AuthService"],
    )

    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith(f"/git/blobs/{ext_sha}"):
            return httpx.Response(200, json=_blob_json(ext_caller_src, sha=ext_sha))
        if request.url.path == "/repos/acme/app/contents/deploy/huge.yaml":
            return httpx.Response(
                403,
                json={"errors": [{"resource": "Blob", "field": "data", "code": "too_large"}]},
            )
        return httpx.Response(404)

    store = _StubQdrantStore(
        callers=(stale_deleted_pt, stale_old_rename_pt, stale_same_file_pt, ext_caller_pt)
    )
    retriever = HybridContextRetriever(store=store, embedder=None)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        assembled = await retriever.retrieve_and_pack(
            ctxs,
            repo_full_name="acme/app",
            blob_fetcher=fetcher,
            packer=TokenBudgetPacker(budget_tokens=3500),
            parsed_diff=parsed_diff,
            head_ref="pr-head-sha",
            in_memory_sources={
                "src/new_auth.py": auth_src,
                "src/settings.py": settings_src,
            },
        )

    # 1. Zero HTTP calls for deleted src/removed.py, zero SHA, or stale main_sha on src/new_auth.py
    assert not any("removed.py" in p or ("0" * 40) in p or ("7" * 40) in p for p in requested_paths)

    # 2. Deleted/renamed-old/stale-main Qdrant callers excluded; live same-file + ext_route kept
    caller_names = {c.symbol_name for c in assembled.retrieval.callers}
    assert "gone" not in caller_names
    assert "old_caller" not in caller_names
    assert "stale_main_sibling" not in caller_names
    assert "ext_route" in caller_names
    assert "standalone_caller" in caller_names

    # 3. Module-level edit src/settings.py produced enclosing slice & non-zero baseline tokens
    assert "DEFAULT_TIMEOUT_SECONDS = 45" in assembled.packed_bundle.rendered_context
    assert assembled.packed_bundle.metrics.tokens_saved_pct >= 45.0

    # 4. Oversize hunk_fallback deploy/huge.yaml recovered inline diff text instead of dropping it
    assert "L011 | + retries: 5" in assembled.packed_bundle.rendered_context

    # 5. Pure single-file module-level edit achieves > 45% savings (never 0.0%)
    single_mod_diff = SmartDiffParser().parse_patch_entries([entries[1]])
    single_mod_ctx = registry.resolve_pr_file(single_mod_diff.files[0], after_source=settings_src)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = GitHubBlobFetcher(_make_auth(), http_client=client)
        mod_assembled = await retriever.retrieve_and_pack(
            [single_mod_ctx],
            repo_full_name="acme/app",
            blob_fetcher=fetcher,
            parsed_diff=single_mod_diff,
            head_ref="pr-head-sha",
            in_memory_sources={"src/settings.py": settings_src},
        )
    assert mod_assembled.packed_bundle.metrics.baseline_tokens > 0
    assert mod_assembled.packed_bundle.metrics.tokens_saved_pct >= 45.0


@pytest.mark.asyncio
async def test_retriever_finds_callers_for_renamed_and_deleted_symbols_from_complexity_deltas() -> (
    None
):
    """[CA-16] Signature rename & pure deletion query Qdrant/local callers via removed deltas."""
    from codesight.ast_engine.registry import ASTParserRegistry
    from codesight.diff.smart_diff_parser import SmartDiffParser
    from codesight.rag.retriever import HybridContextRetriever

    registry = ASTParserRegistry()

    # Case 1: Service function renamed (`calculate_vat_breakdown` -> `compute_regional_vat`),
    # plus a sibling untouched function in the same file still calling the old symbol name.
    before_rename = (
        b"from src.tax.rate_table import lookup_country_vat_rate\n\n"
        b"def calculate_vat_breakdown(subtotal: int, country: str) -> int:\n"
        b"    rate = lookup_country_vat_rate(country)\n"
        b"    return int(subtotal * rate)\n\n"
        b"def preview_local_vat(subtotal: int) -> int:\n"
        b"    return calculate_vat_breakdown(subtotal, 'VN')\n"
    )
    after_rename = (
        b"from src.tax.rate_table import lookup_country_vat_rate\n\n"
        b"def compute_regional_vat(subtotal: int, country: str, include_duty: bool) -> int:\n"
        b"    rate = lookup_country_vat_rate(country)\n"
        b"    return int(subtotal * rate)\n\n"
        b"def preview_local_vat(subtotal: int) -> int:\n"
        b"    return calculate_vat_breakdown(subtotal, 'VN')\n"
    )
    rename_diff_text = (
        "diff --git a/src/services/tax_service.py b/src/services/tax_service.py\n"
        "--- a/src/services/tax_service.py\n"
        "+++ b/src/services/tax_service.py\n"
        "@@ -3,3 +3,3 @@ from src.tax.rate_table import lookup_country_vat_rate\n"
        "-def calculate_vat_breakdown(subtotal: int, country: str) -> int:\n"
        "+def compute_regional_vat(subtotal: int, country: str, include_duty: bool) -> int:\n"
        "     rate = lookup_country_vat_rate(country)\n"
        "     return int(subtotal * rate)\n"
    )
    rename_diff = SmartDiffParser().parse_diff(rename_diff_text)
    rename_ctx = registry.resolve_pr_file(
        rename_diff.files[0],
        after_source=after_rename,
        before_source=before_rename,
    )

    caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000101",
        file_path="src/api/checkout_router.py",
        blob_sha="c" * 40,
        symbol_name="submit_checkout_endpoint",
        called_symbols=["calculate_vat_breakdown"],
        imports=["src.services.tax_service.calculate_vat_breakdown"],
    )
    doc_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000102",
        file_path="docs/adr/0003-regional-vat.md",
        blob_sha="d" * 40,
        symbol_name="Regional VAT Rules",
        symbol_kind="section",
        chunk_type="architecture_doc",
        language="markdown",
        score=0.84,
    )
    store = _StubQdrantStore(callers=(caller_pt,), docs=(doc_pt,))
    embedder = _StubEmbedder()
    retriever = HybridContextRetriever(store=store, embedder=embedder)  # type: ignore[arg-type]

    res = await retriever.retrieve(
        [rename_ctx],
        repo_full_name="acme/app",
        parsed_diff=rename_diff,
        head_ref="pr-rename-sha",
    )

    assert "compute_regional_vat" in store.called_symbols_queries[0]
    assert "calculate_vat_breakdown" in store.called_symbols_queries[0]
    caller_names = {c.symbol_name for c in res.callers}
    assert "submit_checkout_endpoint" in caller_names
    assert "preview_local_vat" in caller_names
    assert res.query_text is not None
    assert "calculate_vat_breakdown" in res.query_text

    # Case 2: Pure function deletion (removed symbol + noise 'get'; 0 touched_chunks)
    before_delete = (
        b"def keep_unchanged(x: int) -> int:\n"
        b"    return x + 1\n\n"
        b"LEGACY_FLAG = True\n\n"
        b"def legacy_sync_helper(order_id: str) -> str:\n"
        b"    return order_id.strip()\n\n"
        b"def get(key: str) -> str:\n"
        b"    return key\n"
    )
    after_delete = b"def keep_unchanged(x: int) -> int:\n    return x + 1\n\nLEGACY_FLAG = True\n"
    delete_diff_text = (
        "diff --git a/src/services/legacy_service.py b/src/services/legacy_service.py\n"
        "--- a/src/services/legacy_service.py\n"
        "+++ b/src/services/legacy_service.py\n"
        "@@ -4,7 +4,1 @@ LEGACY_FLAG = True\n"
        " LEGACY_FLAG = True\n"
        "-\n"
        "-def legacy_sync_helper(order_id: str) -> str:\n"
        "-    return order_id.strip()\n"
        "-\n"
        "-def get(key: str) -> str:\n"
        "-    return key\n"
    )
    delete_diff = SmartDiffParser().parse_diff(delete_diff_text)
    delete_ctx = registry.resolve_pr_file(
        delete_diff.files[0],
        after_source=after_delete,
        before_source=before_delete,
    )
    assert delete_ctx.touched_chunks == ()

    del_caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000103",
        file_path="src/api/admin_router.py",
        blob_sha="e" * 40,
        symbol_name="trigger_legacy_sync",
        called_symbols=["legacy_sync_helper"],
        imports=["src.services.legacy_service.legacy_sync_helper"],
    )
    del_store = _StubQdrantStore(callers=(del_caller_pt,), docs=(doc_pt,))
    del_embedder = _StubEmbedder()
    del_retriever = HybridContextRetriever(
        store=del_store,  # type: ignore[arg-type]
        embedder=del_embedder,  # type: ignore[arg-type]
    )

    del_res = await del_retriever.retrieve(
        [delete_ctx],
        repo_full_name="acme/app",
        parsed_diff=delete_diff,
        head_ref="pr-del-sha",
    )

    assert del_store.called_symbols_queries == [["legacy_sync_helper"], ["get"]]
    assert [c.symbol_name for c in del_res.callers] == ["trigger_legacy_sync"]
    assert del_res.query_text is not None
    assert "legacy_sync_helper" in del_res.query_text


@pytest.mark.asyncio
async def test_retriever_finds_callers_for_renamed_classes_schemas_and_ts_interfaces() -> None:
    """[CA-16/F-01] Class/schema/interface renames find callers without false-removing methods."""
    from codesight.ast_engine.models import ComplexityDelta, ComplexityDiffSummary, PRFileContext
    from codesight.ast_engine.registry import ASTParserRegistry
    from codesight.diff.smart_diff_parser import SmartDiffParser
    from codesight.rag.retriever import HybridContextRetriever, _extract_removed_symbol_names

    registry = ASTParserRegistry()

    # Case 1: Python class renamed while its method `execute_order` remains untouched
    before_py_cls = (
        b"class OrderProcessor:\n"
        b"    def execute_order(self, order_id: str) -> bool:\n"
        b"        return bool(order_id)\n\n"
        b"class OrderCreateRequest:\n"
        b"    def __init__(self, order_id: str) -> None:\n"
        b"        self.order_id = order_id\n"
    )
    after_py_cls = (
        b"class AsyncOrderProcessor:\n"
        b"    def execute_order(self, order_id: str) -> bool:\n"
        b"        return bool(order_id)\n\n"
        b"class CreateOrderPayload:\n"
        b"    def __init__(self, order_id: str) -> None:\n"
        b"        self.order_id = order_id\n"
    )
    py_cls_diff = SmartDiffParser().parse_diff(
        "diff --git a/src/services/order_processor.py b/src/services/order_processor.py\n"
        "--- a/src/services/order_processor.py\n"
        "+++ b/src/services/order_processor.py\n"
        "@@ -1,6 +1,6 @@\n"
        "-class OrderProcessor:\n"
        "+class AsyncOrderProcessor:\n"
        "     def execute_order(self, order_id: str) -> bool:\n"
        "         return bool(order_id)\n"
        " \n"
        "-class OrderCreateRequest:\n"
        "+class CreateOrderPayload:\n"
        "     def __init__(self, order_id: str) -> None:\n"
    )
    py_cls_ctx = registry.resolve_pr_file(
        py_cls_diff.files[0],
        after_source=after_py_cls,
        before_source=before_py_cls,
    )
    assert py_cls_ctx.removed_symbols == ("OrderProcessor", "OrderCreateRequest")
    assert "execute_order" not in py_cls_ctx.removed_symbols

    cls_caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000104",
        file_path="src/api/orders_router.py",
        blob_sha="f" * 40,
        symbol_name="create_order_endpoint",
        called_symbols=["OrderProcessor", "OrderCreateRequest"],
        imports=["src.services.order_processor.OrderProcessor"],
    )
    cls_store = _StubQdrantStore(callers=(cls_caller_pt,))
    cls_retriever = HybridContextRetriever(store=cls_store, embedder=_StubEmbedder())  # type: ignore[arg-type]
    cls_res = await cls_retriever.retrieve(
        [py_cls_ctx],
        repo_full_name="acme/app",
        parsed_diff=py_cls_diff,
        head_ref="pr-cls-sha",
    )
    assert "OrderProcessor" in cls_store.called_symbols_queries[0]
    assert "OrderCreateRequest" in cls_store.called_symbols_queries[0]
    assert "execute_order" not in cls_store.called_symbols_queries[0]
    assert [c.symbol_name for c in cls_res.callers] == ["create_order_endpoint"]
    assert cls_res.query_text is not None
    assert "removed OrderProcessor" in cls_res.query_text
    assert "removed execute_order" not in cls_res.query_text

    # Case 2: TypeScript interface + class renamed while method `submitOrder` stays untouched
    before_ts_cls = (
        b"export interface CartSummary {\n"
        b"  cartId: string;\n"
        b"}\n\n"
        b"export class CheckoutCoordinator {\n"
        b"  submitOrder(id: string): boolean {\n"
        b"    return Boolean(id);\n"
        b"  }\n"
        b"}\n"
    )
    after_ts_cls = (
        b"export interface VerifiedCartSummary {\n"
        b"  cartId: string;\n"
        b"}\n\n"
        b"export class AsyncCheckoutCoordinator {\n"
        b"  submitOrder(id: string): boolean {\n"
        b"    return Boolean(id);\n"
        b"  }\n"
        b"}\n"
    )
    ts_cls_diff = SmartDiffParser().parse_diff(
        "diff --git a/web/src/checkoutCoordinator.ts b/web/src/checkoutCoordinator.ts\n"
        "--- a/web/src/checkoutCoordinator.ts\n"
        "+++ b/web/src/checkoutCoordinator.ts\n"
        "@@ -1,6 +1,6 @@\n"
        "-export interface CartSummary {\n"
        "+export interface VerifiedCartSummary {\n"
        "   cartId: string;\n"
        " }\n"
        " \n"
        "-export class CheckoutCoordinator {\n"
        "+export class AsyncCheckoutCoordinator {\n"
        "   submitOrder(id: string): boolean {\n"
    )
    ts_cls_ctx = registry.resolve_pr_file(
        ts_cls_diff.files[0],
        after_source=after_ts_cls,
        before_source=before_ts_cls,
    )
    assert ts_cls_ctx.removed_symbols == ("CartSummary", "CheckoutCoordinator")
    assert "submitOrder" not in ts_cls_ctx.removed_symbols

    # Case 3: Surviving parent class removes a method while sibling class keeps same method name
    before_method_del = (
        b"class PrimaryWorker:\n"
        b"    def run_job(self) -> int:\n"
        b"        return 1\n\n"
        b"class BackupWorker:\n"
        b"    def run_job(self) -> int:\n"
        b"        return 2\n"
    )
    after_method_del = (
        b"class PrimaryWorker:\n"
        b"    def execute_job(self) -> int:\n"
        b"        return 1\n\n"
        b"class BackupWorker:\n"
        b"    def run_job(self) -> int:\n"
        b"        return 2\n"
    )
    method_del_diff = SmartDiffParser().parse_diff(
        "diff --git a/src/workers/jobs.py b/src/workers/jobs.py\n"
        "--- a/src/workers/jobs.py\n"
        "+++ b/src/workers/jobs.py\n"
        "@@ -1,3 +1,3 @@\n"
        " class PrimaryWorker:\n"
        "-    def run_job(self) -> int:\n"
        "+    def execute_job(self) -> int:\n"
        "         return 1\n"
    )
    method_del_ctx = registry.resolve_pr_file(
        method_del_diff.files[0],
        after_source=after_method_del,
        before_source=before_method_del,
    )
    assert method_del_ctx.removed_symbols == ("run_job",)

    # Case 4: Manual PRFileContext stub without parsed_file still falls back to complexity deltas
    manual_stub = PRFileContext(
        file_path="src/manual.py",
        mode="ast",
        parsed_file=None,
        complexity=ComplexityDiffSummary(
            deltas=(
                ComplexityDelta(
                    symbol_name="manual_removed_fn",
                    qualified_name="manual_removed_fn",
                    change_kind="removed",
                    before_cc=2,
                    after_cc=0,
                    delta_cc=-2,
                ),
            ),
            net_delta_cc=-2,
            max_delta_cc=0,
            max_after_cc=0,
        ),
    )
    assert _extract_removed_symbol_names(manual_stub) == ("manual_removed_fn",)


@pytest.mark.asyncio
async def test_retriever_doc_degrades_on_config_value_and_grpc_errors_raises_zerocode() -> None:
    """[M3] Doc search isolates EmbeddingConfigError/ValueError/RpcError/RuntimeError."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.rag.models import EmbeddingConfigError, ZeroCodeViolationError
    from codesight.rag.retriever import HybridContextRetriever

    chunk = CodeSymbolChunk(
        file_path="src/orders.py",
        language="python",
        symbol_name="submit_order",
        qualified_name="submit_order",
        symbol_kind="function",
        start_line=1,
        end_line=8,
        signature="def submit_order(order_id: str)",
        called_symbols=("charge_wallet",),
    )
    pr_ctx = PRFileContext(
        file_path="src/orders.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/orders.py",
            language="python",
            imports=(),
            chunks=(chunk,),
        ),
        touched_chunks=(chunk,),
    )
    caller_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000201",
        file_path="src/routes/orders.py",
        blob_sha="8" * 40,
        symbol_name="post_order",
        called_symbols=["submit_order"],
    )
    callee_pt = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000202",
        file_path="src/wallet.py",
        blob_sha="9" * 40,
        symbol_name="charge_wallet",
    )

    for exc in (EmbeddingConfigError("missing API key"), ValueError("bad query dimension")):
        store = _StubQdrantStore(callers=(caller_pt,), callees=(callee_pt,))
        retriever = HybridContextRetriever(
            store=store,  # type: ignore[arg-type]
            embedder=_StubEmbedder(fail_exc=exc),  # type: ignore[arg-type]
        )
        res = await retriever.retrieve([pr_ctx], repo_full_name="acme/app")
        assert res.doc_search_degraded is True
        assert res.architecture_docs == ()
        assert [c.symbol_name for c in res.callers] == ["post_order"]
        assert [c.symbol_name for c in res.callees] == ["charge_wallet"]

    class _FakeRpcError(RuntimeError):
        pass

    class _FailingSearchStore(_StubQdrantStore):
        async def search_similar(
            self,
            query_vector: tuple[float, ...],
            *,
            repo_full_name: str | None = None,
            repo_id: str | None = None,
            chunk_type: str | None = None,
            score_threshold: float | None = None,
            limit: int = 10,
        ) -> tuple[Any, ...]:
            raise _FakeRpcError("qdrant grpc unavailable")

    retriever_rpc = HybridContextRetriever(
        store=_FailingSearchStore(callers=(caller_pt,), callees=(callee_pt,)),  # type: ignore[arg-type]
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
    )
    res_rpc = await retriever_rpc.retrieve([pr_ctx], repo_full_name="acme/app")
    assert res_rpc.doc_search_degraded is True
    assert res_rpc.architecture_docs == ()
    assert [c.symbol_name for c in res_rpc.callers] == ["post_order"]
    assert [c.symbol_name for c in res_rpc.callees] == ["charge_wallet"]

    retriever_zc = HybridContextRetriever(
        store=_StubQdrantStore(callers=(caller_pt,)),  # type: ignore[arg-type]
        embedder=_StubEmbedder(fail_exc=ZeroCodeViolationError("payload leak")),  # type: ignore[arg-type]
    )
    with pytest.raises(ZeroCodeViolationError, match="payload leak"):
        await retriever_zc.retrieve([pr_ctx], repo_full_name="acme/app")


@pytest.mark.asyncio
async def test_retriever_separates_ambiguous_symbols_and_applies_affinity_gate() -> None:
    """[N3/M5] Builtins stay dropped, ambiguous CRUD runs separately with affinity gate."""
    from codesight.ast_engine.models import CodeSymbolChunk, ParsedASTFile, PRFileContext
    from codesight.rag.retriever import (
        AMBIGUOUS_METHOD_SYMBOLS,
        BUILTIN_NOISE_SYMBOLS,
        COMMON_NOISE_SYMBOLS,
        HybridContextRetriever,
    )

    assert BUILTIN_NOISE_SYMBOLS & AMBIGUOUS_METHOD_SYMBOLS == frozenset()
    assert {"len", "print", "console", "Math", "JSON", "Promise"} <= BUILTIN_NOISE_SYMBOLS
    assert {"update", "save", "get", "delete", "fetch"} <= AMBIGUOUS_METHOD_SYMBOLS
    assert COMMON_NOISE_SYMBOLS == BUILTIN_NOISE_SYMBOLS

    chunk = CodeSymbolChunk(
        file_path="src/repos/user_repository.py",
        language="python",
        symbol_name="update",
        qualified_name="UserRepository.update",
        symbol_kind="method",
        start_line=5,
        end_line=10,
        signature="def update(self, user: dict) -> dict",
        called_symbols=("validate", "UserRepository.validate", "len"),
    )
    pr_ctx = PRFileContext(
        file_path="src/repos/user_repository.py",
        mode="ast",
        language="python",
        parsed_file=ParsedASTFile(
            file_path="src/repos/user_repository.py",
            language="python",
            imports=(),
            chunks=(chunk,),
        ),
        touched_chunks=(chunk,),
    )
    router_caller = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000301",
        file_path="src/routers/user_router.py",
        blob_sha="a" * 40,
        symbol_name="patch_user",
        called_symbols=["update", "repo.update"],
        imports=["src.repos.user_repository.UserRepository"],
    )
    cron_unrelated = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000302",
        file_path="src/jobs/cron_job.py",
        blob_sha="b" * 40,
        symbol_name="run_cleanup",
        called_symbols=["update", "cache.update"],
        imports=["src.cache.redis_cache.RedisCache"],
    )
    snake_caller = _make_qdrant_point(
        point_id="00000000-0000-5000-8000-000000000303",
        file_path="src/handlers/di_handler.py",
        blob_sha="c" * 40,
        symbol_name="sync_user_di",
        called_symbols=["update", "user_repository.update"],
        imports=[],
    )
    store = _StubQdrantStore(callers=(router_caller, cron_unrelated, snake_caller))
    retriever = HybridContextRetriever(store=store, embedder=_StubEmbedder())  # type: ignore[arg-type]

    res = await retriever.retrieve([pr_ctx], repo_full_name="acme/app")

    assert store.called_symbols_queries == [["UserRepository.update"], ["update"]]
    assert [c.symbol_name for c in res.callers] == ["patch_user", "sync_user_di"]
