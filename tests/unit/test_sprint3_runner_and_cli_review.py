"""Unit tests for Sprint3PRReviewRunner, self-review 422 fallback, DB persistence & CLI (CA-21)."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from typing import Any

import httpx
import pytest
import redis.exceptions
from pydantic import SecretStr
from typer.testing import CliRunner

from codesight.agents.llm_factory import LLMFactory, StructuredInvocationResult
from codesight.agents.schemas import (
    BatchReviewResult,
    CodeSuggestion,
    DiffBatch,
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
    PRReviewState,
    UnreviewableDiffSegment,
    VerifiedFinding,
)
from codesight.cli.main import app as cli_app
from codesight.config_reader.repo_config import RepoConfig
from codesight.core.config import Settings
from codesight.db.models import PRReview, ReviewFinding
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs.blob_fetcher import GitHubBlobFetcher, GitHubBlobFetchError
from codesight.vcs.github_auth import GitHubAuthProvider
from codesight.vcs.github_publisher import GitHubPublishError, GitHubReviewPublisher
from codesight.worker import ai_review_pipeline as pipeline_mod
from codesight.worker.celery_app import run_pr_webhook_task
from codesight.worker.pr_review_runner import Sprint3PRReviewRunner, Week1PRReviewRunner
from codesight.worker.sha_cache import (
    CachedFileReview,
    RedisBlobSHACache,
    compute_patch_fingerprint,
)


class _FakeAsyncRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}

    async def get(self, key: str) -> str | None:
        item = self.store.get(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ex: int = 0, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = (value, ex)
        return True

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store[k][0] if k in self.store else None for k in keys]

    def pipeline(self, *, transaction: bool = False) -> _FakePipeline:
        _ = transaction
        return _FakePipeline(self.store)

    async def eval(self, *_args: Any, **_kwargs: Any) -> int:
        return 1

    async def aclose(self) -> None:
        return None


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


class _FakeSavepoint:
    async def __aenter__(self) -> _FakeSavepoint:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _RecordingAsyncSession:
    def __init__(self, repo_uuid: uuid.UUID, *, repo_exists: bool = True) -> None:
        self.repo_uuid = repo_uuid
        self.repo_exists = repo_exists
        self.added: list[Any] = []
        self.committed = False
        self.executed: list[Any] = []

    async def __aenter__(self) -> _RecordingAsyncSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def execute(self, stmt: Any) -> _ScalarResult:
        self.executed.append(stmt)
        return _ScalarResult([])

    async def scalar(self, _stmt: Any) -> Any:
        return self.repo_uuid if self.repo_exists else None

    def add(self, obj: Any) -> None:
        from codesight.db.models import Repository

        if isinstance(obj, (PRReview, Repository)) and obj.id is None:
            obj.id = self.repo_uuid if isinstance(obj, Repository) else uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        from codesight.db.models import Repository

        for obj in self.added:
            if isinstance(obj, (PRReview, Repository)) and obj.id is None:
                obj.id = self.repo_uuid if isinstance(obj, Repository) else uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionFactory:
    def __init__(self, repo_uuid: uuid.UUID, *, repo_exists: bool = True) -> None:
        self.session = _RecordingAsyncSession(repo_uuid, repo_exists=repo_exists)

    def __call__(self) -> _RecordingAsyncSession:
        return self.session


class _StubLLMFactory(LLMFactory):
    def __init__(self, settings: Settings, *, emit_critical: bool = True) -> None:
        super().__init__(settings=settings, structured_invoker=self._invoke)
        self._emit_critical = emit_critical

    async def _invoke(
        self,
        role: Any,
        model_name: str,
        schema: type[Any],
        messages: list[Any],
        config: Any,
    ) -> StructuredInvocationResult[Any]:
        _ = messages, config, schema
        if role == "security_auditor" and self._emit_critical:
            parsed: Any = LLMAuditorDraftOutput(
                findings=[
                    LLMFindingDraft(
                        file_path="src/auth.py",
                        start_line=2,
                        end_line=2,
                        severity="CRITICAL",
                        category="hardcoded_secret",
                        title="Command injection risk",
                        evidence="os.system called with user_input on L2",
                        confidence=0.95,
                    )
                ]
            )
        elif role in {"security_auditor", "logic_performance_auditor"}:
            parsed = LLMAuditorDraftOutput(findings=[])
        else:
            user_text = str(messages[-1].content) if messages else ""
            cand_ids = re.findall(r"b\d{3}-sec-\d{3}", user_text)
            target_id = cand_ids[0] if cand_ids else "b000-sec-000"
            parsed = LLMCriticDraftOutput(
                verdicts=[
                    LLMCriticVerdictDraft(
                        candidate_id=target_id,
                        decision="APPROVE",
                        rationale="Verified shell command injection in src/auth.py.",
                        confidence=0.95,
                    )
                ]
                if self._emit_critical
                else []
            )
        return StructuredInvocationResult(
            parsed=parsed,
            model_name=model_name,
            input_tokens=120,
            output_tokens=40,
            total_tokens=160,
            estimated_cost_usd=0.0005,
            latency_ms=10.0,
            attempts=1,
        )


async def test_publisher_self_review_request_changes_422_falls_back_to_comment() -> None:
    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test"))
    parsed = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/auth.py",
                status="modified",
                patch="@@ -1 +1,2 @@\n def f():\n+    pass\n",
            )
        ]
    )

    # 1. Real GitHub wire shapes: errors: [str] and errors: [{"message": str}]
    for error_payload in (
        {
            "message": "Unprocessable Entity",
            "errors": ["Review Can not request changes on your own pull request"],
        },
        {
            "message": "Validation Failed",
            "errors": [
                {
                    "resource": "PullRequestReview",
                    "code": "custom",
                    "message": "Can not request changes on your own pull request",
                }
            ],
        },
    ):
        posted_events: list[str] = []

        def handler(
            request: httpx.Request,
            err: dict[str, Any] = error_payload,
            events: list[str] = posted_events,
        ) -> httpx.Response:
            if request.url.path == "/repos/acme/app/pulls/10/reviews":
                body = json.loads(request.content.decode("utf-8"))
                events.append(body["event"])
                if body["event"] == "REQUEST_CHANGES":
                    return httpx.Response(422, json=err)
                return httpx.Response(
                    200,
                    json={
                        "id": 777,
                        "state": "COMMENTED",
                        "html_url": "https://github.com/acme/app/pull/10#pullrequestreview-777",
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pub = GitHubReviewPublisher(
                GitHubAuthProvider(settings, http_client=client), http_client=client
            )
            rec = await pub.publish_review(
                "acme/app",
                10,
                head_sha="abc123",
                parsed_diff=parsed,
                summary_body="Gate failed",
                event="REQUEST_CHANGES",
            )
        assert posted_events == ["REQUEST_CHANGES", "COMMENT"]
        assert rec.review_id == 777
        assert rec.state == "COMMENTED"

    # 2. Non-self-review 422 on REQUEST_CHANGES must still raise GitHubPublishError(422)
    def generic_422_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/pulls/10/reviews":
            return httpx.Response(
                422,
                json={"message": "Unprocessable Entity", "errors": ["Commit sha does not exist"]},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(generic_422_handler)) as client:
        pub = GitHubReviewPublisher(
            GitHubAuthProvider(settings, http_client=client), http_client=client
        )
        with pytest.raises(GitHubPublishError) as exc_info:
            await pub.publish_review(
                "acme/app",
                10,
                head_sha="abc123",
                parsed_diff=parsed,
                summary_body="Gate failed",
                event="REQUEST_CHANGES",
            )
        assert exc_info.value.status_code == 422


async def test_sprint3_runner_and_webhook_dispatch_persist_debt_score_and_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_uuid = uuid.uuid4()
    session_factory = _FakeSessionFactory(repo_uuid)
    fake_redis = _FakeAsyncRedis()
    check_runs_patched: list[dict[str, Any]] = []
    review_posts: list[dict[str, Any]] = []

    cfg_b64 = base64.b64encode(b"min_debt_score: 70\nreview_action: REQUEST_CHANGES\n").decode(
        "ascii"
    )
    blob_b64 = base64.b64encode(
        b"def run(user_input):\n    os.system(user_input)\n    return True\n"
    ).decode("ascii")
    raw_diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        f"index {'1' * 40}..{'a' * 40} 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run(user_input):\n"
        "+    os.system(user_input)\n"
        "     return True\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/app/contents/.codesight.yaml":
            return httpx.Response(
                200, json={"type": "file", "size": 64, "encoding": "base64", "content": cfg_b64}
            )
        if path == "/repos/acme/app/check-runs" and request.method == "POST":
            return httpx.Response(
                201, json={"id": 99, "name": "CodeSight-AI Review", "status": "in_progress"}
            )
        if path == "/repos/acme/app/check-runs/99" and request.method == "PATCH":
            payload = json.loads(request.content.decode("utf-8"))
            check_runs_patched.append(payload)
            return httpx.Response(
                200, json={"id": 99, "status": "completed", "conclusion": payload["conclusion"]}
            )
        if path == "/repos/acme/app/pulls/15":
            return httpx.Response(200, text=raw_diff)
        if path.startswith("/repos/acme/app/git/blobs/") or path.startswith(
            "/repos/acme/app/contents/src/auth.py"
        ):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": "a" * 40,
                    "size": 64,
                    "encoding": "base64",
                    "content": blob_b64,
                },
            )
        if path == "/repos/acme/app/pulls/15/reviews":
            body = json.loads(request.content.decode("utf-8"))
            review_posts.append(body)
            # Simulate self-review 422 on REQUEST_CHANGES to confirm Check Run stays "failure"
            if body["event"] == "REQUEST_CHANGES":
                return httpx.Response(
                    422,
                    json={
                        "message": "Unprocessable Entity",
                        "errors": ["Review Can not request changes on your own pull request"],
                    },
                )
            return httpx.Response(200, json={"id": 501, "state": "COMMENTED"})
        return httpx.Response(404)

    # Include an INFO finding to verify INFO is excluded from inline comments[] but saved in DB
    class _CritAndInfoLLMFactory(LLMFactory):
        def __init__(self, cfg: Settings) -> None:
            super().__init__(settings=cfg, structured_invoker=self._invoke)

        async def _invoke(
            self,
            role: Any,
            model_name: str,
            schema: type[Any],
            messages: list[Any],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            _ = config, schema
            if role == "security_auditor":
                parsed: Any = LLMAuditorDraftOutput(
                    findings=[
                        LLMFindingDraft(
                            file_path="src/auth.py",
                            start_line=2,
                            end_line=2,
                            severity="CRITICAL",
                            category="command_injection",
                            title="Command injection risk",
                            evidence="os.system called with user_input on L2",
                            confidence=0.95,
                        )
                    ]
                )
            elif role == "logic_performance_auditor":
                parsed = LLMAuditorDraftOutput(
                    findings=[
                        LLMFindingDraft(
                            file_path="src/auth.py",
                            start_line=3,
                            end_line=3,
                            severity="INFO",
                            category="readability",
                            title="Minor style hint",
                            evidence="Explicit return True inside run(user_input) on L3",
                            confidence=0.80,
                            suggestion=LLMCodeSuggestionDraft(
                                start_line=3,
                                end_line=3,
                                replacement_code="    return False",
                                explanation="Minor note",
                            ),
                        )
                    ]
                )
            else:
                user_text = str(messages[-1].content) if messages else ""
                cand_ids = re.findall(r"b\d{3}-[a-z]+-\d{3}", user_text)
                verdicts = [
                    LLMCriticVerdictDraft(
                        candidate_id=cid,
                        decision="APPROVE",
                        rationale="Verified against AST and diff in src/auth.py.",
                        confidence=0.95 if "sec" in cid else 0.80,
                    )
                    for cid in cand_ids
                ]
                parsed = LLMCriticDraftOutput(verdicts=verdicts)
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=120,
                output_tokens=40,
                total_tokens=160,
                estimated_cost_usd=0.0005,
                latency_ms=10.0,
                attempts=1,
            )

    settings = Settings(
        _env_file=None,
        github_token=SecretStr("ghp_test"),
        gemini_api_key=SecretStr("fake-gemini-key"),
    )
    payload = {
        "delivery_id": "deliv-ca21-1",
        "repo_id": str(repo_uuid),
        "repo_full_name": "acme/app",
        "pr_number": 15,
        "head_sha": "deadbeef" * 5,
        "base_sha": "main",
        "default_branch": "main",
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        res = await run_pr_webhook_task(
            payload,
            settings=settings,
            http_client=http_client,
            session_factory=session_factory,  # type: ignore[arg-type]
            redis_client=fake_redis,  # type: ignore[arg-type]
            llm_factory=_CritAndInfoLLMFactory(settings),
        )

    assert res["status"] == "completed"
    assert [p["event"] for p in review_posts] == ["REQUEST_CHANGES", "COMMENT"]
    # Zero-Noise: only CRITICAL inline comment posted; INFO routed to <details> summary
    assert len(review_posts[1]["comments"]) == 1
    assert "Command injection risk" in review_posts[1]["comments"][0]["body"]
    assert "<details>" in review_posts[1]["body"]
    assert "Minor style hint" in review_posts[1]["body"]
    assert "Technical_Debt_Score-" in review_posts[1]["body"]
    assert len(check_runs_patched) == 1
    assert check_runs_patched[0]["conclusion"] == "failure"

    assert session_factory.session.committed is True
    pr_reviews = [x for x in session_factory.session.added if isinstance(x, PRReview)]
    findings = [x for x in session_factory.session.added if isinstance(x, ReviewFinding)]
    assert len(pr_reviews) == 1
    assert pr_reviews[0].debt_score is not None and 0 <= pr_reviews[0].debt_score <= 100
    assert pr_reviews[0].critical_count == 1
    assert pr_reviews[0].info_count == 1
    assert pr_reviews[0].latency_ms is not None and pr_reviews[0].latency_ms >= 0
    assert {f.severity for f in findings} == {"CRITICAL", "INFO"}

    # Verify E2E latency_ms passed to persist_pr_review_result overrides inner AI latency
    e2e_session = _FakeSessionFactory(repo_uuid)
    await pipeline_mod.persist_pr_review_result(
        e2e_session,  # type: ignore[arg-type]
        repo_full_name="acme/app",
        pr_number=15,
        head_sha="deadbeef" * 5,
        result=await pipeline_mod.execute_ai_pr_review(
            repo_full_name="acme/app",
            repo_id=str(repo_uuid),
            pr_number=15,
            head_sha="deadbeef" * 5,
            parsed_diff=SmartDiffParser().parse_diff(raw_diff),
            repo_config=RepoConfig(),
            settings=settings,
            trace_id="trace-latency",
            llm_factory=_StubLLMFactory(settings, emit_critical=False),
            in_memory_after_sources={"src/auth.py": "def run(user_input):\n    pass\n"},
        ),
        latency_ms=4321,
    )
    e2e_reviews = [x for x in e2e_session.session.added if isinstance(x, PRReview)]
    assert e2e_reviews[0].latency_ms == 4321

    # Without GEMINI_API_KEY and without llm_factory -> dispatches Week1PRReviewRunner
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    week1_settings = Settings(
        _env_file=None, github_token=SecretStr("ghp_test"), gemini_api_key=None
    )
    dispatched: list[str] = []

    async def _fake_week1_exec(self: Week1PRReviewRunner, _p: Any, **_kw: Any) -> dict[str, str]:
        _ = self
        dispatched.append("week1")
        return {"status": "completed", "trace_id": "w1"}

    async def _fail_sprint3_exec(
        self: Sprint3PRReviewRunner, _p: Any, **_kw: Any
    ) -> dict[str, str]:
        _ = self
        dispatched.append("sprint3")
        raise AssertionError("Sprint3PRReviewRunner must not run without GEMINI_API_KEY")

    monkeypatch.setattr(Week1PRReviewRunner, "execute", _fake_week1_exec)
    monkeypatch.setattr(Sprint3PRReviewRunner, "execute", _fail_sprint3_exec)
    w1_res = await run_pr_webhook_task(payload, settings=week1_settings)
    assert w1_res == {"status": "completed", "trace_id": "w1"}
    assert dispatched == ["week1"]


async def test_commit2_cached_suggestion_preserved_on_publish_and_truncated_batch_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeAsyncRedis()
    sha_cache = RedisBlobSHACache(fake_redis)  # type: ignore[arg-type]
    repo_cfg = RepoConfig(min_debt_score=70)
    raw_diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        f"index {'1' * 40}..{'a' * 40} 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run(user_input):\n"
        "+    os.system(user_input)\n"
        "     return True\n"
    )
    parsed_diff = SmartDiffParser().parse_diff(raw_diff)
    cached_finding = VerifiedFinding(
        candidate_id="b000-sec-001",
        role="security_auditor",
        batch_index=0,
        file_path="src/auth.py",
        start_line=2,
        end_line=2,
        severity="CRITICAL",
        category="command_injection",
        title="Command injection risk",
        evidence="os.system called on L2",
        confidence=0.95,
        rationale="Verified against AST and diff.",
        suggestion=CodeSuggestion(
            start_line=2,
            end_line=2,
            replacement_code="    subprocess.run([user_input], check=True)",
            explanation="Use subprocess.run safely.",
        ),
    )
    await sha_cache.set_many(
        repo_id="repo-1",
        config_hash=repo_cfg.config_hash,
        entries=[
            CachedFileReview(
                file_path="src/auth.py",
                blob_sha="a" * 40,
                config_hash=repo_cfg.config_hash,
                patch_fingerprint=compute_patch_fingerprint(parsed_diff.files[0]),
                findings=(cached_finding,),
            )
        ],
    )

    blob_b64 = base64.b64encode(
        b"def run(user_input):\n    os.system(user_input)\n    return True\n"
    ).decode("ascii")
    posted_reviews: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/acme/app/git/blobs/"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": "a" * 40,
                    "size": 64,
                    "encoding": "base64",
                    "content": blob_b64,
                },
            )
        if request.url.path == "/repos/acme/app/pulls/15/reviews":
            body = json.loads(request.content.decode("utf-8"))
            posted_reviews.append(body)
            return httpx.Response(200, json={"id": 601, "state": "COMMENTED"})
        return httpx.Response(404)

    settings = Settings(
        _env_file=None,
        github_token=SecretStr("ghp_test"),
        gemini_api_key=SecretStr("fake-gemini"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        auth = GitHubAuthProvider(settings, http_client=http_client)
        fetcher = GitHubBlobFetcher(auth, http_client=http_client)
        res = await pipeline_mod.execute_ai_pr_review(
            repo_full_name="acme/app",
            repo_id="repo-1",
            pr_number=15,
            head_sha="deadbeef" * 5,
            parsed_diff=parsed_diff,
            repo_config=repo_cfg,
            settings=settings,
            trace_id="trace-cached-sugg",
            blob_fetcher=fetcher,
            llm_factory=_StubLLMFactory(settings, emit_critical=False),
            sha_cache=sha_cache,
        )
        assert res.cache_hits == 1
        assert "src/auth.py" in res.after_sources_by_file

        pub = GitHubReviewPublisher(auth, http_client=http_client)
        pub_rec = await pub.publish_review(
            "acme/app",
            15,
            head_sha="deadbeef" * 5,
            parsed_diff=res.parsed_diff,
            drafts=res.inline_drafts,
            summary_body=res.summary_markdown,
            event="COMMENT",
            after_sources_by_file=res.after_sources_by_file,
        )
    assert pub_rec.inline_count == 1
    assert "```suggestion" in posted_reviews[0]["comments"][0]["body"]

    # Truncated batch & unreviewable segment must be excluded from SHA cache writes
    trunc_redis = _FakeAsyncRedis()
    trunc_cache = RedisBlobSHACache(trunc_redis)  # type: ignore[arg-type]

    async def _fake_truncated_graph(**_kwargs: Any) -> PRReviewState:
        return PRReviewState(
            pr_id="acme/app#15",
            head_sha="a" * 40,
            trace_id="trace-trunc",
            batches=[
                DiffBatch(
                    batch_index=0,
                    batch_id="b000",
                    file_paths=("src/auth.py",),
                    annotated_diff="@@ -1 +1 @@",
                    estimated_tokens=10,
                )
            ],
            batch_results=[
                BatchReviewResult(
                    batch_index=0,
                    batch_id="b000",
                    truncated_candidates_count=1,
                )
            ],
            unreviewable_segments=[
                UnreviewableDiffSegment(
                    file_path="src/auth.py",
                    hunk_id=1,
                    right_line=2,
                    estimated_tokens=5000,
                    reason="exceeds_batch_budget",
                )
            ],
            status="completed",
        )

    monkeypatch.setattr(pipeline_mod, "run_pr_review_graph", _fake_truncated_graph)
    await pipeline_mod.execute_ai_pr_review(
        repo_full_name="acme/app",
        repo_id="repo-1",
        pr_number=15,
        head_sha="deadbeef" * 5,
        parsed_diff=parsed_diff,
        repo_config=repo_cfg,
        settings=settings,
        trace_id="trace-trunc",
        llm_factory=_StubLLMFactory(settings),
        sha_cache=trunc_cache,
        in_memory_after_sources={"src/auth.py": "def run(user_input):\n    pass\n"},
    )
    assert len(trunc_redis.store) == 0


def test_cli_review_command_no_publish_offline_degradation_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codesight.cli.commands import review as review_mod

    runner = CliRunner()

    # 1. Missing GEMINI_API_KEY without --dry-run exits with code 2
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        review_mod,
        "get_settings",
        lambda: Settings(_env_file=None, gemini_api_key=None),
    )
    missing_key_res = runner.invoke(cli_app, ["review", "https://github.com/acme/app/pull/42"])
    assert missing_key_res.exit_code == 2

    # 2. Offline Redis/Qdrant/Postgres degrade softly, close Qdrant, & pass rag_repo_id=None
    settings = Settings(
        _env_file=None,
        gemini_api_key=SecretStr("fake-gemini-key"),
        github_token=None,
    )
    monkeypatch.setattr(review_mod, "get_settings", lambda: settings)

    raw_diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        f"index {'1' * 40}..{'a' * 40} 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1 +1,2 @@\n"
        " def f():\n"
        "+    return 1\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/app/pulls/42":
            if "vnd.github.diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=raw_diff)
            return httpx.Response(
                200,
                json={
                    "title": "Clean refactor",
                    "state": "open",
                    "base": {
                        "ref": "main",
                        "sha": "base123",
                        "repo": {"id": 999, "default_branch": "main"},
                    },
                    "head": {"ref": "feat/clean", "sha": "head123"},
                },
            )
        if request.url.path.startswith("/repos/acme/app/contents/src/auth.py"):
            blob_b64 = base64.b64encode(b"def f():\n    return 1\n").decode("ascii")
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": "a" * 40,
                    "size": 22,
                    "encoding": "base64",
                    "content": blob_b64,
                },
            )
        return httpx.Response(404)

    class _OfflineRedis:
        async def ping(self) -> None:
            raise redis.exceptions.ConnectionError("Redis offline")

        async def aclose(self) -> None:
            return None

    qdrant_closed = False

    class _OfflineQdrantClient:
        async def collection_exists(self, _name: str) -> bool:
            raise ConnectionError("Qdrant offline")

    class _OfflineQdrantStore:
        def __init__(self, **_kwargs: Any) -> None:
            self.client = _OfflineQdrantClient()

        async def aclose(self) -> None:
            nonlocal qdrant_closed
            qdrant_closed = True

    class _OfflineEngine:
        async def dispose(self) -> None:
            return None

    def _offline_session_factory(_engine: Any) -> Any:
        raise RuntimeError("Postgres offline")

    captured_rag_ids: list[str | None] = []
    orig_exec = review_mod.execute_ai_pr_review

    async def _spy_exec(*args: Any, **kwargs: Any) -> Any:
        captured_rag_ids.append(kwargs.get("rag_repo_id"))
        return await orig_exec(*args, **kwargs)

    monkeypatch.setattr(review_mod.redis_async, "from_url", lambda *_a, **_kw: _OfflineRedis())
    monkeypatch.setattr(review_mod, "QdrantZeroCodeStore", _OfflineQdrantStore)
    monkeypatch.setattr(review_mod, "create_db_engine", lambda _s: _OfflineEngine())
    monkeypatch.setattr(review_mod, "create_session_factory", _offline_session_factory)
    monkeypatch.setattr(review_mod, "execute_ai_pr_review", _spy_exec)

    async def _run_offline_cli() -> review_mod.CLIAIReviewBundle:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await review_mod.run_cli_ai_review(
                review_mod.parse_github_pr_url("https://github.com/acme/app/pull/42"),
                settings=settings,
                publish=False,
                http_client=http_client,
                llm_factory=_StubLLMFactory(settings, emit_critical=False),
            )

    bundle = asyncio.run(_run_offline_cli())
    assert qdrant_closed is True
    assert captured_rag_ids == [None]
    assert len(bundle.warnings) == 3
    assert any("Redis" in w for w in bundle.warnings)
    assert any("Qdrant" in w for w in bundle.warnings)
    assert any("PostgreSQL" in w for w in bundle.warnings)
    assert bundle.publish_skipped_reason == "skipped (--no-publish)"

    # 3. Verify CLI --no-publish flag forwarding and GitHubBlobFetchError exit code 1
    seen_publish_flags: list[bool | None] = []

    async def _fake_cli_entry(
        _pr_ref: Any,
        *,
        settings: Settings | None = None,
        publish: bool | None = None,
        **_kwargs: Any,
    ) -> review_mod.CLIAIReviewBundle:
        _ = settings
        seen_publish_flags.append(publish)
        return bundle

    monkeypatch.setattr(review_mod, "run_cli_ai_review", _fake_cli_entry)
    cli_res = runner.invoke(
        cli_app,
        ["review", "https://github.com/acme/app/pull/42", "--no-publish"],
    )
    assert cli_res.exit_code == 0
    assert seen_publish_flags == [False]
    assert "Debt Score: 100/100" in cli_res.stdout
    assert "Clean PR" in cli_res.stdout

    async def _raise_blob_err(*_args: Any, **_kwargs: Any) -> Any:
        raise GitHubBlobFetchError("Blob missing", status_code=422)

    monkeypatch.setattr(review_mod, "run_cli_ai_review", _raise_blob_err)
    err_res = runner.invoke(
        cli_app,
        ["review", "https://github.com/acme/app/pull/42", "--no-publish"],
    )
    assert err_res.exit_code == 1
    assert "PR review failed" in err_res.stderr
