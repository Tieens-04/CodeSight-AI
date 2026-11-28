"""Integration tests for CA-19: Parallel Security // Perf-Arch Fan-out and Live Gemini gate."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import BaseModel, SecretStr
from tree_sitter import Parser

from codesight.agents.graph import (
    ReviewGraphConfig,
    run_pr_review_graph,
    split_parsed_diff_into_batches,
)
from codesight.agents.llm_factory import LLMFactory, StructuredInvocationResult
from codesight.agents.nodes.context_builder import PRReviewContext
from codesight.agents.schemas import (
    FindingCandidate,
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
    LLMRole,
)
from codesight.ast_engine._python_ast import PYTHON_LANGUAGE
from codesight.ast_engine._typescript_ast import TSX_LANGUAGE, TYPESCRIPT_LANGUAGE
from codesight.ast_engine.models import (
    CodeSymbolChunk,
    ComplexityDelta,
    ComplexityDiffSummary,
    FunctionComplexity,
    PRFileContext,
)
from codesight.config_reader.repo_config import RepoConfig
from codesight.core.config import Settings
from codesight.diff.file_filter import estimate_tokens
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.retriever import (
    AssembledPRContext,
    HybridRetrievalResult,
    RetrievedContextCandidate,
)
from codesight.rag.token_packer import (
    PackedContextBlock,
    PackedContextBundle,
    TokenBudgetMetrics,
)
from codesight.vcs.blob_fetcher import BlobBatchFetchReport
from codesight.vcs.github_publisher import ReviewCommentDraft, partition_review_comments


@dataclass(frozen=True, slots=True)
class GroundTruthLabel:
    """Independent ground-truth expectation evaluated only by the test harness."""

    name: str
    file_path: str
    allowed_spans: frozenset[tuple[int, int]]
    category_keywords: tuple[str, ...]
    expected_role: str | None = None


_HERMETIC_LABELS: tuple[GroundTruthLabel, ...] = (
    GroundTruthLabel(
        name="python_n_plus_one",
        file_path="src/orders/loader.py",
        allowed_spans=frozenset({(3, 4), (4, 4), (3, 5), (4, 5)}),
        category_keywords=("n_plus_one", "n+1", "query", "perf"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="python_blocking_io",
        file_path="src/webhooks/notifier.py",
        allowed_spans=frozenset({(3, 3), (4, 4), (3, 4)}),
        category_keywords=("blocking", "io", "sync", "event_loop", "perf"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="ts_sequential_await",
        file_path="src/ts/syncBatch.ts",
        allowed_spans=frozenset({(3, 5), (4, 4), (3, 4)}),
        category_keywords=("blocking", "await", "promise", "sequential", "perf"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="python_quadratic",
        file_path="src/analytics/dedupe.py",
        allowed_spans=frozenset({(3, 5), (4, 4), (4, 5)}),
        category_keywords=("complex", "quadratic", "o(n", "algorithm", "perf"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="tsx_listener_leak",
        file_path="src/web/ViewportPanel.tsx",
        allowed_spans=frozenset({(3, 5), (4, 4), (3, 4)}),
        category_keywords=("leak", "listener", "cleanup", "resource", "effect"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="adr_layer_violation",
        file_path="src/api/orders_router.py",
        allowed_spans=frozenset({(3, 3), (4, 4), (3, 4)}),
        category_keywords=("arch", "adr", "layer", "violation"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="broken_caller_contract",
        file_path="src/billing/pricing.py",
        allowed_spans=frozenset({(1, 1), (1, 2)}),
        category_keywords=("caller", "contract", "signature", "arch", "break"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="security_sqli",
        file_path="src/users/auth_repo.py",
        allowed_spans=frozenset({(3, 3), (4, 4), (3, 4)}),
        category_keywords=("sql", "injection", "sqli"),
        expected_role="security_auditor",
    ),
)

_HERMETIC_SAFE_FILES: frozenset[str] = frozenset(
    {
        "src/orders/safe_batch_loader.py",
        "src/web/SafeListenerCard.tsx",
    }
)

_HERMETIC_FULL_SOURCES: dict[str, str] = {
    "src/orders/loader.py": (
        "import sqlite3\n"
        "def load_orders(conn: sqlite3.Connection, order_ids: list[str]) -> list[object]:\n"
        "    rows = []\n"
        "    for order_id in order_ids:\n"
        "        rows.append("
        'conn.execute("SELECT id, total FROM orders WHERE id = ?", (order_id,)).fetchone())\n'
        "    return rows\n"
    ),
    "src/webhooks/notifier.py": (
        "import time\n"
        "async def send_webhook_alert(target_url: str) -> str:\n"
        "    time.sleep(2.0)\n"
        "    return target_url\n"
    ),
    "src/ts/syncBatch.ts": (
        "export async function syncItems(ids: string[], fn: (id: string) => Promise<string>) {\n"
        "  const out: string[] = [];\n"
        "  for (const id of ids) {\n"
        "    out.push(await fn(id));\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    ),
    "src/analytics/dedupe.py": (
        "def collect_matches(items: list[str], seen: list[str]) -> list[str]:\n"
        "    matched: list[str] = []\n"
        "    for item in items:\n"
        "        if item in seen:\n"
        "            matched.append(item)\n"
        "    return matched\n"
    ),
    "src/web/ViewportPanel.tsx": (
        "import { useEffect } from 'react';\n"
        "export function ViewportPanel({ onResize }: { onResize: () => void }) {\n"
        "  useEffect(() => {\n"
        "    window.addEventListener('resize', onResize);\n"
        "  }, [onResize]);\n"
        "  return <div />;\n"
        "}\n"
    ),
    "src/api/orders_router.py": (
        "def get_order_endpoint(db_session, order_id: str) -> object:\n"
        "    if not order_id:\n"
        "        return None\n"
        '    return db_session.execute("SELECT id FROM orders WHERE id = ?", (order_id,))\n'
    ),
    "src/billing/pricing.py": (
        "def calculate_total(subtotal: float) -> float:\n    return round(subtotal, 2)\n"
    ),
    "src/users/auth_repo.py": (
        "import sqlite3\n"
        "def find_account(conn: sqlite3.Connection, username: str) -> object:\n"
        "    query = f\"SELECT id FROM accounts WHERE username = '{username}'\"\n"
        "    return conn.execute(query).fetchone()\n"
    ),
}


def _shared_ac2_entries() -> list[PatchFileEntry]:
    return [
        PatchFileEntry(
            filename="src/orders/loader.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,6 @@",
                    " import sqlite3",
                    (
                        " def load_orders("
                        "conn: sqlite3.Connection, order_ids: list[str]) -> list[object]:"
                    ),
                    "+    rows = []",
                    "+    for order_id in order_ids:",
                    (
                        "+        rows.append(conn.execute("
                        '"SELECT id, total FROM orders WHERE id = ?", (order_id,)).fetchone())'
                    ),
                    "+    return rows",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/webhooks/notifier.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,4 @@",
                    " import time",
                    " async def send_webhook_alert(target_url: str) -> str:",
                    "+    time.sleep(2.0)",
                    "+    return target_url",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/billing/pricing.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,2 @@",
                    "-def calculate_total(subtotal: float, tax_rate: float) -> float:",
                    "-    return round(subtotal * (1.0 + tax_rate), 2)",
                    "+def calculate_total(subtotal: float) -> float:",
                    "+    return round(subtotal, 2)",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/users/auth_repo.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,4 @@",
                    " import sqlite3",
                    " def find_account(conn: sqlite3.Connection, username: str) -> object:",
                    "+    query = f\"SELECT id FROM accounts WHERE username = '{username}'\"",
                    "+    return conn.execute(query).fetchone()",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/orders/safe_batch_loader.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,3 @@",
                    " def load_orders_by_batch(conn, batch_id: str) -> list[object]:",
                    (
                        "+    return conn.execute("
                        '"SELECT id, total FROM orders WHERE batch_id = ?", (batch_id,)).fetchall()'
                    ),
                ]
            )
            + "\n",
        ),
    ]


def _build_checkout_caller_block() -> PackedContextBlock:
    return PackedContextBlock(
        tier="caller",
        file_path="src/checkout/service.py",
        start_line=14,
        end_line=18,
        symbol_name="finalize_checkout",
        symbol_kind="function",
        signature="def finalize_checkout(subtotal: float, tax_rate: float) -> float",
        rendered_text=(
            "### [Direct Caller] src/checkout/service.py (L14-L18)\n"
            "L14: def finalize_checkout(subtotal: float, tax_rate: float) -> float:\n"
            "L15:     return calculate_total(subtotal, tax_rate)"
        ),
        token_count=36,
        truncated=False,
    )


def _build_hermetic_bundle() -> tuple[ParsedPullRequestDiff, PRReviewContext]:
    entries = [
        *_shared_ac2_entries(),
        PatchFileEntry(
            filename="src/ts/syncBatch.ts",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,7 @@",
                    (
                        " export async function syncItems("
                        "ids: string[], fn: (id: string) => Promise<string>) {"
                    ),
                    "   const out: string[] = [];",
                    "+  for (const id of ids) {",
                    "+    out.push(await fn(id));",
                    "+  }",
                    "   return out;",
                    " }",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/analytics/dedupe.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,6 @@",
                    " def collect_matches(items: list[str], seen: list[str]) -> list[str]:",
                    "     matched: list[str] = []",
                    "+    for item in items:",
                    "+        if item in seen:",
                    "+            matched.append(item)",
                    "     return matched",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/web/ViewportPanel.tsx",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,6 @@",
                    " import { useEffect } from 'react';",
                    " export function ViewportPanel({ onResize }: { onResize: () => void }) {",
                    "+  useEffect(() => {",
                    "+    window.addEventListener('resize', onResize);",
                    "+  }, [onResize]);",
                    "   return <div />;",
                    " }",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/api/orders_router.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,4 @@",
                    " def get_order_endpoint(db_session, order_id: str) -> object:",
                    "     if not order_id:",
                    "         return None",
                    (
                        "+    return db_session.execute("
                        '"SELECT id FROM orders WHERE id = ?", (order_id,))'
                    ),
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/web/SafeListenerCard.tsx",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,6 @@",
                    " export function SafeListenerCard({ onResize }: { onResize: () => void }) {",
                    "+  useEffect(() => {",
                    "+    window.addEventListener('resize', onResize);",
                    "+    return () => window.removeEventListener('resize', onResize);",
                    "+  }, [onResize]);",
                    "   return <div />;",
                ]
            )
            + "\n",
        ),
    ]
    parsed = SmartDiffParser().parse_patch_entries(entries)

    file_contexts = (
        PRFileContext(
            file_path="src/orders/loader.py",
            mode="ast",
            language="python",
            touched_chunks=(
                CodeSymbolChunk(
                    file_path="src/orders/loader.py",
                    language="python",
                    symbol_name="load_orders",
                    qualified_name="load_orders",
                    symbol_kind="function",
                    start_line=2,
                    end_line=6,
                    signature=(
                        "def load_orders(conn: sqlite3.Connection, order_ids: list[str]) "
                        "-> list[object]"
                    ),
                ),
            ),
            complexity=ComplexityDiffSummary(
                deltas=(
                    ComplexityDelta(
                        symbol_name="load_orders",
                        qualified_name="load_orders",
                        change_kind="modified",
                        before_cc=1,
                        after_cc=3,
                        delta_cc=2,
                        after_metrics=FunctionComplexity(
                            symbol_name="load_orders",
                            qualified_name="load_orders",
                            symbol_kind="function",
                            start_line=2,
                            end_line=6,
                            line_count=5,
                            cyclomatic_complexity=3,
                            max_nesting_depth=2,
                            parameter_count=2,
                        ),
                    ),
                ),
                net_delta_cc=2,
                max_delta_cc=2,
                max_after_cc=3,
            ),
        ),
        PRFileContext(
            file_path="src/billing/pricing.py",
            mode="ast",
            language="python",
            touched_chunks=(
                CodeSymbolChunk(
                    file_path="src/billing/pricing.py",
                    language="python",
                    symbol_name="calculate_total",
                    qualified_name="calculate_total",
                    symbol_kind="function",
                    start_line=1,
                    end_line=2,
                    signature="def calculate_total(subtotal: float) -> float",
                ),
            ),
        ),
    )

    caller_block = _build_checkout_caller_block()
    adr_block = PackedContextBlock(
        tier="architecture_doc",
        file_path="docs/adr/002-layering.md",
        start_line=1,
        end_line=5,
        symbol_name="ADR-002",
        symbol_kind="section",
        signature="ADR-002 Layering",
        rendered_text=(
            "### [Architecture Doc] docs/adr/002-layering.md (L1-L5)\n"
            "Handlers in src/api/* must call repository functions rather than executing SQL "
            "directly on db_session."
        ),
        token_count=30,
        truncated=False,
    )
    packed_bundle = PackedContextBundle(
        blocks=(caller_block, adr_block),
        rendered_context=f"{caller_block.rendered_text}\n\n{adr_block.rendered_text}",
        metrics=TokenBudgetMetrics(
            budget_tokens=3000,
            packed_aux_tokens=66,
            diff_tokens=estimate_tokens(parsed.annotated_diff),
            skipped_noise_tokens=0,
            baseline_tokens=600,
            codesight_tokens=300,
            saved_tokens=300,
            tokens_saved_pct=50.0,
            aux_tokens_saved_pct=50.0,
            unique_baseline_files=10,
            packed_blocks_count=2,
            truncated_blocks_count=0,
            dropped_blocks_count=0,
        ),
    )
    assembled = AssembledPRContext(
        retrieval=HybridRetrievalResult(
            enclosing=(),
            callers=(
                RetrievedContextCandidate(
                    tier="caller",
                    point_id="p-caller-1",
                    repo_full_name="Tieens-04/CodeSight-AI",
                    file_path="src/checkout/service.py",
                    blob_sha="a" * 40,
                    ref=None,
                    symbol_name="finalize_checkout",
                    symbol_kind="function",
                    signature="def finalize_checkout(subtotal: float, tax_rate: float) -> float",
                    start_line=14,
                    end_line=18,
                    relevance_score=5.0,
                    matched_symbols=("calculate_total",),
                ),
            ),
            callees=(),
            architecture_docs=(),
            query_text="calculate_total",
            doc_search_degraded=False,
            qdrant_ms=1.0,
        ),
        blob_report=BlobBatchFetchReport(
            slices=(),
            unique_blobs_requested=1,
            unique_blobs_fetched=1,
            skipped_blobs=0,
            full_file_tokens_by_path={},
            elapsed_ms=2.0,
        ),
        packed_bundle=packed_bundle,
        qdrant_ms=1.0,
        blob_fetch_ms=1.0,
        pack_ms=1.0,
        total_ms=3.0,
    )
    return parsed, PRReviewContext(
        file_contexts=file_contexts,
        assembled_context=assembled,
        repo_config=RepoConfig(custom_rules="Avoid blocking I/O in async functions."),
    )


def _assert_matches_labels(
    findings: Sequence[FindingCandidate],
    labels: Sequence[GroundTruthLabel],
) -> None:
    matched: dict[str, list[FindingCandidate]] = {lbl.name: [] for lbl in labels}
    for finding in findings:
        span = (finding.start_line, finding.end_line)
        matching_labels = [lbl for lbl in labels if lbl.file_path == finding.file_path]
        assert matching_labels, f"Unexpected finding on unlabeled file: {finding.file_path}"
        label = matching_labels[0]
        assert span in label.allowed_spans, (
            f"Finding {finding.file_path}:{span} out-of-span for {label.name}: "
            f"{label.allowed_spans}"
        )
        if label.expected_role is not None:
            assert finding.role == label.expected_role
        assert any(
            kw in finding.category.lower() or kw in finding.title.lower()
            for kw in label.category_keywords
        ), (
            f"Finding {finding.file_path} ({finding.category}: {finding.title!r}) "
            f"missed expected keywords {label.category_keywords}"
        )
        matched[label.name].append(finding)

    for label in labels:
        hits = matched[label.name]
        assert len(hits) == 1, f"Expected exactly 1 match for {label.name}, got {len(hits)}"


def _is_tree_sitter_valid(file_path: str, finding: FindingCandidate) -> bool:
    assert finding.suggestion is not None
    full_source = _HERMETIC_FULL_SOURCES[file_path]
    lang = (
        TSX_LANGUAGE
        if file_path.endswith(".tsx")
        else (TYPESCRIPT_LANGUAGE if file_path.endswith(".ts") else PYTHON_LANGUAGE)
    )
    parser = Parser(lang)
    lines = full_source.splitlines(keepends=True)
    replacement = [f"{ln}\n" for ln in finding.suggestion.replacement_code.splitlines()]
    patched = "".join(
        lines[: finding.suggestion.start_line - 1]
        + replacement
        + lines[finding.suggestion.end_line :]
    )
    return not parser.parse(patched.encode("utf-8")).root_node.has_error


class _RecordingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _FakeStructuredChatModel(BaseChatModel):
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
        ai_msg = AIMessage(
            content=payload.get("raw_content", json.dumps({"ok": True})),
            usage_metadata=payload.get(
                "usage", {"input_tokens": 180, "output_tokens": 60, "total_tokens": 240}
            ),
            response_metadata={"model_name": self.model_name},
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=ai_msg,
                    generation_info={"parsed": payload["parsed"], "parsing_error": None},
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

        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_run)


class _TimedChatModelProxy(BaseChatModel):
    """Wraps a real Gemini chat model to record wall-clock overlap without altering calls."""

    inner: Any
    role: LLMRole
    record_fn: Callable[[LLMRole, float, float], None]

    @property
    def _llm_type(self) -> str:
        return "timed-proxy-chat"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("Use async invocation in tests.")

    def with_structured_output(
        self,
        schema: dict[str, Any] | type[BaseModel],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        inner_runnable = self.inner.with_structured_output(
            schema, include_raw=include_raw, **kwargs
        )
        role = self.role
        record_fn = self.record_fn

        async def _run(messages: list[BaseMessage], config: Any = None) -> Any:
            t0 = time.monotonic()
            try:
                return await inner_runnable.ainvoke(messages, config=config)
            finally:
                t1 = time.monotonic()
                record_fn(role, t0, t1)

        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_run)


class TestParallelAuditorsHermetic:
    """Prove concurrent Security // Perf-Arch fan-out across single_batch and map_reduce."""

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

    @pytest.mark.asyncio
    async def test_single_batch_parallel_fanout_barrier_all_six_categories_and_nfr3(self) -> None:
        parsed_diff, review_context = _build_hermetic_bundle()
        exporter = _RecordingSpanExporter()
        barrier_entered: set[str] = set()
        both_concurrent = asyncio.Event()
        auditor_completed: set[str] = set()
        observed_roles: list[str] = []

        async def _handler(
            _model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            sys_text = str(messages[0].content)
            user_text = str(messages[1].content)

            if schema is LLMAuditorDraftOutput and "Security Auditor" in sys_text:
                observed_roles.append("security_auditor")
                barrier_entered.add("security_auditor")
                if len(barrier_entered) == 2:
                    both_concurrent.set()
                await asyncio.wait_for(both_concurrent.wait(), timeout=2.0)
                auditor_completed.add("security_auditor")
                return {
                    "parsed": LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/users/auth_repo.py",
                                start_line=3,
                                end_line=3,
                                severity="CRITICAL",
                                category="sql_injection",
                                title="SQL injection in find_account",
                                evidence=(
                                    'query = f"SELECT id FROM accounts '
                                    "WHERE username = '{username}'\""
                                ),
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=3,
                                    end_line=4,
                                    replacement_code=(
                                        "  return conn.execute("
                                        '"SELECT id FROM accounts WHERE username = ?", '
                                        "(username,)).fetchone()"
                                    ),
                                ),
                            )
                        ]
                    )
                }

            if schema is LLMAuditorDraftOutput:
                observed_roles.append("logic_performance_auditor")
                assert "Performance & Architecture Auditor" in sys_text
                assert "finalize_checkout" in user_text
                assert "ADR-002" in user_text
                assert "load_orders" in user_text
                barrier_entered.add("logic_performance_auditor")
                if len(barrier_entered) == 2:
                    both_concurrent.set()
                await asyncio.wait_for(both_concurrent.wait(), timeout=2.0)
                auditor_completed.add("logic_performance_auditor")
                return {
                    "parsed": LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/orders/loader.py",
                                start_line=4,
                                end_line=5,
                                severity="WARNING",
                                category="n_plus_one_query",
                                title="N+1 DB query in load_orders",
                                evidence=(
                                    "for order_id in order_ids executes conn.execute per iteration"
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/webhooks/notifier.py",
                                start_line=3,
                                end_line=3,
                                severity="CRITICAL",
                                category="blocking_io_in_async",
                                title="Blocking time.sleep inside async def",
                                evidence="send_webhook_alert calls blocking time.sleep(2.0)",
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=3,
                                    end_line=3,
                                    replacement_code="  await asyncio.sleep(2.0)",
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/ts/syncBatch.ts",
                                start_line=3,
                                end_line=5,
                                severity="WARNING",
                                category="blocking_io_in_async",
                                title="Sequential await inside TypeScript loop",
                                evidence="for (const id of ids) awaits fn(id) sequentially",
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=3,
                                    end_line=5,
                                    replacement_code=(
                                        "    const loaded = await Promise.all(\n"
                                        "        ids.map((id) => fn(id))\n"
                                        "    );\n"
                                        "    out.push(...loaded);"
                                    ),
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/analytics/dedupe.py",
                                start_line=3,
                                end_line=5,
                                severity="WARNING",
                                category="algorithmic_complexity",
                                title="O(N^2) list membership in collect_matches",
                                evidence="for item in items performs linear item in seen lookup",
                            ),
                            LLMFindingDraft(
                                file_path="src/web/ViewportPanel.tsx",
                                start_line=3,
                                end_line=5,
                                severity="WARNING",
                                category="resource_or_listener_leak",
                                title="Uncleaned resize listener in ViewportPanel",
                                evidence=(
                                    "useEffect adds window.addEventListener('resize', onResize) "
                                    "without cleanup"
                                ),
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=3,
                                    end_line=5,
                                    replacement_code=(
                                        "    useEffect(() => {\n"
                                        "      window.addEventListener('resize', onResize);\n"
                                        "      return () => "
                                        "window.removeEventListener('resize', onResize);\n"
                                        "    }, [onResize]);"
                                    ),
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/api/orders_router.py",
                                start_line=4,
                                end_line=4,
                                severity="WARNING",
                                category="architecture_violation",
                                title="Router executes SQL directly violating ADR-002",
                                evidence="get_order_endpoint runs db_session.execute directly",
                            ),
                            LLMFindingDraft(
                                file_path="src/billing/pricing.py",
                                start_line=1,
                                end_line=1,
                                severity="CRITICAL",
                                category="broken_caller_contract",
                                title="Signature change breaks finalize_checkout caller",
                                evidence=(
                                    "calculate_total removed tax_rate required by finalize_checkout"
                                ),
                            ),
                        ]
                    )
                }

            observed_roles.append("critic")
            assert auditor_completed == {"security_auditor", "logic_performance_auditor"}
            return {
                "parsed": LLMCriticDraftOutput(
                    verdicts=[
                        LLMCriticVerdictDraft(
                            candidate_id=f"b000-log-00{i}",
                            decision="APPROVE",
                            rationale="Confirmed grounded issue.",
                        )
                        for i in range(7)
                    ]
                    + [
                        LLMCriticVerdictDraft(
                            candidate_id="b000-sec-000",
                            decision="APPROVE",
                            rationale="Confirmed SQLi.",
                        )
                    ]
                )
            }

        settings = Settings(
            _env_file=None,
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-ca19-001",
            langfuse_secret_key=SecretStr("sk-lf-ca19-001"),
        )
        factory = LLMFactory(
            settings=settings,
            chat_model_builder=lambda _role, model_name: _FakeStructuredChatModel(
                model_name=model_name, handler=_handler
            ),
        )
        state = await run_pr_review_graph(
            parsed_diff=parsed_diff,
            llm_factory=factory,
            review_context=review_context,
            pr_id="Tieens-04/CodeSight-AI#19",
            head_sha="ca19head",
            trace_id="trace-ca19-single",
            span_exporter=exporter,
        )

        assert state.status == "completed"
        assert state.batching_mode == "single_batch"
        assert both_concurrent.is_set()
        assert len(state.llm_metrics) == 3
        assert set(observed_roles[:2]) == {"security_auditor", "logic_performance_auditor"}
        assert observed_roles[2] == "critic"
        _assert_matches_labels(state.merged_findings, _HERMETIC_LABELS)
        assert all(f.file_path not in _HERMETIC_SAFE_FILES for f in state.merged_findings)

        # Verify language-aware indentation & tree-sitter safety + Zero HTTP 422 partitioning
        for finding in state.merged_findings:
            if finding.suggestion is not None:
                assert _is_tree_sitter_valid(finding.file_path, finding)
                if finding.file_path == "src/webhooks/notifier.py":
                    assert finding.suggestion.replacement_code == "    await asyncio.sleep(2.0)"
                if finding.file_path == "src/ts/syncBatch.ts":
                    assert finding.suggestion.replacement_code.startswith("  const loaded")
                    assert "\n    ids.map" in finding.suggestion.replacement_code
                if finding.file_path == "src/web/ViewportPanel.tsx":
                    assert finding.suggestion.replacement_code.startswith("  useEffect")
                    assert "\n    window.addEventListener" in finding.suggestion.replacement_code

        pub_drafts = [
            ReviewCommentDraft(
                path=f.file_path,
                line=f.end_line,
                start_line=None if f.start_line == f.end_line else f.start_line,
                body=f.evidence,
                severity=f.severity,
            )
            for f in state.merged_findings
        ]
        prepared = partition_review_comments(parsed_diff, pub_drafts)
        assert len(prepared.inline_comments) == 8
        assert len(prepared.downgraded_comments) == 0

        # NFR-3: raw code/prompt must never leak into OTel spans
        for span in exporter.spans:
            serialized = json.dumps(dict(span.attributes or {}), default=str)
            assert "send_webhook_alert" not in serialized
            assert "calculate_total" not in serialized

    @pytest.mark.asyncio
    async def test_map_reduce_send_runs_auditors_concurrently_per_batch_and_cancels_on_error(
        self,
    ) -> None:
        entries = [
            PatchFileEntry(
                filename="src/mod_a.py",
                status="modified",
                patch="@@ -1,1 +1,3 @@\n async def alpha():\n+    time.sleep(1)\n+    return 1\n",
            ),
            PatchFileEntry(
                filename="src/mod_b.py",
                status="modified",
                patch="@@ -1,1 +1,3 @@\n def beta(uid):\n+    q = f'SELECT {uid}'\n+    return q\n",
            ),
        ]
        parsed = SmartDiffParser().parse_patch_entries(entries)
        graph_cfg = ReviewGraphConfig(
            batch_diff_token_limit=55,
            max_request_tokens=4000,
            output_reserve_tokens=256,
        )
        batches, _ = split_parsed_diff_into_batches(
            parsed, max_batch_tokens=graph_cfg.available_auditor_diff_tokens()
        )
        assert len(batches) == 2

        events_by_batch: dict[int, tuple[set[str], asyncio.Event]] = {
            0: (set(), asyncio.Event()),
            1: (set(), asyncio.Event()),
        }

        async def _map_reduce_invoker(
            role: str,
            model_name: str,
            _schema: type[BaseModel],
            messages: list[BaseMessage],
            _config: Any,
        ) -> StructuredInvocationResult[Any]:
            user_text = str(messages[1].content)
            b_idx = 0 if "src/mod_a.py" in user_text else 1
            parsed_out: BaseModel
            if role in {"security_auditor", "logic_performance_auditor"}:
                seen, evt = events_by_batch[b_idx]
                seen.add(role)
                if len(seen) == 2:
                    evt.set()
                await asyncio.wait_for(evt.wait(), timeout=2.0)
                if role == "logic_performance_auditor" and b_idx == 0:
                    parsed_out = LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/mod_a.py",
                                start_line=2,
                                end_line=2,
                                severity="CRITICAL",
                                category="blocking_io_in_async",
                                title="Blocking sleep",
                                evidence="time.sleep(1) in async def alpha",
                            )
                        ]
                    )
                elif role == "security_auditor" and b_idx == 1:
                    parsed_out = LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/mod_b.py",
                                start_line=2,
                                end_line=2,
                                severity="CRITICAL",
                                category="sql_injection",
                                title="SQLi",
                                evidence="q = f'SELECT {uid}'",
                            )
                        ]
                    )
                else:
                    parsed_out = LLMAuditorDraftOutput(findings=[])
            else:
                parsed_out = LLMCriticDraftOutput(verdicts=[])
            return StructuredInvocationResult(
                parsed=parsed_out,
                model_name=model_name,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                estimated_cost_usd=0.0001,
                latency_ms=5.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),
            structured_invoker=cast(Any, _map_reduce_invoker),
        )
        state = await run_pr_review_graph(
            parsed_diff=parsed,
            llm_factory=factory,
            graph_config=graph_cfg,
            pr_id="Tieens-04/CodeSight-AI#19-mr",
            head_sha="mrsha",
        )
        assert state.batching_mode == "map_reduce"
        assert state.status == "completed"
        assert events_by_batch[0][1].is_set()
        assert events_by_batch[1][1].is_set()
        assert len(state.merged_findings) == 2

        # Sibling cancellation check when one auditor raises inside process_diff_batch
        sibling_cancelled = asyncio.Event()

        async def _failing_handler(
            _model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            sys_text = str(messages[0].content)
            if schema is LLMAuditorDraftOutput and "Security Auditor" in sys_text:
                raise ValueError("Simulated permanent failure in security_auditor")
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
            return {"parsed": LLMAuditorDraftOutput(findings=[])}

        fail_factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),
            chat_model_builder=lambda _role, model_name: _FakeStructuredChatModel(
                model_name=model_name, handler=_failing_handler
            ),
        )
        with pytest.raises(ValueError, match="Simulated permanent failure"):
            await run_pr_review_graph(
                parsed_diff=parsed,
                llm_factory=fail_factory,
                graph_config=graph_cfg,
                pr_id="Tieens-04/CodeSight-AI#19-fail",
                head_sha="failsha",
            )
        assert sibling_cancelled.is_set()

    def test_assert_matches_labels_rejects_wrong_category_role_span_duplicate_and_safe_control(
        self,
    ) -> None:
        base_valid = [
            FindingCandidate(
                candidate_id="b000-log-000",
                role="logic_performance_auditor",
                batch_index=0,
                file_path="src/orders/loader.py",
                start_line=4,
                end_line=5,
                severity="WARNING",
                category="n_plus_one_query",
                title="N+1 DB query",
                evidence="conn.execute",
            ),
            FindingCandidate(
                candidate_id="b000-log-001",
                role="logic_performance_auditor",
                batch_index=0,
                file_path="src/webhooks/notifier.py",
                start_line=3,
                end_line=3,
                severity="CRITICAL",
                category="blocking_io_in_async",
                title="Blocking time.sleep",
                evidence="time.sleep(2.0)",
            ),
            FindingCandidate(
                candidate_id="b000-log-002",
                role="logic_performance_auditor",
                batch_index=0,
                file_path="src/billing/pricing.py",
                start_line=1,
                end_line=1,
                severity="CRITICAL",
                category="broken_caller_contract",
                title="Removed tax_rate breaks caller",
                evidence="calculate_total",
            ),
            FindingCandidate(
                candidate_id="b000-sec-000",
                role="security_auditor",
                batch_index=0,
                file_path="src/users/auth_repo.py",
                start_line=3,
                end_line=3,
                severity="CRITICAL",
                category="sql_injection",
                title="SQL injection",
                evidence="SELECT id FROM accounts",
            ),
        ]
        _assert_matches_labels(base_valid, _LIVE_AC_LABELS)

        wrong_category = list(base_valid)
        wrong_category[0] = base_valid[0].model_copy(
            update={"category": "style_nitpick", "title": "Rename rows variable"}
        )
        with pytest.raises(AssertionError, match="missed expected keywords"):
            _assert_matches_labels(wrong_category, _LIVE_AC_LABELS)

        wrong_role = list(base_valid)
        wrong_role[0] = base_valid[0].model_copy(update={"role": "security_auditor"})
        with pytest.raises(AssertionError):
            _assert_matches_labels(wrong_role, _LIVE_AC_LABELS)

        overwide = list(base_valid)
        overwide[0] = base_valid[0].model_copy(update={"start_line": 2, "end_line": 6})
        with pytest.raises(AssertionError, match="out-of-span"):
            _assert_matches_labels(overwide, _LIVE_AC_LABELS)

        duplicate = [
            *base_valid,
            base_valid[0].model_copy(update={"candidate_id": "b000-log-099"}),
        ]
        with pytest.raises(AssertionError, match="Expected exactly 1 match"):
            _assert_matches_labels(duplicate, _LIVE_AC_LABELS)

        safe_hit = [
            *base_valid,
            FindingCandidate(
                candidate_id="b000-log-100",
                role="logic_performance_auditor",
                batch_index=0,
                file_path="src/orders/safe_batch_loader.py",
                start_line=2,
                end_line=2,
                severity="WARNING",
                category="n_plus_one_query",
                title="False positive",
                evidence="conn.execute",
            ),
        ]
        with pytest.raises(AssertionError, match="Unexpected"):
            _assert_matches_labels(safe_hit, _LIVE_AC_LABELS)


