"""Unit tests for CA-17: LLMFactory, Pydantic schemas, Langfuse observability, and LangGraph."""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, Generation, LLMResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_google_genai.chat_models import (
    GoogleAPIError,
    GoogleAuthenticationError,
    GoogleContextOverflowError,
    GoogleInvalidRequestError,
    GooglePermissionDeniedError,
    GoogleRateLimitError,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import BaseModel, SecretStr, ValidationError

from codesight.agents.graph import (
    DEFAULT_BATCH_DIFF_TOKEN_LIMIT,
    ReviewGraphConfig,
    run_pr_review_graph,
    split_parsed_diff_into_batches,
)
from codesight.agents.llm_factory import (
    LLMConfigurationError,
    LLMFactory,
    LLMStructuredOutputError,
    StructuredInvocationResult,
)
from codesight.agents.schemas import (
    AuditorOutput,
    CodeSuggestion,
    CriticOutput,
    CriticVerdict,
    FindingCandidate,
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
    PRReviewState,
)
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import clear_trace_context
from codesight.core.observability import (
    CodeSightLangfuseCallbackHandler,
    _normalize_langfuse_trace_id,
    _ResilientSpanExporter,
    create_langfuse_trace_session,
    estimate_model_cost_usd,
    mask_otel_spans_allowlist,
    sanitize_telemetry_value,
)
from codesight.diff.file_filter import estimate_tokens
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser

# ---------------------------------------------------------------------------
# Helpers & Test Fakes
# ---------------------------------------------------------------------------


def _make_patch_diff(
    files: Sequence[tuple[str, Sequence[str]]],
) -> ParsedPullRequestDiff:
    """Build a real ParsedPullRequestDiff via SmartDiffParser from (path, added_lines) pairs."""
    entries: list[PatchFileEntry] = []
    for path, added_lines in files:
        count = len(added_lines)
        header = f"@@ -1,1 +1,{count + 1} @@"
        body_lines = [" def anchor() -> None:"] + [f"+{line}" for line in added_lines]
        patch_text = "\n".join([header, *body_lines]) + "\n"
        entries.append(PatchFileEntry(filename=path, patch=patch_text, status="modified"))
    return SmartDiffParser().parse_patch_entries(entries)


def _make_exact_token_diff(
    target_tokens: int, *, file_path: str = "src/service.py"
) -> ParsedPullRequestDiff:
    """Construct a real ParsedPullRequestDiff whose annotated_diff estimates to target_tokens."""
    # Start with a small 1-line diff and grow the added line content to hit target_tokens exactly.
    base_diff = _make_patch_diff([(file_path, ["x = 1"])])
    base_bytes = len(base_diff.annotated_diff.encode("utf-8"))
    # estimate_tokens(s) == ceil(utf8_bytes / 4) -> target_bytes = target_tokens * 4
    target_bytes = target_tokens * 4
    assert target_bytes > base_bytes
    extra_bytes = target_bytes - base_bytes

    # Distribute extra bytes across clean lines (~80 chars each) so lines stay realistic.
    lines: list[str] = []
    # Measure incrementally so line-number prefix width stays exact.
    line_payload = "    value = '" + ("a" * 64) + "'"
    est_lines = max(1, extra_bytes // (len(line_payload) + 12))
    lines = [f"    v_{i:04d} = '{'a' * 64}'" for i in range(est_lines)]
    candidate = _make_patch_diff([(file_path, lines)])

    # Adjust by adding/removing whole lines until <= target_tokens, then pad last line precisely.
    while estimate_tokens(candidate.annotated_diff) > target_tokens and len(lines) > 1:
        lines.pop()
        candidate = _make_patch_diff([(file_path, lines)])

    while estimate_tokens(candidate.annotated_diff) < target_tokens:
        deficit_tokens = target_tokens - estimate_tokens(candidate.annotated_diff)
        if deficit_tokens > 30:
            idx = len(lines)
            lines.append(f"    v_{idx:04d} = '{'b' * 64}'")
            next_cand = _make_patch_diff([(file_path, lines)])
            if estimate_tokens(next_cand.annotated_diff) <= target_tokens:
                candidate = next_cand
                continue
            lines.pop()
        # Pad the last line byte-by-byte to land on exact target_tokens.
        cur_bytes = len(candidate.annotated_diff.encode("utf-8"))
        desired_bytes = target_tokens * 4 - 1  # ceil((4*k - 1) / 4) == k
        if desired_bytes < cur_bytes:
            desired_bytes = target_tokens * 4
        pad = max(1, desired_bytes - cur_bytes)
        lines[-1] = lines[-1] + ("z" * pad)
        candidate = _make_patch_diff([(file_path, lines)])
        if estimate_tokens(candidate.annotated_diff) > target_tokens:
            # Trim back slightly if line-width grew
            overshoot = len(candidate.annotated_diff.encode("utf-8")) - target_tokens * 4
            lines[-1] = lines[-1][: -max(1, overshoot)]
            candidate = _make_patch_diff([(file_path, lines)])
            break

    assert estimate_tokens(candidate.annotated_diff) == target_tokens
    return candidate


class _RecordingSpanExporter(SpanExporter):
    """In-memory OTel SpanExporter capturing every exported span for assertion."""

    def __init__(
        self,
        *,
        fail_on_export: bool = False,
        fail_on_flush: bool = False,
        export_result: SpanExportResult = SpanExportResult.SUCCESS,
        export_delay_seconds: float = 0.0,
        flush_delay_seconds: float = 0.0,
        flush_return: bool = True,
    ) -> None:
        self.spans: list[ReadableSpan] = []
        self.fail_on_export = fail_on_export
        self.fail_on_flush = fail_on_flush
        self.export_result = export_result
        self.export_delay_seconds = export_delay_seconds
        self.flush_delay_seconds = flush_delay_seconds
        self.flush_return = flush_return

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.export_delay_seconds > 0.0:
            time.sleep(self.export_delay_seconds)
        if self.fail_on_export:
            raise RuntimeError("langfuse exporter connection refused")
        if self.export_result != SpanExportResult.SUCCESS:
            return self.export_result
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self.flush_delay_seconds > 0.0:
            time.sleep(self.flush_delay_seconds)
        if self.fail_on_flush:
            raise RuntimeError("langfuse exporter flush timed out")
        return self.flush_return


class _FakeStructuredChatModel(BaseChatModel):
    """LangChain BaseChatModel stub that fires real callbacks and returns structured outputs."""

    model_name: str
    handler: Callable[[str, type[BaseModel], list[BaseMessage]], Awaitable[dict[str, Any]]]

    @property
    def _llm_type(self) -> str:
        return "fake-gemini-chat"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("Use async invocation in tests.")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        schema = kwargs["structured_schema"]
        payload = await self.handler(self.model_name, schema, messages)
        parsed = payload["parsed"]
        usage = payload.get(
            "usage",
            {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
        )
        raw_content = payload.get("raw_content", json.dumps({"ok": True}))
        ai_msg = AIMessage(
            content=raw_content,
            usage_metadata=usage,
            response_metadata={"model_name": self.model_name},
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=ai_msg,
                    generation_info={
                        "parsed": parsed,
                        "parsing_error": payload.get("parsing_error"),
                    },
                )
            ],
            llm_output={"model_name": self.model_name},
        )

    def with_structured_output(
        self,
        schema: dict[str, Any] | type[BaseModel],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        assert isinstance(schema, type) and issubclass(schema, BaseModel)

        async def _run(messages: list[BaseMessage], config: Any = None) -> Any:
            result = await self.agenerate(
                [messages],
                callbacks=config.get("callbacks") if isinstance(config, dict) else None,
                structured_schema=schema,
            )
            gen = result.generations[0][0]
            assert isinstance(gen, ChatGeneration)
            info = gen.generation_info or {}
            if include_raw:
                return {
                    "raw": gen.message,
                    "parsed": info.get("parsed"),
                    "parsing_error": info.get("parsing_error"),
                }
            return info.get("parsed")

        return RunnableLambda(_run)


# ---------------------------------------------------------------------------
# 1. Config & Model Selection Tests
# ---------------------------------------------------------------------------


class TestConfigAndModelSelection:
    @pytest.fixture(autouse=True)
    def _clear_llm_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_name in (
            "LLM_PROVIDER",
            "AUDITOR_MODEL",
            "CRITIC_MODEL",
            "LLM_MAX_CONCURRENCY",
            "LANGFUSE_TRACING_ENABLED",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "GEMINI_API_KEY",
        ):
            monkeypatch.delenv(env_name, raising=False)

    def test_settings_defaults_and_critic_model_variants(self) -> None:
        default_cfg = Settings(_env_file=None)  # type: ignore[call-arg]
        assert default_cfg.llm_provider == "gemini"
        assert default_cfg.auditor_model == "gemini-2.5-flash"
        assert default_cfg.critic_model == "gemini-2.5-flash"
        assert default_cfg.llm_max_concurrency == 3
        assert default_cfg.gemini_embed_concurrency == 2
        assert default_cfg.has_langfuse_credentials is False

        pro_cfg = Settings(_env_file=None, critic_model="gemini-2.5-pro")  # type: ignore[call-arg]
        assert pro_cfg.critic_model == "gemini-2.5-pro"

        with pytest.raises(ValidationError):
            Settings(_env_file=None, critic_model="gemini-invalid-99")  # type: ignore[call-arg]
        with pytest.raises(ValidationError, match="AUDITOR_MODEL"):
            Settings(_env_file=None, auditor_model="gemini-invalid-99")  # type: ignore[call-arg]

    def test_llm_factory_selects_models_and_disables_sdk_retries(self) -> None:
        flash_settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            gemini_api_key=SecretStr("fake-gemini-key"),
            critic_model="gemini-2.5-flash",
        )
        pro_settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            gemini_api_key=SecretStr("fake-gemini-key"),
            critic_model="gemini-2.5-pro",
        )

        flash_factory = LLMFactory(settings=flash_settings)
        pro_factory = LLMFactory(settings=pro_settings)
        custom_out_factory = LLMFactory(settings=pro_settings, max_output_tokens=2048)

        assert flash_factory.resolve_model_name("security_auditor") == "gemini-2.5-flash"
        assert flash_factory.resolve_model_name("logic_performance_auditor") == "gemini-2.5-flash"
        assert flash_factory.resolve_model_name("critic") == "gemini-2.5-flash"
        assert pro_factory.resolve_model_name("critic") == "gemini-2.5-pro"

        with patch("codesight.agents.llm_factory.ChatGoogleGenerativeAI") as mock_chat_cls:
            mock_chat_cls.return_value = SimpleNamespace(model="gemini-2.5-pro")
            pro_factory.build_chat_model("critic")
            kwargs = mock_chat_cls.call_args.kwargs
            assert kwargs["model"] == "gemini-2.5-pro"
            # max_retries=1 sets google-genai HttpRetryOptions(attempts=1) -> zero SDK retries
            assert kwargs["max_retries"] == 1
            assert kwargs["max_output_tokens"] == 4096

            custom_out_factory.build_chat_model("critic")
            assert mock_chat_cls.call_args.kwargs["max_output_tokens"] == 2048

    def test_llm_factory_missing_api_key_and_unknown_provider_fail_fast(self) -> None:
        no_key = Settings(_env_file=None, gemini_api_key=None)  # type: ignore[call-arg]
        factory = LLMFactory(settings=no_key)
        with pytest.raises(LLMConfigurationError, match="GEMINI_API_KEY"):
            factory.build_chat_model("security_auditor")

        custom_factory = LLMFactory(settings=no_key, provider="unsupported-provider")
        with pytest.raises(LLMConfigurationError, match="Unsupported LLM provider"):
            custom_factory.build_chat_model("security_auditor")

        # Registering a custom provider adapter works cleanly without touching Gemini defaults
        stub_model = SimpleNamespace(model="custom-llm")
        custom_factory.register_provider("custom", lambda *_args: stub_model)  # type: ignore[arg-type, return-value]
        custom_via_reg = LLMFactory(settings=no_key, provider="custom")
        custom_via_reg.register_provider("custom", lambda *_args: stub_model)  # type: ignore[arg-type, return-value]
        assert custom_via_reg.build_chat_model("critic") is stub_model

        # Optional providers: missing API key & missing optional package both fail clearly
        openai_factory = LLMFactory(settings=no_key, provider="openai", auditor_model="gpt-4o-mini")
        with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
            openai_factory.build_chat_model("security_auditor")

        anthropic_factory = LLMFactory(
            settings=no_key,
            provider="anthropic",
            auditor_model="claude-3-5-sonnet",
        )
        with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY"):
            anthropic_factory.build_chat_model("security_auditor")

        with patch(
            "codesight.agents.llm_factory.importlib.import_module",
            side_effect=ImportError("no module"),
        ):
            with pytest.raises(LLMConfigurationError, match="langchain-openai"):
                openai_factory.build_chat_model("security_auditor")
            with pytest.raises(LLMConfigurationError, match="langchain-anthropic"):
                anthropic_factory.build_chat_model("security_auditor")

        # Configured OpenAI/Anthropic keys flow from Settings into the provider chat models
        openai_cfg = Settings(
            _env_file=None,
            llm_provider="openai",
            auditor_model="gpt-4o-mini",
            critic_model="gpt-4o",
            openai_api_key=SecretStr("sk-test-openai"),
        )
        assert openai_cfg.has_active_llm_api_key is True
        assert LLMFactory(settings=openai_cfg).build_chat_model("security_auditor") is not None

        anthropic_cfg = Settings(
            _env_file=None,
            llm_provider="anthropic",
            auditor_model="claude-3-5-sonnet-latest",
            critic_model="claude-3-5-sonnet-latest",
            anthropic_api_key=SecretStr("sk-ant-test"),
        )
        assert anthropic_cfg.has_active_llm_api_key is True
        assert LLMFactory(settings=anthropic_cfg).build_chat_model("critic") is not None


