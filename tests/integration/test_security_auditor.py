"""Integration tests for CA-18: Context Builder + Security Auditor graph and live Gemini gate."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import BaseModel, SecretStr
from tree_sitter import Parser

from codesight.agents.graph import (
    ReviewGraphConfig,
    run_pr_review_graph,
    split_parsed_diff_into_batches,
)
from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes.context_builder import PRReviewContext
from codesight.agents.schemas import (
    FindingCandidate,
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
)
from codesight.ast_engine._python_ast import PYTHON_LANGUAGE
from codesight.ast_engine._typescript_ast import TSX_LANGUAGE
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
    must_mask_literal: str | None = None


_SYNTHETIC_DEMO_TOKEN = "cs_live_demo_secret_token_948102"

_GROUND_TRUTH_LABELS: tuple[GroundTruthLabel, ...] = (
    GroundTruthLabel(
        name="python_sqli",
        file_path="src/users/repository.py",
        allowed_spans=frozenset({(6, 6), (7, 7), (6, 7)}),
        category_keywords=("sql", "injection", "sqli"),
    ),
    GroundTruthLabel(
        name="hardcoded_token",
        file_path="src/billing/client.py",
        allowed_spans=frozenset({(3, 3)}),
        category_keywords=("secret", "token", "credential", "key"),
        must_mask_literal=_SYNTHETIC_DEMO_TOKEN,
    ),
    GroundTruthLabel(
        name="tsx_xss",
        file_path="src/web/UserBioCard.tsx",
        allowed_spans=frozenset({(5, 5)}),
        category_keywords=("xss", "cross_site", "script", "html"),
    ),
    GroundTruthLabel(
        name="cross_file_caller_authz",
        file_path="src/orders/service.py",
        allowed_spans=frozenset({(2, 2), (3, 3), (2, 3), (2, 4), (3, 4)}),
        category_keywords=("auth", "idor", "access", "permission", "authorization"),
    ),
)

_SAFE_CONTROL_FILES: frozenset[str] = frozenset(
    {
        "src/users/safe_repository.py",
        "src/web/SafeBioCard.tsx",
    }
)

_FULL_SOURCES_BY_FILE: dict[str, str] = {
    "src/users/repository.py": (
        "import sqlite3\n\n"
        "def find_user_by_id(conn: sqlite3.Connection, user_id: str) -> object:\n"
        "    if not user_id:\n"
        "        return None\n"
        "    query = f\"SELECT id, email FROM users WHERE id = '{user_id}'\"\n"
        "    return conn.execute(query).fetchone()\n"
    ),
    "src/billing/client.py": (
        "import os\n"
        "def build_billing_headers() -> dict[str, str]:\n"
        f'    API_TOKEN = "{_SYNTHETIC_DEMO_TOKEN}"\n'
        '    return {"Authorization": f"Bearer {API_TOKEN}"}\n'
    ),
    "src/web/UserBioCard.tsx": (
        "import React from 'react';\n"
        "export function UserBioCard({ bioHtml }: { bioHtml: string }) {\n"
        "  return (\n"
        "    <section>\n"
        "      <div dangerouslySetInnerHTML={{ __html: bioHtml }} />\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    ),
    "src/orders/service.py": (
        "import sqlite3\n"
        "def delete_order(conn: sqlite3.Connection, order_id: str) -> None:\n"
        '    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))\n'
        "    conn.commit()\n"
    ),
}


def _build_owasp_fixture_bundle() -> tuple[ParsedPullRequestDiff, PRReviewContext]:
    """Construct realistic Python + TSX PR diff and RAM context without ground-truth hints."""
    entries = [
        PatchFileEntry(
            filename="src/users/repository.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,4 +1,7 @@",
                    " import sqlite3",
                    " ",
                    " def find_user_by_id(conn: sqlite3.Connection, user_id: str) -> object:",
                    "+    if not user_id:",
                    "+        return None",
                    "+    query = f\"SELECT id, email FROM users WHERE id = '{user_id}'\"",
                    "     return conn.execute(query).fetchone()",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/billing/client.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,4 @@",
                    " import os",
                    " def build_billing_headers() -> dict[str, str]:",
                    f'+    API_TOKEN = "{_SYNTHETIC_DEMO_TOKEN}"',
                    '     return {"Authorization": f"Bearer {API_TOKEN}"}',
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/web/UserBioCard.tsx",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,5 +1,7 @@",
                    " import React from 'react';",
                    " export function UserBioCard({ bioHtml }: { bioHtml: string }) {",
                    "   return (",
                    "     <section>",
                    "+      <div dangerouslySetInnerHTML={{ __html: bioHtml }} />",
                    "     </section>",
                    "   );",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/orders/service.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,2 +1,5 @@",
                    " import sqlite3",
                    "+def delete_order(conn: sqlite3.Connection, order_id: str) -> None:",
                    '+    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))',
                    "+    conn.commit()",
                    " ",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/users/safe_repository.py",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,4 @@",
                    " import sqlite3",
                    " def find_user_safe(conn: sqlite3.Connection, user_id: str) -> object:",
                    '+    return conn.execute("SELECT id, email FROM users WHERE id = ?", '
                    "(user_id,)).fetchone()",
                ]
            )
            + "\n",
        ),
        PatchFileEntry(
            filename="src/web/SafeBioCard.tsx",
            status="modified",
            patch="\n".join(
                [
                    "@@ -1,3 +1,4 @@",
                    " import React from 'react';",
                    " export function SafeBioCard({ bioText }: { bioText: string }) {",
                    "+  return <p>{bioText}</p>;",
                ]
            )
            + "\n",
        ),
    ]
    parsed_diff = SmartDiffParser().parse_patch_entries(entries)

    file_contexts = (
        PRFileContext(
            file_path="src/users/repository.py",
            mode="ast",
            language="python",
            touched_chunks=(
                CodeSymbolChunk(
                    file_path="src/users/repository.py",
                    language="python",
                    symbol_name="find_user_by_id",
                    qualified_name="find_user_by_id",
                    symbol_kind="function",
                    start_line=3,
                    end_line=7,
                    signature=(
                        "def find_user_by_id(conn: sqlite3.Connection, user_id: str) -> object"
                    ),
                    called_symbols=("execute", "fetchone"),
                ),
            ),
            complexity=ComplexityDiffSummary(
                deltas=(
                    ComplexityDelta(
                        symbol_name="find_user_by_id",
                        qualified_name="find_user_by_id",
                        change_kind="modified",
                        before_cc=1,
                        after_cc=2,
                        delta_cc=1,
                        after_metrics=FunctionComplexity(
                            symbol_name="find_user_by_id",
                            qualified_name="find_user_by_id",
                            symbol_kind="function",
                            start_line=3,
                            end_line=7,
                            line_count=5,
                            cyclomatic_complexity=2,
                            max_nesting_depth=1,
                            parameter_count=2,
                        ),
                    ),
                ),
                net_delta_cc=1,
                max_delta_cc=1,
                max_after_cc=2,
            ),
        ),
        PRFileContext(
            file_path="src/orders/service.py",
            mode="ast",
            language="python",
            touched_chunks=(
                CodeSymbolChunk(
                    file_path="src/orders/service.py",
                    language="python",
                    symbol_name="delete_order",
                    qualified_name="delete_order",
                    symbol_kind="function",
                    start_line=2,
                    end_line=4,
                    signature="def delete_order(conn: sqlite3.Connection, order_id: str) -> None",
                    called_symbols=("execute", "commit"),
                ),
            ),
        ),
    )

    caller_block = PackedContextBlock(
        tier="caller",
        file_path="src/api/orders_router.py",
        start_line=18,
        end_line=21,
        symbol_name="handle_delete_order",
        symbol_kind="function",
        signature="def handle_delete_order(request, db)",
        rendered_text=(
            "### [Direct Caller] src/api/orders_router.py (L18-L21)\n"
            "L18: def handle_delete_order(request, db):\n"
            "L19:     order_id = request.path_params['order_id']\n"
            "L20:     delete_order(db, order_id)\n"
            "L21:     return {'deleted': True}"
        ),
        token_count=35,
        truncated=False,
    )
    enclosing_orders = PackedContextBlock(
        tier="enclosing_symbol",
        file_path="src/orders/service.py",
        start_line=2,
        end_line=4,
        symbol_name="delete_order",
        symbol_kind="function",
        signature="def delete_order(conn, order_id: str)",
        rendered_text=(
            "### [Enclosing Symbol] src/orders/service.py (L2-L4)\n"
            "L2: def delete_order(conn: sqlite3.Connection, order_id: str) -> None:\n"
            'L3:     conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))\n'
            "L4:     conn.commit()"
        ),
        token_count=35,
        truncated=False,
    )
    adr_block = PackedContextBlock(
        tier="architecture_doc",
        file_path="docs/adr-authz.md",
        start_line=1,
        end_line=5,
        symbol_name="Authorization Policy",
        symbol_kind="section",
        signature="ADR-004 Tenant & Owner Authorization",
        rendered_text=(
            "### [Architecture Doc] docs/adr-authz.md (L1-L5)\n"
            "Every mutating endpoint and service handler MUST enforce actor_id ownership "
            "or admin role before mutating state."
        ),
        token_count=26,
        truncated=False,
    )
    metrics = TokenBudgetMetrics(
        budget_tokens=3500,
        packed_aux_tokens=106,
        diff_tokens=estimate_tokens(parsed_diff.annotated_diff),
        skipped_noise_tokens=0,
        baseline_tokens=600,
        codesight_tokens=300,
        saved_tokens=300,
        tokens_saved_pct=50.0,
        aux_tokens_saved_pct=50.0,
        unique_baseline_files=6,
        packed_blocks_count=3,
        truncated_blocks_count=0,
        dropped_blocks_count=0,
    )
    assembled = AssembledPRContext(
        retrieval=HybridRetrievalResult(
            enclosing=(),
            callers=(
                RetrievedContextCandidate(
                    tier="caller",
                    point_id="pt-orders-caller",
                    repo_full_name="org/repo",
                    file_path="src/api/orders_router.py",
                    blob_sha="b" * 40,
                    ref=None,
                    symbol_name="handle_delete_order",
                    symbol_kind="function",
                    signature="def handle_delete_order(request, db)",
                    start_line=18,
                    end_line=21,
                    relevance_score=6.0,
                    matched_symbols=("delete_order",),
                ),
            ),
            callees=(),
            architecture_docs=(),
            query_text="delete_order",
            doc_search_degraded=False,
            qdrant_ms=1.0,
        ),
        blob_report=BlobBatchFetchReport(
            slices=(),
            unique_blobs_requested=0,
            unique_blobs_fetched=0,
            skipped_blobs=0,
            full_file_tokens_by_path={},
            elapsed_ms=1.0,
        ),
        packed_bundle=PackedContextBundle(
            blocks=(enclosing_orders, caller_block, adr_block),
            rendered_context="",
            metrics=metrics,
        ),
        qdrant_ms=1.0,
        blob_fetch_ms=1.0,
        pack_ms=1.0,
        total_ms=3.0,
    )
    review_context = PRReviewContext(
        file_contexts=file_contexts,
        assembled_context=assembled,
        repo_config=RepoConfig(
            custom_rules=(
                "Require parameterized SQL, environment-injected secrets, "
                "and explicit actor ownership checks."
            )
        ),
    )
    return parsed_diff, review_context


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
        raise NotImplementedError

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
                "usage",
                {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260},
            ),
            response_metadata={"model_name": self.model_name},
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=ai_msg,
                    generation_info={
                        "parsed": payload["parsed"],
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


def _is_tree_sitter_valid(file_path: str, finding: FindingCandidate) -> bool:
    if finding.suggestion is None:
        return False
    source = _FULL_SOURCES_BY_FILE.get(file_path)
    if source is None:
        return True
    lang = TSX_LANGUAGE if file_path.endswith(".tsx") else PYTHON_LANGUAGE
    parser = Parser(lang)
    lines = source.splitlines(keepends=True)
    replacement = [ln + "\n" for ln in finding.suggestion.replacement_code.splitlines()]
    patched = "".join(
        lines[: finding.suggestion.start_line - 1]
        + replacement
        + lines[finding.suggestion.end_line :]
    )
    return not parser.parse(patched.encode("utf-8")).root_node.has_error


def _assert_matches_labels(
    findings: Sequence[FindingCandidate],
    labels: Sequence[GroundTruthLabel],
) -> None:
    sec_findings = [f for f in findings if f.role == "security_auditor"]
    matches_by_label: dict[str, list[FindingCandidate]] = {label.name: [] for label in labels}
    unexpected: list[tuple[str, str, int, int]] = []

    for finding in sec_findings:
        matched_label: GroundTruthLabel | None = None
        for label in labels:
            if (
                finding.file_path == label.file_path
                and (finding.start_line, finding.end_line) in label.allowed_spans
                and any(
                    kw in finding.category.lower() or kw in finding.title.lower()
                    for kw in label.category_keywords
                )
            ):
                matched_label = label
                break
        if matched_label is None:
            unexpected.append(
                (finding.category, finding.file_path, finding.start_line, finding.end_line)
            )
        else:
            matches_by_label[matched_label.name].append(finding)

    assert not unexpected, f"Unexpected or out-of-span security findings: {unexpected}"
    for label in labels:
        matches = matches_by_label[label.name]
        assert len(matches) == 1, (
            f"Expected exactly 1 match for {label.name} ({label.file_path}), got {len(matches)}"
        )
        if label.must_mask_literal is not None:
            match = matches[0]
            assert label.must_mask_literal not in match.title
            assert label.must_mask_literal not in match.evidence
            if match.suggestion is not None:
                assert label.must_mask_literal not in match.suggestion.replacement_code
                assert label.must_mask_literal not in (match.suggestion.explanation or "")


class TestSecurityAuditorIntegration:
    @pytest.mark.asyncio
    async def test_security_auditor_catches_all_labeled_owasp_and_cross_file_authz_single_batch(
        self,
    ) -> None:
        parsed_diff, review_context = _build_owasp_fixture_bundle()
        exporter = _RecordingSpanExporter()
        observed_calls: list[tuple[str, str]] = []

        async def _fake_model_handler(
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            sys_text = str(messages[0].content)
            user_text = str(messages[1].content)
            if schema is LLMAuditorDraftOutput and "Security Auditor" in sys_text:
                observed_calls.append(("security_auditor", user_text))
                # Ensure Context Builder delivered all 5 sections including external caller
                assert "## 1. Diff" in user_text
                assert "## 2. Enclosing Symbols" in user_text
                assert "## 3. AST Complexity Delta" in user_text
                assert "## 4. Callers, Callees & Architecture Docs" in user_text
                assert "src/api/orders_router.py" in user_text
                assert "## 5. Repository Custom Rules" in user_text

                return {
                    "parsed": LLMAuditorDraftOutput(
                        findings=[
                            LLMFindingDraft(
                                file_path="src/users/repository.py",
                                start_line=6,
                                end_line=6,
                                severity="CRITICAL",
                                category="sql_injection",
                                title="SQL injection in find_user_by_id",
                                evidence=(
                                    "L6 interpolates user_id directly into SQL string passed "
                                    "to conn.execute(query)."
                                ),
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=6,
                                    end_line=7,
                                    replacement_code=(
                                        'return conn.execute("SELECT id, email FROM users '
                                        'WHERE id = ?", (user_id,)).fetchone()'
                                    ),
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/billing/client.py",
                                start_line=3,
                                end_line=3,
                                severity="CRITICAL",
                                category="hardcoded_secret",
                                title=f"Hardcoded API_TOKEN {_SYNTHETIC_DEMO_TOKEN}",
                                evidence=f'L3 hardcodes API_TOKEN = "{_SYNTHETIC_DEMO_TOKEN}".',
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=3,
                                    end_line=3,
                                    replacement_code='API_TOKEN = os.environ["BILLING_API_TOKEN"]',
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/web/UserBioCard.tsx",
                                start_line=5,
                                end_line=5,
                                severity="CRITICAL",
                                category="xss",
                                title="DOM XSS via dangerouslySetInnerHTML",
                                evidence=(
                                    "L5 passes unescaped bioHtml into dangerouslySetInnerHTML."
                                ),
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=5,
                                    end_line=5,
                                    replacement_code="<div>{bioHtml}</div>",
                                ),
                            ),
                            LLMFindingDraft(
                                file_path="src/orders/service.py",
                                start_line=2,
                                end_line=3,
                                severity="CRITICAL",
                                category="broken_access_control",
                                title=(
                                    "Missing authorization / IDOR across caller "
                                    "handle_delete_order -> delete_order"
                                ),
                                evidence=(
                                    "Caller handle_delete_order (orders_router.py) passes "
                                    "request.path_params['order_id'] directly to delete_order "
                                    "(L2-L3) without verifying actor_id ownership."
                                ),
                                suggestion=LLMCodeSuggestionDraft(
                                    start_line=2,
                                    end_line=3,
                                    replacement_code=(
                                        "def delete_order("
                                        "conn: sqlite3.Connection, order_id: str, actor_id: str"
                                        ") -> None:\n"
                                        '    conn.execute("DELETE FROM orders WHERE id = ? '
                                        'AND owner_id = ?", (order_id, actor_id))'
                                    ),
                                ),
                            ),
                        ]
                    ),
                    "raw_content": _SYNTHETIC_DEMO_TOKEN,
                }
            if schema is LLMAuditorDraftOutput:
                observed_calls.append(("logic_performance_auditor", user_text))
                return {"parsed": LLMAuditorDraftOutput(findings=[])}

            observed_calls.append(("critic", user_text))
            return {
                "parsed": LLMCriticDraftOutput(
                    verdicts=[
                        LLMCriticVerdictDraft(
                            candidate_id=f"b000-sec-00{i}",
                            decision="APPROVE",
                            rationale="Confirmed grounded security vulnerability.",
                        )
                        for i in range(4)
                    ]
                )
            }

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            langfuse_tracing_enabled=True,
            langfuse_public_key="pk-lf-ca18-int-001",
            langfuse_secret_key=SecretStr("sk-lf-ca18-int-001"),
        )
        factory = LLMFactory(
            settings=settings,
            chat_model_builder=lambda role, model_name: _FakeStructuredChatModel(
                model_name=model_name,
                handler=_fake_model_handler,
            ),
        )

        final_state = await run_pr_review_graph(
            parsed_diff=parsed_diff,
            llm_factory=factory,
            review_context=review_context,
            pr_id="Tieens-04/CodeSight-AI#18",
            head_sha="ca18head001",
            trace_id="trace-ca18-single",
            span_exporter=exporter,
        )

        assert final_state.status == "completed"
        assert final_state.batching_mode == "single_batch"
        assert [c[0] for c in observed_calls].count("security_auditor") == 1
        assert [c[0] for c in observed_calls].count("logic_performance_auditor") == 1
        assert [c[0] for c in observed_calls].count("critic") == 1
        assert len(final_state.llm_metrics) == 3

        _assert_matches_labels(final_state.merged_findings, _GROUND_TRUTH_LABELS)

        # Zero false positives on safe controls
        assert all(f.file_path not in _SAFE_CONTROL_FILES for f in final_state.merged_findings)

        # Verify restored indentation + syntax safety + GitHub Zero-422 inline compatibility
        pub_drafts: list[ReviewCommentDraft] = []
        for finding in final_state.merged_findings:
            assert finding.suggestion is not None
            assert _is_tree_sitter_valid(finding.file_path, finding)
            if finding.file_path == "src/users/repository.py":
                assert finding.suggestion.replacement_code.startswith("    return conn.execute")
            if finding.file_path == "src/billing/client.py":
                assert (
                    finding.suggestion.replacement_code
                    == '    API_TOKEN = os.environ["BILLING_API_TOKEN"]'
                )
                assert "***REDACTED***" in finding.evidence
            if finding.file_path == "src/web/UserBioCard.tsx":
                assert finding.suggestion.replacement_code == "      <div>{bioHtml}</div>"
            pub_drafts.append(
                ReviewCommentDraft(
                    path=finding.file_path,
                    line=finding.end_line,
                    start_line=None
                    if finding.start_line == finding.end_line
                    else finding.start_line,
                    body=finding.evidence,
                    severity=finding.severity,
                )
            )

        prepared = partition_review_comments(parsed_diff, pub_drafts)
        assert len(prepared.inline_comments) == 4
        assert len(prepared.downgraded_comments) == 0

        # NFR-3: canary secret/code must never appear in exported Langfuse spans
        for span in exporter.spans:
            serialized = json.dumps(dict(span.attributes or {}), default=str)
            assert _SYNTHETIC_DEMO_TOKEN not in serialized
            assert "dangerouslySetInnerHTML" not in serialized

    @pytest.mark.asyncio
    async def test_security_auditor_map_reduce_split_hunk_identical_lines_and_malformed_drafts(
        self,
    ) -> None:
        # Two files share L1..L3, plus a deleted line and a split-hunk file.
        oversized_lines = (
            [" def delete_order(conn, order_id: str):"]
            + [f"+    audit_field_{i} = '{'z' * 50}'" for i in range(14)]
            + [
                "-    legacy_check = True",
                "+    conn.execute('DELETE FROM orders WHERE id = ?', (order_id,))",
            ]
        )
        entries = [
            PatchFileEntry(
                filename="src/alpha_auth.py",
                status="modified",
                patch="\n".join(
                    [
                        "@@ -1,3 +1,3 @@",
                        " def check_user(uid):",
                        "-    old_secret = 'legacy_drop'",
                        "+    return f'SELECT * FROM t WHERE id={uid}'",
                        "     pass",
                    ]
                )
                + "\n",
            ),
            PatchFileEntry(
                filename="src/beta_xss.tsx",
                status="modified",
                patch="\n".join(
                    [
                        "@@ -1,3 +1,3 @@",
                        " export function Card({ html }: { html: string }) {",
                        "-  const dead = 1;",
                        "+  return <div dangerouslySetInnerHTML={{ __html: html }} />;",
                        " }",
                    ]
                )
                + "\n",
            ),
            PatchFileEntry(
                filename="src/orders/service.py",
                status="modified",
                patch="\n".join([f"@@ -1,2 +1,{len(oversized_lines) - 1} @@", *oversized_lines])
                + "\n",
            ),
        ]
        parsed_diff = SmartDiffParser().parse_patch_entries(entries)
        graph_config = ReviewGraphConfig(
            batch_diff_token_limit=220,
            max_request_tokens=2_000,
            output_reserve_tokens=200,
        )
        expected_batches, unrev = split_parsed_diff_into_batches(
            parsed_diff,
            max_batch_tokens=graph_config.available_auditor_diff_tokens(),
        )
        assert unrev == ()
        assert len(expected_batches) >= 3

        review_context = PRReviewContext(
            file_contexts=(
                PRFileContext(
                    file_path="src/orders/service.py",
                    mode="ast",
                    language="python",
                    touched_chunks=(
                        CodeSymbolChunk(
                            file_path="src/orders/service.py",
                            language="python",
                            symbol_name="delete_order",
                            qualified_name="delete_order",
                            symbol_kind="function",
                            start_line=1,
                            end_line=len(oversized_lines),
                            signature="def delete_order(conn, order_id: str)",
                        ),
                    ),
                ),
            ),
            packed_bundle=PackedContextBundle(
                blocks=(
                    PackedContextBlock(
                        tier="caller",
                        file_path="src/api/orders_router.py",
                        start_line=10,
                        end_line=15,
                        symbol_name="route_delete",
                        symbol_kind="function",
                        signature="def route_delete(req)",
                        rendered_text="L12: delete_order(db, req.order_id)",
                        token_count=20,
                        truncated=False,
                    ),
                ),
                rendered_context="",
                metrics=TokenBudgetMetrics(
                    budget_tokens=3500,
                    packed_aux_tokens=20,
                    diff_tokens=0,
                    skipped_noise_tokens=0,
                    baseline_tokens=0,
                    codesight_tokens=20,
                    saved_tokens=0,
                    tokens_saved_pct=0.0,
                    aux_tokens_saved_pct=0.0,
                    unique_baseline_files=0,
                    packed_blocks_count=1,
                    truncated_blocks_count=0,
                    dropped_blocks_count=0,
                ),
            ),
            block_target_symbols={("src/api/orders_router.py", "route_delete"): ("delete_order",)},
        )

        caller_prompts_seen: list[bool] = []
        calls_per_role: dict[str, int] = {
            "security_auditor": 0,
            "logic_performance_auditor": 0,
            "critic": 0,
        }

        async def _map_reduce_handler(
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
        ) -> dict[str, Any]:
            sys_text = str(messages[0].content)
            user_text = str(messages[1].content)
            if schema is LLMAuditorDraftOutput and "Security Auditor" in sys_text:
                calls_per_role["security_auditor"] += 1
                if "src/orders/service.py" in user_text:
                    caller_prompts_seen.append("src/api/orders_router.py" in user_text)
                else:
                    assert "src/api/orders_router.py" not in user_text

                drafts: list[LLMFindingDraft] = [
                    # Line 0, out-of-hunk, and external caller drafts must all drop.
                    LLMFindingDraft(
                        file_path="src/alpha_auth.py",
                        start_line=0,
                        end_line=0,
                        severity="WARNING",
                        category="broken",
                        title="Invalid L0",
                        evidence="Bad L0",
                    ),
                    LLMFindingDraft(
                        file_path="src/api/orders_router.py",
                        start_line=12,
                        end_line=12,
                        severity="CRITICAL",
                        category="broken_access_control",
                        title="Out-of-diff caller file",
                        evidence="L12 in external caller",
                    ),
                ]
                if "src/alpha_auth.py" in user_text:
                    drafts.append(
                        LLMFindingDraft(
                            file_path="src/alpha_auth.py",
                            start_line=2,
                            end_line=2,
                            severity="CRITICAL",
                            category="sql_injection",
                            title="SQLi in alpha_auth",
                            evidence="L2 f-string SELECT",
                        )
                    )
                if "src/beta_xss.tsx" in user_text:
                    drafts.append(
                        LLMFindingDraft(
                            file_path="src/beta_xss.tsx",
                            start_line=2,
                            end_line=2,
                            severity="CRITICAL",
                            category="xss",
                            title="XSS in beta_xss",
                            evidence="L2 dangerouslySetInnerHTML",
                        )
                    )
                if "conn.execute('DELETE FROM orders" in user_text:
                    drafts.append(
                        LLMFindingDraft(
                            file_path="src/orders/service.py",
                            start_line=16,
                            end_line=16,
                            severity="CRITICAL",
                            category="broken_access_control",
                            title="Missing ownership check in split hunk tail",
                            evidence="L16 executes DELETE without owner check",
                        )
                    )
                    # Also inject a finding targeting L2 which sits in an earlier split batch!
                    drafts.append(
                        LLMFindingDraft(
                            file_path="src/orders/service.py",
                            start_line=2,
                            end_line=2,
                            severity="WARNING",
                            category="security",
                            title="Wrong split batch coordinate",
                            evidence="L2 belongs to earlier batch",
                        )
                    )
                return {"parsed": LLMAuditorDraftOutput(findings=drafts)}

            if schema is LLMAuditorDraftOutput:
                calls_per_role["logic_performance_auditor"] += 1
                return {"parsed": LLMAuditorDraftOutput(findings=[])}

            calls_per_role["critic"] += 1
            return {"parsed": LLMCriticDraftOutput(verdicts=[])}

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),  # type: ignore[call-arg]
            chat_model_builder=lambda role, model_name: _FakeStructuredChatModel(
                model_name=model_name,
                handler=_map_reduce_handler,
            ),
        )
        state = await run_pr_review_graph(
            parsed_diff=parsed_diff,
            llm_factory=factory,
            graph_config=graph_config,
            review_context=review_context,
            pr_id="org/repo#18-mr",
            head_sha="mrhead001",
            trace_id="trace-ca18-mr",
        )

        assert state.batching_mode == "map_reduce"
        assert state.status == "completed"
        assert len(state.batches) == len(expected_batches)
        assert calls_per_role["security_auditor"] == len(expected_batches)
        assert calls_per_role["logic_performance_auditor"] == len(expected_batches)
        assert calls_per_role["critic"] == sum(1 for br in state.batch_results if br.findings)
        assert any(caller_prompts_seen)
        assert sum(br.dropped_invalid_findings_count for br in state.batch_results) > 0

        # Every surviving finding uses exact RIGHT coordinates and posts inline (0 downgrades).
        assert {(f.file_path, f.start_line, f.end_line) for f in state.merged_findings} == {
            ("src/alpha_auth.py", 2, 2),
            ("src/beta_xss.tsx", 2, 2),
            ("src/orders/service.py", 16, 16),
        }
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
        assert len(prepared.inline_comments) == 3
        assert len(prepared.downgraded_comments) == 0

    def test_ground_truth_matcher_rejects_overwide_spans_and_extra_findings(self) -> None:
        base_valid = [
            FindingCandidate(
                candidate_id="b000-sec-000",
                role="security_auditor",
                batch_index=0,
                file_path="src/users/repository.py",
                start_line=6,
                end_line=6,
                severity="CRITICAL",
                category="sql_injection",
                title="SQL injection",
                evidence="query = f\"SELECT ... '{user_id}'\"",
            ),
            FindingCandidate(
                candidate_id="b000-sec-001",
                role="security_auditor",
                batch_index=0,
                file_path="src/billing/client.py",
                start_line=3,
                end_line=3,
                severity="CRITICAL",
                category="hardcoded_secret",
                title="Hardcoded API_TOKEN",
                evidence='API_TOKEN = "***REDACTED***"',
            ),
            FindingCandidate(
                candidate_id="b000-sec-002",
                role="security_auditor",
                batch_index=0,
                file_path="src/web/UserBioCard.tsx",
                start_line=5,
                end_line=5,
                severity="CRITICAL",
                category="xss",
                title="DOM XSS",
                evidence="dangerouslySetInnerHTML={{ __html: bioHtml }}",
            ),
            FindingCandidate(
                candidate_id="b000-sec-003",
                role="security_auditor",
                batch_index=0,
                file_path="src/orders/service.py",
                start_line=2,
                end_line=3,
                severity="CRITICAL",
                category="broken_access_control",
                title="Missing authorization",
                evidence="delete_order lacks ownership check",
            ),
        ]
        _assert_matches_labels(base_valid, _GROUND_TRUTH_LABELS)

        # Over-wide span on tsx_xss (L2-L5 instead of L5-L5) must fail
        overwide = list(base_valid)
        overwide[2] = base_valid[2].model_copy(update={"start_line": 2, "end_line": 5})
        with pytest.raises(AssertionError, match="out-of-span"):
            _assert_matches_labels(overwide, _GROUND_TRUTH_LABELS)

        # Extra finding inside a vulnerable file must fail
        extra_in_vuln = [
            *base_valid,
            FindingCandidate(
                candidate_id="b000-sec-004",
                role="security_auditor",
                batch_index=0,
                file_path="src/users/repository.py",
                start_line=4,
                end_line=4,
                severity="WARNING",
                category="sql_injection",
                title="Spurious finding on guard",
                evidence="if not user_id:",
            ),
        ]
        with pytest.raises(AssertionError, match="Unexpected"):
            _assert_matches_labels(extra_in_vuln, _GROUND_TRUTH_LABELS)

        # Duplicate finding on the same labeled vulnerability must fail
        duplicate_match = [
            *base_valid,
            base_valid[2].model_copy(update={"candidate_id": "b000-sec-005"}),
        ]
        with pytest.raises(AssertionError, match="Expected exactly 1 match"):
            _assert_matches_labels(duplicate_match, _GROUND_TRUTH_LABELS)

        # Extra finding on a safe control file must fail
        safe_control_hit = [
            *base_valid,
            FindingCandidate(
                candidate_id="b000-sec-006",
                role="security_auditor",
                batch_index=0,
                file_path="src/users/safe_repository.py",
                start_line=3,
                end_line=3,
                severity="WARNING",
                category="sql_injection",
                title="False positive on safe query",
                evidence="conn.execute",
            ),
        ]
        with pytest.raises(AssertionError, match="Unexpected"):
            _assert_matches_labels(safe_control_hit, _GROUND_TRUTH_LABELS)


def _has_live_gemini_credentials() -> bool:
    try:
        cfg = Settings()
    except Exception:
        return False
    return cfg.has_gemini_api_key


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_live_gemini_credentials(),
    reason="Requires GEMINI_API_KEY in env/.env for live Gemini CA-18 OWASP audit gate.",
)
@pytest.mark.asyncio
async def test_ca18_live_gemini_security_auditor_owasp_suite() -> None:
    """Live Gemini verification across 4 labeled Python/TSX vulnerabilities + 2 safe controls."""
    parsed_diff, review_context = _build_owasp_fixture_bundle()
    cfg = Settings()
    factory = LLMFactory(settings=cfg)

    final_state = await run_pr_review_graph(
        parsed_diff=parsed_diff,
        llm_factory=factory,
        review_context=review_context,
        pr_id="Tieens-04/CodeSight-AI#live-ca18",
        head_sha="liveca18",
    )

    assert final_state.status == "completed"
    assert len(final_state.llm_metrics) == 3
    _assert_matches_labels(final_state.merged_findings, _GROUND_TRUTH_LABELS)

    # Every security finding must remain valid for GitHub inline publication (0 out-of-hunk leaks)
    sec_findings = [f for f in final_state.merged_findings if f.role == "security_auditor"]
    for finding in sec_findings:
        if finding.suggestion is not None:
            assert _is_tree_sitter_valid(finding.file_path, finding)
    pub_drafts = [
        ReviewCommentDraft(
            path=f.file_path,
            line=f.end_line,
            start_line=None if f.start_line == f.end_line else f.start_line,
            body=f.evidence,
            severity=f.severity,
        )
        for f in sec_findings
    ]
    prepared = partition_review_comments(parsed_diff, pub_drafts)
    assert len(prepared.downgraded_comments) == 0

    # Report any false positives on safe control files explicitly if triggered
    safe_hits = [f for f in sec_findings if f.file_path in _SAFE_CONTROL_FILES]
    assert safe_hits == [], f"Unexpected false positives on safe controls: {len(safe_hits)}"