def _has_live_gemini_credentials() -> bool:
    try:
        cfg = Settings()
    except Exception:
        return False
    return cfg.has_gemini_api_key


_LIVE_AC_LABELS: tuple[GroundTruthLabel, ...] = (
    GroundTruthLabel(
        name="live_n_plus_one",
        file_path="src/orders/loader.py",
        allowed_spans=frozenset({(4, 4), (5, 5), (4, 5), (3, 5), (3, 6), (4, 6)}),
        category_keywords=("n_plus_one", "n+1", "query", "loop", "db"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="live_blocking_io",
        file_path="src/webhooks/notifier.py",
        allowed_spans=frozenset({(3, 3), (2, 3)}),
        category_keywords=("blocking", "io", "sleep", "sync", "event"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="live_broken_caller",
        file_path="src/billing/pricing.py",
        allowed_spans=frozenset({(1, 1), (1, 2)}),
        category_keywords=("caller", "signature", "contract", "break", "param"),
        expected_role="logic_performance_auditor",
    ),
    GroundTruthLabel(
        name="live_sqli",
        file_path="src/users/auth_repo.py",
        allowed_spans=frozenset({(3, 3), (4, 4), (3, 4)}),
        category_keywords=("sql", "injection", "sqli"),
        expected_role="security_auditor",
    ),
)

_LIVE_SAFE_FILES: frozenset[str] = frozenset(
    {
        "src/orders/safe_batch_loader.py",
        "src/webhooks/safe_async.py",
    }
)


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_live_gemini_credentials(),
    reason="Requires GEMINI_API_KEY in env/.env for live Gemini CA-19 parallel auditors gate.",
)
@pytest.mark.asyncio
async def test_ca19_live_gemini_parallel_auditors_under_10s() -> None:
    """Live Gemini test verifying Security // Perf-Arch overlap, <= 10s, and AC-2 detection."""
    entries = [
        *_shared_ac2_entries(),
        PatchFileEntry(
            filename="src/webhooks/safe_async.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,4 @@",
                    " import asyncio",
                    " async def wait_backoff() -> None:",
                    "+    await asyncio.sleep(0.5)",
                    "+    return None",
                ]
            )
            + "\n",
        ),
    ]
    parsed_diff = SmartDiffParser().parse_patch_entries(entries)
    caller_block = _build_checkout_caller_block()
    review_context = PRReviewContext(
        file_contexts=(
            PRFileContext(
                file_path="src/billing/pricing.py",
                mode="ast",
                language="python",
                touched_chunks=(
                    CodeSymbolChunk(
                        file_path="src/billing/pricing.py",
                        language="python",
                        symbol_name="calculate_total",
                        qualified_name="calculate_total",
                        symbol_kind="function",
                        start_line=1,
                        end_line=2,
                        signature="def calculate_total(subtotal: float) -> float",
                    ),
                ),
            ),
        ),
        packed_bundle=PackedContextBundle(
            blocks=(caller_block,),
            rendered_context=caller_block.rendered_text,
            metrics=TokenBudgetMetrics(
                budget_tokens=3000,
                packed_aux_tokens=36,
                diff_tokens=estimate_tokens(parsed_diff.annotated_diff),
                skipped_noise_tokens=0,
                baseline_tokens=400,
                codesight_tokens=200,
                saved_tokens=200,
                tokens_saved_pct=50.0,
                aux_tokens_saved_pct=50.0,
                unique_baseline_files=6,
                packed_blocks_count=1,
                truncated_blocks_count=0,
                dropped_blocks_count=0,
            ),
        ),
        block_target_symbols={
            ("src/checkout/service.py", "finalize_checkout"): ("calculate_total",)
        },
    )

    cfg = Settings()
    timings: list[tuple[LLMRole, float, float]] = []

    def _timed_builder(role: LLMRole, _model_name: str) -> BaseChatModel:
        real_model = LLMFactory(settings=cfg).build_chat_model(role)
        return _TimedChatModelProxy(
            inner=real_model,
            role=role,
            record_fn=lambda r, s, e: timings.append((r, s, e)),
        )

    factory = LLMFactory(settings=cfg, chat_model_builder=_timed_builder)

    final_state = await run_pr_review_graph(
        parsed_diff=parsed_diff,
        llm_factory=factory,
        review_context=review_context,
        pr_id="Tieens-04/CodeSight-AI#live-ca19",
        head_sha="liveca19",
    )

    assert final_state.status == "completed"
    assert len(final_state.llm_metrics) == 3

    sec_span = next(t for t in timings if t[0] == "security_auditor")
    perf_span = next(t for t in timings if t[0] == "logic_performance_auditor")
    # Concurrent overlap & combined parallel wall-clock <= 10.0s
    assert max(sec_span[1], perf_span[1]) < min(sec_span[2], perf_span[2])
    parallel_wall_seconds = max(sec_span[2], perf_span[2]) - min(sec_span[1], perf_span[1])
    assert parallel_wall_seconds <= 10.0, (
        f"Parallel Security // Perf-Arch took {parallel_wall_seconds:.2f}s (> 10.0s)"
    )

    _assert_matches_labels(final_state.merged_findings, _LIVE_AC_LABELS)
    assert all(f.file_path not in _LIVE_SAFE_FILES for f in final_state.merged_findings)

    for finding in final_state.merged_findings:
        if finding.suggestion is not None and finding.file_path in _HERMETIC_FULL_SOURCES:
            assert _is_tree_sitter_valid(finding.file_path, finding)

    pub_drafts = [
        ReviewCommentDraft(
            path=f.file_path,
            line=f.end_line,
            start_line=None if f.start_line == f.end_line else f.start_line,
            body=f.evidence,
            severity=f.severity,
        )
        for f in final_state.merged_findings
    ]
    prepared = partition_review_comments(parsed_diff, pub_drafts)
    assert len(prepared.downgraded_comments) == 0