# ---------------------------------------------------------------------------
# 2. Pydantic v2 Schema Validation Tests
# ---------------------------------------------------------------------------


class TestAgentSchemas:
    def test_code_suggestion_validates_line_bounds_and_rejects_bool(self) -> None:
        ok = CodeSuggestion(start_line=10, end_line=12, replacement_code="return safe_val")
        assert ok.is_single_line is False

        single = CodeSuggestion(start_line=10, end_line=10, replacement_code="return safe_val")
        assert single.is_single_line is True

        with pytest.raises(ValidationError):
            CodeSuggestion(start_line=15, end_line=10, replacement_code="x = 1")
        with pytest.raises(ValidationError):
            CodeSuggestion(start_line=0, end_line=1, replacement_code="x = 1")
        with pytest.raises(ValidationError):
            CodeSuggestion(start_line=True, end_line=2, replacement_code="x = 1")  # type: ignore[arg-type]

    def test_finding_candidate_validates_coordinates_and_github_mapping(self) -> None:
        single = FindingCandidate(
            candidate_id="b000-sec-001",
            role="security_auditor",
            batch_index=0,
            file_path="src/auth.py",
            start_line=24,
            end_line=24,
            severity="CRITICAL",
            category="injection",
            title="SQL string interpolation",
            evidence="User input concatenated into SQL statement at L24.",
        )
        # Single-line MUST omit start_line per github-diff-suggestion-guard
        assert single.to_github_line_coords() == (None, 24)

        multi = FindingCandidate(
            candidate_id="b000-log-001",
            role="logic_performance_auditor",
            batch_index=0,
            file_path="src/auth.py",
            start_line=20,
            end_line=25,
            severity="WARNING",
            category="performance",
            title="N+1 query inside loop",
            evidence="Database call inside for-loop on L20-L25.",
            suggestion=CodeSuggestion(start_line=20, end_line=25, replacement_code="pass"),
        )
        assert multi.to_github_line_coords() == (20, 25)

        with pytest.raises(ValidationError):
            FindingCandidate(
                candidate_id="b000-sec-002",
                role="security_auditor",
                batch_index=0,
                file_path="src/auth.py",
                start_line=30,
                end_line=12,
                severity="CRITICAL",
                category="injection",
                title="Reversed lines",
                evidence="Invalid range.",
            )

        with pytest.raises(ValidationError):
            FindingCandidate(
                candidate_id="   ",
                role="security_auditor",
                batch_index=0,
                file_path="src/auth.py",
                start_line=1,
                end_line=1,
                severity="INFO",
                category="style",
                title="Blank id",
                evidence="Evidence",
            )

    def test_critic_verdict_and_review_state_validation(self) -> None:
        verdict = CriticVerdict(
            candidate_id="b000-sec-001",
            batch_index=0,
            decision="APPROVE",
            rationale="Verified reachable from untrusted input.",
        )
        assert verdict.decision == "APPROVE"

        with pytest.raises(ValidationError):
            CriticVerdict(
                candidate_id="b000-sec-001",
                batch_index=0,
                decision="MAYBE",  # type: ignore[arg-type]
                rationale="Invalid decision",
            )

        state = PRReviewState(pr_id="Tieens-04/CodeSight-AI#17", head_sha="abc123", trace_id="t1")
        assert state.status == "pending"
        assert state.batches == []


# ---------------------------------------------------------------------------
# 3. LLMFactory Semaphore(3), Retry 429, Permanent Errors & Cancellation
# ---------------------------------------------------------------------------


class TestLLMFactoryConcurrencyAndRetries:
    @pytest.mark.asyncio
    async def test_shared_semaphore_caps_inflight_provider_calls_at_three(self) -> None:
        in_flight = 0
        max_in_flight = 0

        async def _slow_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
                return StructuredInvocationResult(
                    parsed=AuditorOutput(findings=[]),
                    model_name=model_name,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    estimated_cost_usd=0.00001,
                    latency_ms=20.0,
                    attempts=1,
                )
            finally:
                in_flight -= 1

        settings = Settings(_env_file=None, llm_max_concurrency=3)  # type: ignore[call-arg]
        factory = LLMFactory(settings=settings, structured_invoker=_slow_invoker)

        tasks = [
            factory.invoke_structured(
                role="security_auditor" if i % 2 == 0 else "logic_performance_auditor",
                schema=AuditorOutput,
                messages=[],
            )
            for i in range(9)
        ]
        results = await asyncio.gather(*tasks)
        assert len(results) == 9
        assert max_in_flight == 3

    @pytest.mark.asyncio
    async def test_retries_429_honoring_retry_info_and_releases_permit_before_sleep(self) -> None:
        sleeps: list[float] = []
        sleep_entered = asyncio.Event()
        release_sleep = asyncio.Event()
        attempts_by_tag: dict[str, int] = {}
        concurrent_during_backoff = 0
        max_during_backoff = 0

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            sleep_entered.set()
            await release_sleep.wait()

        async def _invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            nonlocal concurrent_during_backoff, max_during_backoff
            tag = str(messages[0].content)
            attempts_by_tag[tag] = attempts_by_tag.get(tag, 0) + 1
            if tag == "retrying" and attempts_by_tag[tag] == 1:
                cause = genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "3.5s",
                                }
                            ],
                        }
                    },
                )
                raise GoogleRateLimitError("429 RESOURCE_EXHAUSTED") from cause

            if tag.startswith("peer-"):
                concurrent_during_backoff += 1
                max_during_backoff = max(max_during_backoff, concurrent_during_backoff)
                await asyncio.sleep(0.02)
                concurrent_during_backoff -= 1

            return StructuredInvocationResult(
                parsed=AuditorOutput(findings=[]),
                model_name=model_name,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=None,
                latency_ms=1.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, llm_max_concurrency=3),  # type: ignore[call-arg]
            structured_invoker=_invoker,
            sleep_fn=_fake_sleep,
            max_retries=2,
        )

        from langchain_core.messages import HumanMessage

        retry_task = asyncio.create_task(
            factory.invoke_structured(
                role="security_auditor",
                schema=AuditorOutput,
                messages=[HumanMessage(content="retrying")],
            )
        )
        await asyncio.wait_for(sleep_entered.wait(), timeout=2.0)

        # All 3 semaphore slots MUST be free while retry_task is sleeping in backoff!
        peer_tasks = [
            asyncio.create_task(
                factory.invoke_structured(
                    role="logic_performance_auditor",
                    schema=AuditorOutput,
                    messages=[HumanMessage(content=f"peer-{i}")],
                )
            )
            for i in range(3)
        ]
        await asyncio.gather(*peer_tasks)
        assert max_during_backoff == 3

        release_sleep.set()
        res = await retry_task
        assert res.attempts == 2
        assert len(sleeps) == 1
        assert 3.5 <= sleeps[0] <= 3.75

    @pytest.mark.asyncio
    async def test_exhausted_429_and_503_raise_domain_retry_errors(self) -> None:
        sleeps: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def _always_429(*_: Any) -> Any:
            resp = SimpleNamespace(headers={"Retry-After": "6"})
            cause = genai_errors.ClientError(
                429, {"error": {"message": "RESOURCE_EXHAUSTED"}}, resp
            )
            raise GoogleRateLimitError("429") from cause

        factory_429 = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_always_429,
            sleep_fn=_fake_sleep,
            max_retries=2,
        )
        with pytest.raises(RateLimitTaskError) as exc_429:
            await factory_429.invoke_structured(
                role="critic",
                schema=CriticOutput,
                messages=[],
            )
        assert exc_429.value.retry_after_seconds == 6.0
        assert len(sleeps) == 2
        assert all(6.0 <= s <= 6.25 for s in sleeps)

        sleeps.clear()
        calls_503 = 0

        async def _always_503(*_: Any) -> Any:
            nonlocal calls_503
            calls_503 += 1
            if calls_503 == 1:
                raise GoogleAPIError(code=503, response_json={"error": {"message": "overloaded"}})
            raise httpx.ReadTimeout("timed out")

        factory_503 = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_always_503,
            sleep_fn=_fake_sleep,
            max_retries=2,
            retry_base_seconds=0.5,
            retry_cap_seconds=2.0,
        )
        with pytest.raises(RetryableTaskError) as exc_503:
            await factory_503.invoke_structured(
                role="critic",
                schema=CriticOutput,
                messages=[],
            )
        assert not isinstance(exc_503.value, RateLimitTaskError)
        assert calls_503 == 3
        assert len(sleeps) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            GoogleAuthenticationError("401 invalid key"),
            GooglePermissionDeniedError("403 forbidden"),
            GoogleInvalidRequestError("400 bad schema"),
            GoogleContextOverflowError(code=400, response_json={"error": {"message": "too long"}}),
            LLMStructuredOutputError("schema parse failed"),
            ValueError("bad argument"),
        ],
    )
    async def test_permanent_errors_propagate_immediately_without_retries(
        self,
        exc: Exception,
    ) -> None:
        calls = 0
        sleeps: list[float] = []

        async def _fail_once(*_: Any) -> Any:
            nonlocal calls
            calls += 1
            raise exc

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        factory = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_fail_once,
            sleep_fn=_fake_sleep,
            max_retries=3,
        )
        with pytest.raises(type(exc)):
            await factory.invoke_structured(
                role="security_auditor", schema=AuditorOutput, messages=[]
            )

        assert calls == 1
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_cancellation_propagates_immediately_during_backoff(self) -> None:
        in_sleep = asyncio.Event()
        calls = 0

        async def _flaky(*_: Any) -> Any:
            nonlocal calls
            calls += 1
            raise GoogleRateLimitError("429 RESOURCE_EXHAUSTED")

        async def _blocking_sleep(delay: float) -> None:
            in_sleep.set()
            await asyncio.sleep(60.0)

        factory = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_flaky,
            sleep_fn=_blocking_sleep,
            max_retries=5,
        )
        task = asyncio.create_task(
            factory.invoke_structured(role="security_auditor", schema=AuditorOutput, messages=[])
        )
        await asyncio.wait_for(in_sleep.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1

    @pytest.mark.asyncio
    async def test_separate_llm_factory_instances_share_per_loop_semaphore(self) -> None:
        in_flight = 0
        max_in_flight = 0

        async def _slow_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.03)
                return StructuredInvocationResult(
                    parsed=AuditorOutput(findings=[]),
                    model_name=model_name,
                    input_tokens=5,
                    output_tokens=2,
                    total_tokens=7,
                    estimated_cost_usd=None,
                    latency_ms=30.0,
                    attempts=1,
                )
            finally:
                in_flight -= 1

        cfg = Settings(_env_file=None, llm_max_concurrency=3)  # type: ignore[call-arg]
        factory_a = LLMFactory(settings=cfg, structured_invoker=_slow_invoker)
        factory_b = LLMFactory(settings=cfg, structured_invoker=_slow_invoker)

        tasks = [
            factory_a.invoke_structured(role="security_auditor", schema=AuditorOutput, messages=[])
            for _ in range(6)
        ] + [
            factory_b.invoke_structured(
                role="logic_performance_auditor", schema=AuditorOutput, messages=[]
            )
            for _ in range(6)
        ]
        results = await asyncio.gather(*tasks)
        assert len(results) == 12
        assert max_in_flight == 3

    @pytest.mark.asyncio
    async def test_retry_429_fails_fast_when_retry_after_exceeds_deadline_or_cap(self) -> None:
        sleeps: list[float] = []
        calls = 0

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def _invoker(*_: Any) -> Any:
            nonlocal calls
            calls += 1
            cause = genai_errors.ClientError(
                429,
                {
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "45s",
                            }
                        ],
                    }
                },
            )
            raise GoogleRateLimitError("429 RESOURCE_EXHAUSTED") from cause

        factory = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_invoker,
            sleep_fn=_fake_sleep,
            max_retries=3,
            retry_cap_seconds=15.0,
            max_total_wait_seconds=20.0,
        )
        with pytest.raises(RateLimitTaskError) as exc_info:
            await factory.invoke_structured(
                role="security_auditor", schema=AuditorOutput, messages=[]
            )

        assert calls == 1
        assert sleeps == []
        assert exc_info.value.retry_after_seconds == 45.0

    @pytest.mark.asyncio
    async def test_exponential_backoff_doubles_delay_until_cap(self) -> None:
        sleeps: list[float] = []

        class _MaxBoundRng(random.Random):
            def uniform(self, a: float, b: float) -> float:
                return b

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def _always_503(*_: Any) -> Any:
            raise GoogleAPIError(code=503, response_json={"error": {"message": "busy"}})

        factory = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_always_503,
            sleep_fn=_fake_sleep,
            rng=_MaxBoundRng(),
            max_retries=4,
            retry_base_seconds=0.5,
            retry_cap_seconds=2.0,
            max_total_wait_seconds=None,
        )
        with pytest.raises(RetryableTaskError):
            await factory.invoke_structured(
                role="security_auditor", schema=AuditorOutput, messages=[]
            )

        assert sleeps == [0.5, 1.0, 2.0, 2.0]

    @pytest.mark.asyncio
    async def test_retry_hint_prioritizes_retry_info_over_retry_after_header(self) -> None:
        sleeps: list[float] = []
        calls = 0

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def _invoker(*_: Any) -> StructuredInvocationResult[Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                resp = SimpleNamespace(headers={"Retry-After": "9"})
                cause = genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "4.2s",
                                }
                            ],
                        }
                    },
                    resp,
                )
                raise GoogleRateLimitError("429 RESOURCE_EXHAUSTED") from cause
            return StructuredInvocationResult(
                parsed=AuditorOutput(findings=[]),
                model_name="gemini-2.5-flash",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=None,
                latency_ms=1.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            structured_invoker=_invoker,
            sleep_fn=_fake_sleep,
            max_retries=2,
            retry_cap_seconds=15.0,
            max_total_wait_seconds=20.0,
        )
        result = await factory.invoke_structured(
            role="security_auditor", schema=AuditorOutput, messages=[]
        )
        assert result.attempts == 2
        assert len(sleeps) == 1
        assert 4.2 <= sleeps[0] <= 4.45

    @pytest.mark.asyncio
    async def test_chat_model_cached_across_invocations_per_role(self) -> None:
        build_calls: list[tuple[str, str]] = []

        async def _handler(
            model_name: str, schema: type[BaseModel], messages: list[BaseMessage]
        ) -> dict[str, Any]:
            return {
                "parsed": schema(),
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            }

        def _builder(role: str, model_name: str) -> BaseChatModel:
            build_calls.append((role, model_name))
            return _FakeStructuredChatModel(model_name=model_name, handler=_handler)

        factory = LLMFactory(
            settings=Settings(_env_file=None, critic_model="gemini-2.5-pro"),  # type: ignore[call-arg]
            chat_model_builder=_builder,
        )
        await factory.invoke_structured(role="security_auditor", schema=AuditorOutput, messages=[])
        await factory.invoke_structured(role="security_auditor", schema=AuditorOutput, messages=[])
        await factory.invoke_structured(role="critic", schema=CriticOutput, messages=[])

        assert build_calls == [
            ("security_auditor", "gemini-2.5-flash"),
            ("critic", "gemini-2.5-pro"),
        ]


# ---------------------------------------------------------------------------
# 4. Diff Batching Boundary Tests (11_999 / 12_000 / 12_001, Hunk, Line)
# ---------------------------------------------------------------------------


class TestDiffBatchingBoundaries:
    def test_exact_token_boundaries_11999_12000_12001(self) -> None:
        d_11999 = _make_exact_token_diff(11_999)
        batches_11999, unrev_11999 = split_parsed_diff_into_batches(
            d_11999,
            max_batch_tokens=DEFAULT_BATCH_DIFF_TOKEN_LIMIT,
        )
        assert len(batches_11999) == 1
        assert unrev_11999 == ()
        assert batches_11999[0].estimated_tokens == 11_999

        d_12000 = _make_exact_token_diff(12_000)
        batches_12000, unrev_12000 = split_parsed_diff_into_batches(
            d_12000,
            max_batch_tokens=DEFAULT_BATCH_DIFF_TOKEN_LIMIT,
        )
        assert len(batches_12000) == 1
        assert unrev_12000 == ()
        assert batches_12000[0].estimated_tokens == 12_000

        d_12001 = _make_exact_token_diff(12_001)
        batches_12001, unrev_12001 = split_parsed_diff_into_batches(
            d_12001,
            max_batch_tokens=DEFAULT_BATCH_DIFF_TOKEN_LIMIT,
        )
        assert len(batches_12001) >= 2
        assert unrev_12001 == ()
        assert all(
            estimate_tokens(b.annotated_diff) <= DEFAULT_BATCH_DIFF_TOKEN_LIMIT
            for b in batches_12001
        )

    def test_multifile_and_single_huge_hunk_preserve_all_lines_without_duplication(self) -> None:
        # Multi-file diff that exceeds a tight 200-token batch budget
        diff_multi = _make_patch_diff(
            [
                ("src/a.py", [f"    a_{i} = '{'x' * 50}'" for i in range(8)]),
                ("src/b.py", [f"    b_{i} = '{'y' * 50}'" for i in range(8)]),
                ("src/c.py", [f"    c_{i} = '{'z' * 50}'" for i in range(8)]),
            ]
        )
        batches, unrev = split_parsed_diff_into_batches(diff_multi, max_batch_tokens=200)
        assert len(batches) >= 3
        assert unrev == ()
        assert all(estimate_tokens(b.annotated_diff) <= 200 for b in batches)

        # Verify every original added line appears in EXACTLY ONE batch with original path & L-num
        expected_lines = [
            dl.llm_text
            for f in diff_multi.files
            for h in f.hunks
            for dl in h.lines
            if dl.kind == "add"
        ]
        seen_counts = {line: 0 for line in expected_lines}
        for batch in batches:
            assert "=== FILE: " in batch.annotated_diff
            assert "@@ " in batch.annotated_diff
            for line in expected_lines:
                if line in batch.annotated_diff:
                    seen_counts[line] += 1
        assert all(count == 1 for count in seen_counts.values())

    def test_single_oversized_line_returns_unreviewable_metadata_without_dropping_siblings(
        self,
    ) -> None:
        huge_secret_line = "    blob = '" + ("S" * 1600) + "'"  # ~400 tokens alone
        diff = _make_patch_diff(
            [
                (
                    "src/huge.py",
                    [
                        "    before_ok = 1",
                        huge_secret_line,
                        "    after_ok = 2",
                    ],
                )
            ]
        )
        batches, unrev = split_parsed_diff_into_batches(diff, max_batch_tokens=120)
        assert len(unrev) == 1
        seg = unrev[0]
        assert seg.file_path == "src/huge.py"
        assert seg.hunk_id == 1
        assert seg.right_line == 3  # anchor=L1, before_ok=L2, huge_secret_line=L3
        assert seg.estimated_tokens > 120
        assert "SSSSS" not in str(seg.model_dump())

        # Sibling normal lines MUST still be batched and within budget
        combined = "\n".join(b.annotated_diff for b in batches)
        assert "before_ok = 1" in combined
        assert "after_ok = 2" in combined
        assert "SSSSS" not in combined
        assert all(estimate_tokens(b.annotated_diff) <= 120 for b in batches)

    def test_single_file_multiple_hunks_splits_at_hunk_boundary_preserving_file_header(
        self,
    ) -> None:
        hunk1 = "\n".join(
            [
                "@@ -1,1 +1,6 @@",
                " def alpha() -> None:",
                *[f"+    a_{i} = '{'x' * 55}'" for i in range(5)],
            ]
        )
        hunk2 = "\n".join(
            [
                "@@ -50,1 +55,6 @@",
                " def beta() -> None:",
                *[f"+    b_{i} = '{'y' * 55}'" for i in range(5)],
            ]
        )
        diff = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/multi_hunk.py",
                    patch=f"{hunk1}\n{hunk2}\n",
                    status="modified",
                )
            ]
        )
        assert len(diff.files) == 1
        assert len(diff.files[0].hunks) == 2

        hunk1_tokens = estimate_tokens(
            f"=== FILE: src/multi_hunk.py (modified) ===\n{diff.files[0].hunks[0].annotated_text}"
        )
        # Budget fits each hunk individually, but NOT both hunks together
        budget = hunk1_tokens + 15
        batches, unrev = split_parsed_diff_into_batches(diff, max_batch_tokens=budget)
        assert len(batches) == 2
        assert unrev == ()
        assert all(
            "=== FILE: src/multi_hunk.py (modified) ===" in b.annotated_diff for b in batches
        )
        assert "@@ -1,1 +1,6 @@" in batches[0].annotated_diff
        assert "@@ -50,1 +55,6 @@" in batches[1].annotated_diff
        assert "@@ -50,1 +55,6 @@" not in batches[0].annotated_diff
        assert "@@ -1,1 +1,6 @@" not in batches[1].annotated_diff

    def test_graph_config_validation_guards(self) -> None:
        with pytest.raises(ValueError, match="batch_diff_token_limit"):
            ReviewGraphConfig(batch_diff_token_limit=0)
        with pytest.raises(ValueError, match="output_reserve_tokens"):
            ReviewGraphConfig(max_request_tokens=100, output_reserve_tokens=100)
        tiny = ReviewGraphConfig(
            batch_diff_token_limit=50,
            max_request_tokens=200,
            output_reserve_tokens=100,
        )
        with pytest.raises(ValueError, match="too small"):
            tiny.available_auditor_diff_tokens()

    def test_deleted_files_are_excluded_from_batches(self) -> None:
        deleted_only = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/legacy_dead.py",
                    patch="@@ -1,3 +0,0 @@\n-old_1 = 1\n-old_2 = 2\n-old_3 = 3\n",
                    status="removed",
                )
            ]
        )
        batches_only, unrev_only = split_parsed_diff_into_batches(deleted_only)
        assert batches_only == ()
        assert unrev_only == ()

        mixed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/legacy_dead.py",
                    patch="@@ -1,3 +0,0 @@\n-old_1 = 1\n-old_2 = 2\n-old_3 = 3\n",
                    status="removed",
                ),
                PatchFileEntry(
                    filename="src/active.py",
                    patch="@@ -1,1 +1,2 @@\n def run() -> int:\n+    return 1\n",
                    status="modified",
                ),
            ]
        )
        batches_mixed, unrev_mixed = split_parsed_diff_into_batches(mixed)
        assert len(batches_mixed) == 1
        assert unrev_mixed == ()
        assert batches_mixed[0].file_paths == ("src/active.py",)
        assert "legacy_dead.py" not in batches_mixed[0].annotated_diff


# ---------------------------------------------------------------------------
# 5. LangGraph End-to-End Orchestration Tests
# ---------------------------------------------------------------------------


class TestLangGraphOrchestration:
    @pytest.mark.asyncio
    async def test_empty_filtered_diff_makes_zero_llm_calls(self) -> None:
        calls: list[str] = []

        async def _unexpected(*_: Any) -> Any:
            calls.append("called")
            raise AssertionError("LLM must not be invoked on empty diff")

        empty_diff = SmartDiffParser().parse_patch_entries(
            [PatchFileEntry(filename="package-lock.json", patch="@@ -1 +1 @@\n-a\n+b\n")]
        )
        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            structured_invoker=_unexpected,
        )
        final_state = await run_pr_review_graph(
            parsed_diff=empty_diff,
            llm_factory=factory,
            pr_id="org/repo#1",
            head_sha="sha0",
            trace_id="trace-empty",
        )
        assert calls == []
        assert final_state.batching_mode == "empty"
        assert final_state.status == "empty_diff"
        assert final_state.merged_findings == []
        assert final_state.merged_verdicts == []

    @pytest.mark.asyncio
    async def test_medium_pr_makes_exactly_three_calls_with_parallel_auditors_then_critic(
        self,
    ) -> None:
        # < 500 lines, well under 12,000 tokens
        diff = _make_patch_diff(
            [("src/payments.py", [f"    step_{i} = process({i})" for i in range(40)])]
        )
        assert estimate_tokens(diff.annotated_diff) <= DEFAULT_BATCH_DIFF_TOKEN_LIMIT

        auditor_entered: set[str] = set()
        both_auditors_concurrent = asyncio.Event()
        auditors_completed: set[str] = set()
        call_log: list[tuple[str, str]] = []
        critic_saw_both = False

        async def _invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            nonlocal critic_saw_both
            call_log.append((role, model_name))

            if role in {"security_auditor", "logic_performance_auditor"}:
                auditor_entered.add(role)
                if len(auditor_entered) == 2:
                    both_auditors_concurrent.set()
                # Wait briefly to PROVE both auditors are executing concurrently!
                await asyncio.wait_for(both_auditors_concurrent.wait(), timeout=2.0)
                auditors_completed.add(role)
                finding = FindingCandidate(
                    candidate_id=f"raw-{role}",
                    role=role,  # type: ignore[arg-type]
                    batch_index=0,
                    file_path="src/payments.py",
                    start_line=5 if role == "security_auditor" else 12,
                    end_line=5 if role == "security_auditor" else 12,
                    severity="CRITICAL" if role == "security_auditor" else "WARNING",
                    category="security" if role == "security_auditor" else "performance",
                    title=f"Finding from {role}",
                    evidence=f"Evidence from {role} calling process",
                )
                return StructuredInvocationResult(
                    parsed=AuditorOutput(findings=[finding]),
                    model_name=model_name,
                    input_tokens=300,
                    output_tokens=60,
                    total_tokens=360,
                    estimated_cost_usd=0.00024,
                    latency_ms=15.0,
                    attempts=1,
                )

            assert role == "critic"
            assert auditors_completed == {"security_auditor", "logic_performance_auditor"}
            prompt_text = "\n".join(str(m.content) for m in messages)
            critic_saw_both = (
                "Finding from security_auditor" in prompt_text
                and "Finding from logic_performance_auditor" in prompt_text
            )
            return StructuredInvocationResult(
                parsed=CriticOutput(
                    verdicts=[
                        CriticVerdict(
                            candidate_id="b000-sec-000",
                            batch_index=0,
                            decision="APPROVE",
                            rationale="Confirmed security risk.",
                        ),
                        CriticVerdict(
                            candidate_id="b000-log-000",
                            batch_index=0,
                            decision="APPROVE",
                            rationale="Confirmed performance issue.",
                        ),
                    ]
                ),
                model_name=model_name,
                input_tokens=450,
                output_tokens=80,
                total_tokens=530,
                estimated_cost_usd=0.000335,
                latency_ms=20.0,
                attempts=1,
            )

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            critic_model="gemini-2.5-pro",
            langfuse_tracing_enabled=False,
        )
        factory = LLMFactory(settings=settings, structured_invoker=_invoker)

        final_state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            pr_id="Tieens-04/CodeSight-AI#17",
            head_sha="deadbeef",
            trace_id="trace-medium",
        )

        # Exactly 3 LLM calls on happy path!
        assert len(call_log) == 3
        assert {r for r, _ in call_log[:2]} == {"security_auditor", "logic_performance_auditor"}
        assert call_log[2] == ("critic", "gemini-2.5-pro")
        assert both_auditors_concurrent.is_set()
        assert critic_saw_both is True
        assert final_state.batching_mode == "single_batch"
        assert final_state.status == "completed"
        assert len(final_state.merged_findings) == 2
        assert len(final_state.merged_verdicts) == 2
        assert len(final_state.llm_metrics) == 3

    @pytest.mark.asyncio
    async def test_mega_pr_map_reduce_send_handles_out_of_order_batches_and_caps_critic_budget(
        self,
    ) -> None:
        # Build a multi-file PR exceeding a configured batch limit so Send() fans out >= 3 batches
        diff = _make_patch_diff(
            [
                ("src/mod_0.py", [f"    m0_{i} = '{'a' * 70}'" for i in range(10)]),
                ("src/mod_1.py", [f"    m1_{i} = '{'b' * 70}'" for i in range(10)]),
                ("src/mod_2.py", [f"    m2_{i} = '{'c' * 70}'" for i in range(10)]),
            ]
        )
        graph_config = ReviewGraphConfig(
            batch_diff_token_limit=300,
            max_request_tokens=1_200,
            output_reserve_tokens=200,
        )
        expected_batches, _ = split_parsed_diff_into_batches(diff, max_batch_tokens=300)
        assert len(expected_batches) >= 3

        in_flight = 0
        max_in_flight = 0
        observed_request_tokens: list[tuple[str, int]] = []

        async def _invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                user_text = "\n".join(str(m.content) for m in messages)
                schema_text = json.dumps(schema.model_json_schema())
                req_tokens = (
                    estimate_tokens(user_text)
                    + estimate_tokens(schema_text)
                    + graph_config.output_reserve_tokens
                )
                observed_request_tokens.append((role, req_tokens))

                # Force reverse completion order: batch 0 sleeps longest, batch 2 finishes fastest
                if "src/mod_0.py" in user_text:
                    await asyncio.sleep(0.05)
                    target_file = "src/mod_0.py"
                    var_prefix = "m0"
                elif "src/mod_1.py" in user_text:
                    await asyncio.sleep(0.025)
                    target_file = "src/mod_1.py"
                    var_prefix = "m1"
                else:
                    await asyncio.sleep(0.005)
                    target_file = "src/mod_2.py"
                    var_prefix = "m2"

                if role in {"security_auditor", "logic_performance_auditor"}:
                    # Emit verbose findings to exercise Critic input budget capping
                    findings = [
                        FindingCandidate(
                            candidate_id=f"tmp-{idx}",
                            role=role,  # type: ignore[arg-type]
                            batch_index=0,
                            file_path=target_file,
                            start_line=2 + idx,
                            end_line=2 + idx,
                            severity="CRITICAL" if idx == 0 else "INFO",
                            category="audit",
                            title=f"{role} issue {idx} on {target_file}",
                            evidence=f"Detail on {var_prefix}_{idx} " + ("E" * 260),
                        )
                        for idx in range(5)
                    ]
                    return StructuredInvocationResult(
                        parsed=AuditorOutput(findings=findings),
                        model_name=model_name,
                        input_tokens=200,
                        output_tokens=100,
                        total_tokens=300,
                        estimated_cost_usd=0.00031,
                        latency_ms=10.0,
                        attempts=1,
                    )

                assert role == "critic"
                # Return a verdict for the first candidate mentioned in Critic prompt
                cand_ids: list[str] = []
                for token in user_text.split():
                    cleaned = token.strip('",:[]{}()')
                    if cleaned.startswith("b") and ("-sec-" in cleaned or "-log-" in cleaned):
                        if cleaned not in cand_ids:
                            cand_ids.append(cleaned)
                verdicts = [
                    CriticVerdict(
                        candidate_id=cid,
                        batch_index=int(cid[1:4]),
                        decision="APPROVE",
                        rationale="Verified within batch.",
                    )
                    for cid in cand_ids[:2]
                ]
                return StructuredInvocationResult(
                    parsed=CriticOutput(verdicts=verdicts),
                    model_name=model_name,
                    input_tokens=250,
                    output_tokens=60,
                    total_tokens=310,
                    estimated_cost_usd=0.000225,
                    latency_ms=12.0,
                    attempts=1,
                )
            finally:
                in_flight -= 1

        factory = LLMFactory(
            settings=Settings(
                _env_file=None, llm_max_concurrency=3, langfuse_tracing_enabled=False
            ),  # type: ignore[call-arg]
            structured_invoker=_invoker,
        )
        state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            graph_config=graph_config,
            pr_id="org/repo#99",
            head_sha="cafebabe",
            trace_id="trace-mega",
        )

        assert state.batching_mode == "map_reduce"
        assert state.status == "completed"
        assert len(state.batch_results) == len(expected_batches)
        # Shared semaphore strictly enforces == 3 concurrent calls across all Send branches
        assert max_in_flight == 3
        # Every single LLM request (both Auditors AND Critic) stayed inside max_request_tokens!
        assert all(tok <= graph_config.max_request_tokens for _, tok in observed_request_tokens)
        # Critic truncated verbose overflow candidates cleanly and recorded exact truncated IDs
        assert sum(br.truncated_candidates_count for br in state.batch_results) > 0
        assert all(
            len(br.truncated_candidate_ids) == br.truncated_candidates_count
            for br in state.batch_results
        )
        truncated_ids = {cid for br in state.batch_results for cid in br.truncated_candidate_ids}
        assert truncated_ids
        assert truncated_ids.isdisjoint({v.candidate_id for v in state.merged_verdicts})
        # Deterministic merged ordering despite out-of-order batch completion
        merged_batches = [f.batch_index for f in state.merged_findings]
        assert merged_batches == sorted(merged_batches)
        verdict_keys = [(v.batch_index, v.candidate_id) for v in state.merged_verdicts]
        assert verdict_keys == sorted(verdict_keys)

    @pytest.mark.asyncio
    async def test_malformed_individual_finding_and_suggestion_are_filtered_without_failing_review(
        self,
    ) -> None:
        diff = _make_patch_diff([("src/resilient.py", [f"    line_{i} = {i}" for i in range(12)])])

        async def _raw_payload_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            if role in {"security_auditor", "logic_performance_auditor"}:
                if role == "security_auditor":
                    raw_payload: dict[str, Any] = {
                        "findings": [
                            {
                                "file_path": "src/resilient.py",
                                "start_line": 5,
                                "end_line": 5,
                                "severity": "critical",
                                "category": "sec",
                                "title": "Valid critical finding",
                                "evidence": "Reachable at L5 via line_3.",
                            },
                            {
                                "file_path": "src/resilient.py",
                                "start_line": 0,
                                "end_line": 0,
                                "severity": "WARNING",
                                "category": "sec",
                                "title": "Invalid line zero finding",
                                "evidence": "Deleted line hallucinated as L0.",
                            },
                            {
                                "file_path": "src/resilient.py",
                                "start_line": 10,
                                "end_line": 10,
                                "severity": "INFO",
                                "category": "sec",
                                "title": "Valid finding with broken suggestion",
                                "evidence": "Issue at L10 via line_8.",
                                "suggestion": {
                                    "start_line": 15,
                                    "end_line": 10,
                                    "replacement_code": "fixed = True",
                                },
                            },
                        ]
                    }
                else:
                    raw_payload = {"findings": []}
                parsed = schema.model_validate(raw_payload)
            else:
                parsed = schema.model_validate(
                    {
                        "verdicts": [
                            {
                                "candidate_id": "b000-sec-000",
                                "decision": "APPROVE",
                                "rationale": "Confirmed L5 issue.",
                            }
                        ]
                    }
                )
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=50,
                output_tokens=20,
                total_tokens=70,
                estimated_cost_usd=None,
                latency_ms=5.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            structured_invoker=_raw_payload_invoker,
        )
        state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            pr_id="org/repo#resilient",
            head_sha="sha-resilient",
            trace_id="trace-resilient",
        )
        assert state.status == "completed"
        assert len(state.merged_findings) == 2
        assert state.merged_findings[0].start_line == 5
        assert state.merged_findings[0].severity == "CRITICAL"
        assert state.merged_findings[1].start_line == 10
        assert state.merged_findings[1].suggestion is None
        assert state.batch_results[0].dropped_invalid_findings_count == 1

    @pytest.mark.asyncio
    async def test_critic_verdict_reconciliation_drops_orphans_and_duplicates(self) -> None:
        diff = _make_patch_diff([("src/reconcile.py", ["    x = eval(user_input)"])])

        async def _invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            if role == "security_auditor":
                parsed = schema.model_validate(
                    {
                        "findings": [
                            {
                                "file_path": "src/reconcile.py",
                                "start_line": 2,
                                "end_line": 2,
                                "severity": "CRITICAL",
                                "category": "injection",
                                "title": "Eval injection",
                                "evidence": "Direct eval on user_input.",
                            }
                        ]
                    }
                )
            elif role == "logic_performance_auditor":
                parsed = schema.model_validate({"findings": []})
            else:
                parsed = schema.model_validate(
                    {
                        "verdicts": [
                            {
                                "candidate_id": "b000-sec-000",
                                "decision": "APPROVE",
                                "rationale": "Valid first verdict.",
                            },
                            {
                                "candidate_id": "b000-sec-000",
                                "decision": "REJECT",
                                "rationale": "Duplicate verdict for same ID.",
                            },
                            {
                                "candidate_id": "b000-sec-999",
                                "decision": "APPROVE",
                                "rationale": "Hallucinated orphan ID.",
                            },
                        ]
                    }
                )
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=40,
                output_tokens=15,
                total_tokens=55,
                estimated_cost_usd=None,
                latency_ms=4.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            structured_invoker=_invoker,
        )
        state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            pr_id="org/repo#reconcile",
            head_sha="sha-rec",
            trace_id="trace-rec",
        )
        assert len(state.merged_verdicts) == 1
        assert state.merged_verdicts[0].candidate_id == "b000-sec-000"
        assert state.merged_verdicts[0].decision == "APPROVE"
        assert state.batch_results[0].dropped_invalid_verdicts_count == 2

    @pytest.mark.asyncio
    async def test_only_oversized_unreviewable_diff_returns_unreviewable_only_status(self) -> None:
        huge_line = "    payload = '" + ("X" * 1600) + "'"
        diff = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/blob_const.py",
                    patch=f"@@ -0,0 +1,1 @@\n+{huge_line}\n",
                    status="added",
                )
            ]
        )
        calls = 0

        async def _unexpected(*_: Any) -> Any:
            nonlocal calls
            calls += 1
            raise AssertionError("LLM must not be called when only unreviewable segments exist")

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            structured_invoker=_unexpected,
        )
        cfg = ReviewGraphConfig(
            batch_diff_token_limit=120,
            max_request_tokens=2_000,
            output_reserve_tokens=200,
        )
        state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            graph_config=cfg,
            pr_id="org/repo#unrev",
            head_sha="sha-unrev",
            trace_id="trace-unrev",
        )
        assert calls == 0
        assert state.status == "unreviewable_only"
        assert len(state.unreviewable_segments) == 1

    @pytest.mark.asyncio
    async def test_unbound_trace_id_generates_distinct_ids_across_runs(self) -> None:
        diff = _make_patch_diff([("src/small.py", ["    return True"])])

        async def _ok(*_: Any) -> StructuredInvocationResult[Any]:
            return StructuredInvocationResult(
                parsed=AuditorOutput(findings=[])
                if _[0] != "critic"
                else CriticOutput(verdicts=[]),
                model_name="gemini-2.5-flash",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=None,
                latency_ms=1.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            structured_invoker=_ok,
        )
        clear_trace_context()
        try:
            r1 = await run_pr_review_graph(
                parsed_diff=diff, llm_factory=factory, pr_id="org/repo#1", head_sha="aaa"
            )
            r2 = await run_pr_review_graph(
                parsed_diff=diff, llm_factory=factory, pr_id="org/repo#2", head_sha="bbb"
            )
        finally:
            clear_trace_context()

        assert r1.trace_id != "-"
        assert r2.trace_id != "-"
        assert r1.trace_id != r2.trace_id
        assert r1.telemetry.trace_id is not None
        assert r2.telemetry.trace_id is not None
        assert r1.telemetry.trace_id != r2.telemetry.trace_id


# ---------------------------------------------------------------------------
# 6. Langfuse Observability, Cost Estimation, NFR-3 Masking & Degradation
# ---------------------------------------------------------------------------


class TestLangfuseObservabilityAndNFR3:
    def test_model_cost_estimation_and_unknown_model_handling(self) -> None:
        flash_cost = estimate_model_cost_usd(
            "gemini-2.5-flash", input_tokens=1_000, output_tokens=500
        )
        assert flash_cost is not None
        assert flash_cost["total"] > 0.0
        assert pytest.approx(flash_cost["total"], rel=1e-6) == (
            flash_cost["input"] + flash_cost["output"]
        )

        pro_cost = estimate_model_cost_usd(
            "models/gemini-2.5-pro", input_tokens=1_000, output_tokens=500
        )
        assert pro_cost is not None
        assert pro_cost["total"] > flash_cost["total"]

        assert (
            estimate_model_cost_usd("unknown-custom-model", input_tokens=100, output_tokens=50)
            is None
        )

    def test_sanitize_telemetry_value_strips_code_and_secrets(self) -> None:
        raw = {
            "pr_id": "org/repo#17",
            "batch_index": 0,
            "annotated_diff": "L001 | + api_key = 'AIzaSySECRET1234567890'",
            "prompt": "Review this secret function def steal(): pass",
            "evidence": "raw source snippet",
        }
        cleaned = sanitize_telemetry_value(raw)
        assert cleaned["pr_id"] == "org/repo#17"
        assert cleaned["batch_index"] == 0
        serialized = json.dumps(cleaned)
        assert "AIzaSySECRET" not in serialized
        assert "def steal" not in serialized
        assert "raw source snippet" not in serialized

    @pytest.mark.asyncio
    async def test_langfuse_hierarchical_trace_exports_metrics_cost_and_zero_canary_leaks(
        self,
    ) -> None:
        canary_code = "CANARY_RAW_SOURCE_CODE_LINE_999"
        canary_evidence = "CANARY_EVIDENCE_CODE_LEAK_888"
        canary_suggestion = "CANARY_SUGGESTION_PATCH_777"
        canary_secret = "AIzaSyCANARYSECRETKEY1234567890"

        diff = _make_patch_diff(
            [("src/secret_mod.py", [f"    token = '{canary_code}'  # {canary_secret}"])]
        )
        exporter = _RecordingSpanExporter()

        async def _fake_model_handler(
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            if schema in {AuditorOutput, LLMAuditorDraftOutput}:
                return {
                    "parsed": LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/secret_mod.py",
                                start_line=2,
                                end_line=2,
                                severity="CRITICAL",
                                category="secrets",
                                title="Hardcoded secret",
                                evidence=f"token leaks {canary_evidence}",
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=2,
                                    end_line=2,
                                    replacement_code=canary_suggestion,
                                ),
                            )
                        ]
                    ),
                    "usage": {"input_tokens": 500, "output_tokens": 120, "total_tokens": 620},
                    "raw_content": f"{canary_evidence} {canary_suggestion} {canary_secret}",
                }
            return {
                "parsed": LLMCriticDraftOutput(
                    verdicts=[
                        LLMCriticVerdictDraft(
                            candidate_id="b000-sec-000",
                            decision="APPROVE",
                            rationale=f"Confirmed {canary_evidence}",
                        )
                    ]
                ),
                "usage": {"input_tokens": 400, "output_tokens": 90, "total_tokens": 490},
                "raw_content": f"Critic output {canary_evidence}",
            }

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            critic_model="gemini-2.5-pro",
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-test-unit-001",
            langfuse_secret_key=SecretStr("sk-lf-test-unit-001"),
            langfuse_host="https://cloud.langfuse.com",
        )
        factory = LLMFactory(
            settings=settings,
            chat_model_builder=lambda role, model_name: _FakeStructuredChatModel(
                model_name=model_name,
                handler=_fake_model_handler,
            ),
        )

        final_state = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=factory,
            pr_id="Tieens-04/CodeSight-AI#17",
            head_sha="1234567890ab",
            trace_id="trace-langfuse-nfr3",
            span_exporter=exporter,
        )

        assert final_state.status == "completed"
        assert final_state.telemetry.status == "active"
        assert final_state.telemetry.trace_id is not None
        assert len(exporter.spans) >= 4

        # Single trace ID across all exported spans (hierarchical trace for the PR)
        trace_ids = {span.context.trace_id for span in exporter.spans if span.context is not None}
        assert len(trace_ids) == 1

        # Verify generation spans carry model name, usage tokens, cost, and parent hierarchy
        generation_spans = [
            span
            for span in exporter.spans
            if span.attributes and span.attributes.get("langfuse.observation.type") == "generation"
        ]
        assert len(generation_spans) == 3
        span_by_id = {
            span.context.span_id: span for span in exporter.spans if span.context is not None
        }
        for gen_span in generation_spans:
            assert gen_span.parent is not None
            ancestors: list[str] = []
            cur = span_by_id.get(gen_span.parent.span_id)
            while cur is not None:
                ancestors.append(cur.name)
                cur = span_by_id.get(cur.parent.span_id) if cur.parent is not None else None
            assert "codesight_pr_review" in ancestors
            assert any(
                node in ancestors
                for node in ("security_auditor", "logic_performance_auditor", "critic_synthesis")
            )
            attrs = dict(gen_span.attributes or {})
            assert attrs.get("langfuse.observation.model.name") in {
                "gemini-2.5-flash",
                "gemini-2.5-pro",
            }
            assert "langfuse.observation.usage_details" in attrs
            assert "langfuse.observation.cost_details" in attrs
            cost_obj = json.loads(str(attrs["langfuse.observation.cost_details"]))
            assert cost_obj["total"] > 0.0

        # NFR-3 IRONCLAD CHECK: zero canary code/prompt/evidence/suggestion/secret in ANY span!
        for span in exporter.spans:
            serialized_attrs = json.dumps(dict(span.attributes or {}), default=str)
            assert canary_code not in serialized_attrs
            assert canary_evidence not in serialized_attrs
            assert canary_suggestion not in serialized_attrs
            assert canary_secret not in serialized_attrs
            assert "sk-lf-test-unit-001" not in serialized_attrs

    @pytest.mark.asyncio
    async def test_structured_output_error_masks_raw_error_message_in_exported_span(self) -> None:
        canary_err = "CANARY_VALIDATION_ERROR_RAW_CODE_LEAK_555"
        diff = _make_patch_diff([("src/err.py", [f"    secret = '{canary_err}'"])])
        exporter = _RecordingSpanExporter()

        async def _error_model_handler(
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            raise ValueError(f"Schema validation failed on input_value='{canary_err}'")

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-test-err-001",
            langfuse_secret_key=SecretStr("sk-lf-test-err-001"),
        )
        factory = LLMFactory(
            settings=settings,
            chat_model_builder=lambda role, model_name: _FakeStructuredChatModel(
                model_name=model_name,
                handler=_error_model_handler,
            ),
        )

        with pytest.raises(ValueError):
            await run_pr_review_graph(
                parsed_diff=diff,
                llm_factory=factory,
                pr_id="org/repo#err",
                head_sha="eee111",
                trace_id="trace-err-mask",
                span_exporter=exporter,
            )

        assert len(exporter.spans) >= 1
        for span in exporter.spans:
            serialized_attrs = json.dumps(dict(span.attributes or {}), default=str)
            assert canary_err not in serialized_attrs

    @pytest.mark.asyncio
    async def test_structured_output_parsing_error_raises_sanitized_error_and_masks_raw_content(
        self,
    ) -> None:
        canary_raw = "CANARY_BROKEN_STRUCTURED_RAW_SECRET_444"
        diff = _make_patch_diff([("src/parse_err.py", ["    x = 1"])])
        exporter = _RecordingSpanExporter()

        async def _bad_parse_handler(
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            return {
                "parsed": None,
                "parsing_error": ValueError(f"Bad JSON near '{canary_raw}'"),
                "raw_content": f'{{"broken": "{canary_raw}"}}',
                "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            }

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-test-parse-err-001",
            langfuse_secret_key=SecretStr("sk-lf-test-parse-err-001"),
        )
        factory = LLMFactory(
            settings=settings,
            chat_model_builder=lambda role, model_name: _FakeStructuredChatModel(
                model_name=model_name,
                handler=_bad_parse_handler,
            ),
        )

        with pytest.raises(LLMStructuredOutputError) as exc_info:
            await run_pr_review_graph(
                parsed_diff=diff,
                llm_factory=factory,
                pr_id="org/repo#parse-err",
                head_sha="fff222",
                trace_id="trace-parse-err",
                span_exporter=exporter,
            )

        assert canary_raw not in str(exc_info.value)
        assert len(exporter.spans) >= 1
        for span in exporter.spans:
            serialized_attrs = json.dumps(dict(span.attributes or {}), default=str)
            assert canary_raw not in serialized_attrs

    @pytest.mark.asyncio
    async def test_missing_langfuse_credentials_and_broken_exporter_still_return_review(
        self,
    ) -> None:
        diff = _make_patch_diff([("src/app.py", ["    return 42"])])

        async def _ok_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            parsed: BaseModel = (
                LLMAuditorDraftOutput(findings=[])
                if schema in {AuditorOutput, LLMAuditorDraftOutput}
                else LLMCriticDraftOutput(verdicts=[])
            )
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=50,
                output_tokens=10,
                total_tokens=60,
                estimated_cost_usd=0.00004,
                latency_ms=5.0,
                attempts=1,
            )

        # Case 1: Missing Langfuse keys -> disabled telemetry, review succeeds
        no_lf_settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_public_key=None,
            langfuse_secret_key=None,
        )
        no_lf_factory = LLMFactory(settings=no_lf_settings, structured_invoker=_ok_invoker)
        res_disabled = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=no_lf_factory,
            pr_id="org/repo#no-lf",
            head_sha="111aaa",
            trace_id="trace-no-lf",
        )
        assert res_disabled.status == "completed"
        assert res_disabled.telemetry.status == "disabled"

        # Case 2: Broken Langfuse exporter -> degraded telemetry, review STILL succeeds!
        broken_exporter = _RecordingSpanExporter(fail_on_export=True, fail_on_flush=True)
        lf_settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-test-broken-001",
            langfuse_secret_key=SecretStr("sk-lf-test-broken-001"),
        )
        lf_factory = LLMFactory(settings=lf_settings, structured_invoker=_ok_invoker)
        res_degraded = await run_pr_review_graph(
            parsed_diff=diff,
            llm_factory=lf_factory,
            pr_id="org/repo#broken-lf",
            head_sha="222bbb",
            trace_id="trace-broken-lf",
            span_exporter=broken_exporter,
        )
        assert res_degraded.status == "completed"
        assert res_degraded.telemetry.status == "degraded"
        assert res_degraded.telemetry.error_type == "RuntimeError"

    def test_create_langfuse_session_disabled_when_flag_false(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=False,
            langfuse_public_key="pk-lf-disabled",
            langfuse_secret_key=SecretStr("sk-lf-disabled"),
        )
        session = create_langfuse_trace_session(settings=settings, trace_id="t-off")
        assert session.telemetry_status.status == "disabled"
        assert session.callbacks == ()
        assert session.flush_and_close().status == "disabled"

    def test_observability_masking_and_degradation_edge_cases(self) -> None:
        from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

        # Normalize 32-hex, arbitrary seed, hyphen fallback, and None trace IDs
        hex32 = "a" * 32
        assert _normalize_langfuse_trace_id(hex32) == hex32
        assert len(_normalize_langfuse_trace_id("custom-seed")) == 32
        assert len(_normalize_langfuse_trace_id(None)) == 32
        assert _normalize_langfuse_trace_id("-") != _normalize_langfuse_trace_id("-")

        # Sanitize BaseModel, nested sequences, secrets, and raw strings
        state = PRReviewState(pr_id="org/repo#1", head_sha="abc", trace_id="t1")
        model_summary = sanitize_telemetry_value(state)
        assert model_summary["type"] == "PRReviewState"
        assert model_summary["redacted"] is True
        assert sanitize_telemetry_value(["secret_item"]) == {"redacted": True, "count": 1}
        assert sanitize_telemetry_value("raw prompt code") == {"redacted": True, "type": "str"}
        assert sanitize_telemetry_value({"pr_id": "AIzaSySECRETKEY99999999"}) == {
            "pr_id": "[REDACTED]"
        }

        # Direct mask_otel_spans_allowlist check across every attribute branch
        sid = OtelSpanIdentifier(trace_id=hex32, span_id="1" * 16)
        span_data = OtelSpanData(
            trace_id=hex32,
            span_id="1" * 16,
            parent_span_id=None,
            name="gen",
            instrumentation_scope_name="langfuse-sdk",
            instrumentation_scope_version="5.2.0",
            attributes={
                "langfuse.trace.name": "AIzaSyLEAKEDKEY12345678",
                "langfuse.observation.input": "not-json raw diff",
                "langfuse.observation.output": json.dumps({"finding_count": 2, "raw_code": "x"}),
                "langfuse.observation.status_message": "ValueError: secret raw text",
                "langfuse.observation.metadata.pr_id": "org/repo#1",
                "langfuse.observation.metadata.role": "line1\nline2",
                "langfuse.observation.metadata.unapproved_key": "drop-me",
                "langfuse.observation.model.name": "gemini-2.5-flash",
                "langfuse.observation.usage_details": json.dumps({"input": 100, "output": 50}),
                "langfuse.observation.cost_details": "bad-json",
                "exception.stacktrace": "Traceback secret",
            },
            resource_attributes={},
        )
        masked = mask_otel_spans_allowlist(MaskOtelSpansParams(spans={sid: span_data}))
        patch_obj = masked.span_patches[sid]
        assert patch_obj is not None
        assert "langfuse.trace.name" in patch_obj.delete_attributes
        assert "langfuse.observation.metadata.role" in patch_obj.delete_attributes
        assert "langfuse.observation.metadata.unapproved_key" in patch_obj.delete_attributes
        assert "exception.stacktrace" in patch_obj.delete_attributes
        assert patch_obj.set_attributes["langfuse.observation.status_message"] == "ValueError"
        assert "langfuse.observation.cost_details" in patch_obj.set_attributes
        assert "raw diff" not in str(patch_obj.set_attributes["langfuse.observation.input"])
        assert "raw_code" not in str(patch_obj.set_attributes["langfuse.observation.output"])

        # Resilient span exporter catches export/flush/shutdown failures cleanly
        class _ExplodingExporter(SpanExporter):
            def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
                raise RuntimeError("export boom")

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                raise TimeoutError("flush boom")

            def shutdown(self) -> None:
                raise OSError("shutdown boom")

        resilient = _ResilientSpanExporter(_ExplodingExporter())
        assert resilient.export([]) == SpanExportResult.FAILURE
        assert resilient.last_error_type == "RuntimeError"
        assert resilient.force_flush() is False
        assert resilient.last_error_type == "TimeoutError"
        resilient.shutdown()

        # Scrub non-chat Generation cleanly
        plain_res = LLMResult(
            generations=[[Generation(text="secret completion", generation_info={"x": 1})]],
            llm_output={"model_name": "gemini-2.5-flash", "extra": "drop"},
        )
        scrubbed = CodeSightLangfuseCallbackHandler._scrub_llm_result(plain_res)
        assert scrubbed.generations[0][0].text == '{"redacted": true}'
        assert scrubbed.llm_output == {"model_name": "gemini-2.5-flash"}

        # Init failure degrades session cleanly without raising
        lf_cfg = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-init-fail",
            langfuse_secret_key=SecretStr("sk-lf-init-fail"),
        )
        with patch("codesight.core.observability.Langfuse", side_effect=RuntimeError("init boom")):
            degraded_session = create_langfuse_trace_session(settings=lf_cfg, trace_id=hex32)
        assert degraded_session.telemetry_status.status == "degraded"
        assert degraded_session.telemetry_status.error_type == "RuntimeError"

    def test_create_langfuse_session_does_not_call_get_trace_url_and_reuses_cached_client(
        self,
    ) -> None:
        from langfuse._client.resource_manager import LangfuseResourceManager

        from codesight.core import observability as obs_mod

        pk = "pk-lf-cache-001"
        cfg = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key=pk,
            langfuse_secret_key=SecretStr("sk-lf-cache-001"),
            langfuse_host="http://127.0.0.1:9",
        )
        dummy_exporter = _RecordingSpanExporter()
        if hasattr(obs_mod, "reset_cached_langfuse_clients"):
            obs_mod.reset_cached_langfuse_clients()
        LangfuseResourceManager._instances.pop(pk, None)
        try:
            with (
                patch(
                    "langfuse.Langfuse.get_trace_url",
                    side_effect=AssertionError("get_trace_url must not be called on hot path"),
                ),
                patch(
                    "codesight.core.observability.OTLPSpanExporter",
                    return_value=dummy_exporter,
                    create=True,
                ),
                patch(
                    "langfuse._client.span_processor.OTLPSpanExporter",
                    return_value=dummy_exporter,
                ),
            ):
                s1 = create_langfuse_trace_session(settings=cfg, trace_id="trace-cache-1")
                s2 = create_langfuse_trace_session(settings=cfg, trace_id="trace-cache-2")
                assert s1.telemetry_status.status == "active"
                assert s1.telemetry_status.trace_url is None
                assert s2.telemetry_status.status == "active"
                assert s1._client is not None
                assert s1._client is s2._client
        finally:
            if hasattr(obs_mod, "reset_cached_langfuse_clients"):
                obs_mod.reset_cached_langfuse_clients()
            LangfuseResourceManager._instances.pop(pk, None)

    @pytest.mark.asyncio
    async def test_slow_langfuse_flush_does_not_block_event_loop_and_degrades_on_timeout(
        self,
    ) -> None:
        diff = _make_patch_diff([("src/slow_flush.py", ["    return 1"])])
        slow_exporter = _RecordingSpanExporter(export_delay_seconds=2.0)

        async def _ok_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            parsed: BaseModel = (
                AuditorOutput(findings=[]) if role != "critic" else CriticOutput(verdicts=[])
            )
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=None,
                latency_ms=1.0,
                attempts=1,
            )

        cfg = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-slow-flush-001",
            langfuse_secret_key=SecretStr("sk-lf-slow-flush-001"),
        )
        factory = LLMFactory(settings=cfg, structured_invoker=_ok_invoker)

        heartbeat_ticks = 0
        stop_heartbeat = asyncio.Event()

        async def _heartbeat() -> None:
            nonlocal heartbeat_ticks
            while not stop_heartbeat.is_set():
                heartbeat_ticks += 1
                await asyncio.sleep(0.02)

        async def _run_review() -> PRReviewState:
            try:
                return await run_pr_review_graph(
                    parsed_diff=diff,
                    llm_factory=factory,
                    pr_id="org/repo#slow-flush",
                    head_sha="sha-slow",
                    trace_id="trace-slow-flush",
                    span_exporter=slow_exporter,
                    telemetry_flush_timeout_seconds=0.1,
                )
            finally:
                stop_heartbeat.set()

        started = time.perf_counter()
        final_state, _ = await asyncio.gather(_run_review(), _heartbeat())
        elapsed = time.perf_counter() - started

        assert elapsed < 0.8
        assert heartbeat_ticks >= 3
        assert final_state.status == "completed"
        assert final_state.telemetry.status == "degraded"
        assert final_state.telemetry.error_type == "TimeoutError"

    @pytest.mark.asyncio
    async def test_production_otlp_export_failure_sets_telemetry_degraded(self) -> None:
        from langfuse._client.resource_manager import LangfuseResourceManager

        from codesight.core import observability as obs_mod

        pk = "pk-lf-prod-fail-001"
        diff = _make_patch_diff([("src/prod_fail.py", ["    return 7"])])
        failing_otlp = _RecordingSpanExporter(
            export_result=SpanExportResult.FAILURE,
            flush_return=False,
        )

        async def _ok_invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            parsed: BaseModel = (
                AuditorOutput(findings=[]) if role != "critic" else CriticOutput(verdicts=[])
            )
            return StructuredInvocationResult(
                parsed=parsed,
                model_name=model_name,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=None,
                latency_ms=1.0,
                attempts=1,
            )

        cfg = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key=pk,
            langfuse_secret_key=SecretStr("sk-lf-prod-fail-001"),
            langfuse_host="http://127.0.0.1:9",
        )
        factory = LLMFactory(settings=cfg, structured_invoker=_ok_invoker)

        if hasattr(obs_mod, "reset_cached_langfuse_clients"):
            obs_mod.reset_cached_langfuse_clients()
        LangfuseResourceManager._instances.pop(pk, None)
        try:
            with (
                patch("langfuse.Langfuse.get_trace_url", return_value=None),
                patch(
                    "codesight.core.observability.OTLPSpanExporter",
                    return_value=failing_otlp,
                    create=True,
                ),
                patch(
                    "langfuse._client.span_processor.OTLPSpanExporter",
                    return_value=failing_otlp,
                ),
            ):
                final_state = await run_pr_review_graph(
                    parsed_diff=diff,
                    llm_factory=factory,
                    pr_id="org/repo#prod-fail",
                    head_sha="sha-prod-fail",
                    trace_id="trace-prod-fail",
                )
        finally:
            if hasattr(obs_mod, "reset_cached_langfuse_clients"):
                obs_mod.reset_cached_langfuse_clients()
            LangfuseResourceManager._instances.pop(pk, None)

        assert final_state.status == "completed"
        assert final_state.telemetry.status == "degraded"
        assert final_state.telemetry.flushed is False
        assert final_state.telemetry.error_type == "SpanExportFailure"

    def test_on_chain_end_strips_inputs_and_metadata_allowlist_stays_consistent(self) -> None:
        from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

        cfg = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-chain-end-001",
            langfuse_secret_key=SecretStr("sk-lf-chain-end-001"),
        )
        session = create_langfuse_trace_session(
            settings=cfg,
            trace_id="trace-chain-end",
            span_exporter=_RecordingSpanExporter(),
        )
        try:
            assert len(session.callbacks) == 1
            handler = session.callbacks[0]
            captured_kwargs: dict[str, Any] = {}

            def _fake_super_on_chain_end(
                self_cb: Any, outputs: Any, *, run_id: Any, parent_run_id: Any = None, **kw: Any
            ) -> None:
                captured_kwargs.update(kw)

            with patch.object(LangfuseCallbackHandler, "on_chain_end", _fake_super_on_chain_end):
                handler.on_chain_end(
                    {"finding_count": 1},
                    run_id=uuid.uuid4(),
                    inputs={"annotated_diff": "CANARY_RAW_CHAIN_INPUT"},
                )

            assert "inputs" not in captured_kwargs

            sanitized_meta = sanitize_telemetry_value(
                {
                    "langgraph_node": "critic_synthesis",
                    "ls_model_name": "gemini-2.5-flash",
                    "raw_code": "CANARY_SECRET_CODE",
                }
            )
            assert sanitized_meta["langgraph_node"] == "critic_synthesis"
            assert sanitized_meta["ls_model_name"] == "gemini-2.5-flash"
            assert "raw_code" not in sanitized_meta
        finally:
            session.flush_and_close()

    def test_existing_unmasked_langfuse_singleton_degrades_fail_closed(self) -> None:
        from langfuse import Langfuse
        from langfuse._client.resource_manager import LangfuseResourceManager
        from opentelemetry.sdk.trace import TracerProvider

        from codesight.core import observability as obs_mod

        pk = "pk-lf-unmasked-001"
        if hasattr(obs_mod, "reset_cached_langfuse_clients"):
            obs_mod.reset_cached_langfuse_clients()
        LangfuseResourceManager._instances.pop(pk, None)
        raw_client = Langfuse(
            public_key=pk,
            secret_key="sk-lf-unmasked-001",
            tracer_provider=TracerProvider(),
            span_exporter=_RecordingSpanExporter(),
        )
        try:
            cfg = Settings(
                _env_file=None,  # type: ignore[call-arg]
                langfuse_tracing_enabled=True,
                langfuse_public_key=pk,
                langfuse_secret_key=SecretStr("sk-lf-unmasked-001"),
            )
            session = create_langfuse_trace_session(settings=cfg, trace_id="trace-unmasked")
            assert session.telemetry_status.status == "degraded"
            assert session.telemetry_status.active is False
            assert session.callbacks == ()
        finally:
            raw_client.shutdown()
            if hasattr(obs_mod, "reset_cached_langfuse_clients"):
                obs_mod.reset_cached_langfuse_clients()
            LangfuseResourceManager._instances.pop(pk, None)
