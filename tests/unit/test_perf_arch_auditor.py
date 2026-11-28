"""Unit tests for CA-19: Perf & Arch Auditor, shared guardrails, 2/4-space indents, and config."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError
from tree_sitter import Parser

from codesight.agents.graph import ReviewGraphConfig, split_parsed_diff_into_batches
from codesight.agents.llm_factory import LLMFactory, StructuredInvocationResult
from codesight.agents.nodes.context_builder import PRReviewContext, build_batch_context
from codesight.agents.nodes.perf_arch_auditor import (
    build_perf_arch_auditor_messages,
    run_perf_arch_auditor,
    validate_perf_arch_findings,
)
from codesight.agents.nodes.security_auditor import validate_security_findings
from codesight.agents.prompts.perf_arch_prompt import (
    PERF_ARCH_AUDITOR_SYSTEM_PROMPT,
    PERF_ARCH_CATEGORIES,
    PERF_ARCH_PROMPT_VERSION,
    get_perf_arch_system_prompt,
)
from codesight.agents.schemas import (
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMFindingDraft,
)
from codesight.ast_engine._python_ast import PYTHON_LANGUAGE
from codesight.ast_engine._typescript_ast import TSX_LANGUAGE, TYPESCRIPT_LANGUAGE
from codesight.core.config import ALLOWED_GEMINI_CRITIC_MODELS, Settings
from codesight.core.observability import MODEL_PRICING_PER_1M_TOKENS, estimate_model_cost_usd
from codesight.diff.file_filter import estimate_tokens
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.token_packer import (
    PackedContextBlock,
    PackedContextBundle,
    TokenBudgetMetrics,
)


def _empty_metrics() -> TokenBudgetMetrics:
    return TokenBudgetMetrics(
        budget_tokens=2000,
        packed_aux_tokens=0,
        diff_tokens=0,
        skipped_noise_tokens=0,
        baseline_tokens=0,
        codesight_tokens=0,
        saved_tokens=0,
        tokens_saved_pct=0.0,
        aux_tokens_saved_pct=0.0,
        unique_baseline_files=0,
        packed_blocks_count=0,
        truncated_blocks_count=0,
        dropped_blocks_count=0,
    )


def _make_block(
    *,
    tier: str,
    file_path: str,
    start_line: int,
    end_line: int,
    symbol_name: str,
    rendered_text: str,
) -> PackedContextBlock:
    return PackedContextBlock(
        tier=tier,  # type: ignore[arg-type]
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        symbol_name=symbol_name,
        symbol_kind="function",
        signature=symbol_name,
        rendered_text=rendered_text,
        token_count=estimate_tokens(rendered_text),
        truncated=False,
    )


def _is_syntax_safe(
    full_source: str,
    start_line: int,
    end_line: int,
    replacement_code: str,
    *,
    lang: str = "py",
) -> bool:
    lang_obj = (
        PYTHON_LANGUAGE
        if lang == "py"
        else (TSX_LANGUAGE if lang == "tsx" else TYPESCRIPT_LANGUAGE)
    )
    parser = Parser(lang_obj)
    lines = full_source.splitlines(keepends=True)
    replacement = [f"{ln}\n" for ln in replacement_code.splitlines()]
    patched = "".join(lines[: start_line - 1] + replacement + lines[end_line:])
    tree = parser.parse(patched.encode("utf-8"))
    return not tree.root_node.has_error


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


class TestPerfArchPromptAndConfig:
    """Verify prompt budget, categories, Gemini model allowlist, and cost estimation."""

    def test_prompt_budget_and_categories(self) -> None:
        assert PERF_ARCH_PROMPT_VERSION == "ca19-v1"
        assert get_perf_arch_system_prompt() == PERF_ARCH_AUDITOR_SYSTEM_PROMPT
        assert estimate_tokens(PERF_ARCH_AUDITOR_SYSTEM_PROMPT) <= 220
        assert set(PERF_ARCH_CATEGORIES) == {
            "n_plus_one_query",
            "blocking_io_in_async",
            "algorithmic_complexity",
            "resource_or_listener_leak",
            "architecture_violation",
            "broken_caller_contract",
        }
        cfg = ReviewGraphConfig()
        assert cfg.available_auditor_user_tokens() > 1000

    def test_settings_allows_gemini_3_5_flash_lite_and_prices_it(self) -> None:
        default_cfg = Settings(_env_file=None)
        assert default_cfg.auditor_model == "gemini-2.5-flash"
        assert default_cfg.critic_model == "gemini-2.5-flash"

        lite_cfg = Settings(
            _env_file=None,
            auditor_model="gemini-3.5-flash-lite",
            critic_model="gemini-3.5-flash-lite",
        )
        assert lite_cfg.auditor_model == "gemini-3.5-flash-lite"
        assert lite_cfg.critic_model == "gemini-3.5-flash-lite"

        assert set(MODEL_PRICING_PER_1M_TOKENS) == set(ALLOWED_GEMINI_CRITIC_MODELS)
        assert "gemini-3.5-flash" not in ALLOWED_GEMINI_CRITIC_MODELS
        with pytest.raises(ValidationError):
            Settings(_env_file=None, critic_model="gemini-3.5-flash")
        with pytest.raises(ValidationError):
            Settings(_env_file=None, critic_model="gemini-invalid-99")

        cost = estimate_model_cost_usd(
            "gemini-3.5-flash-lite",
            input_tokens=1_000,
            output_tokens=200,
        )
        assert cost is not None
        assert cost["input"] == pytest.approx(0.0003)
        assert cost["output"] == pytest.approx(0.0005)
        assert cost["total"] == pytest.approx(0.0008)


class TestPerfArchValidationAndGrounding:
    """Verify 6-category grounding, ADR anti-hallucination, LEFT secret masking, and indents."""

    def test_all_six_categories_and_caller_left_signature_link_pass_grounding(self) -> None:
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/orders/loader.py",
                    patch=(
                        "@@ -1,2 +1,5 @@\n"
                        " def load_orders(conn, order_ids: list[str]):\n"
                        "+    results = []\n"
                        "+    for oid in order_ids:\n"
                        "+        results.append("
                        "conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/webhooks/client.py",
                    patch=(
                        "@@ -1,2 +1,3 @@\n"
                        " async def push_event(url: str):\n"
                        "+    return requests.post(url, timeout=5)\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/analytics/matcher.py",
                    patch=(
                        "@@ -1,2 +1,5 @@\n"
                        " def find_dupes(items: list[str], seen: list[str]):\n"
                        "+    out = []\n"
                        "+    for item in items:\n"
                        "+        if item in seen:\n"
                        "+            out.append(item)\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/web/Ticker.tsx",
                    patch=(
                        "@@ -1,2 +1,5 @@\n"
                        " export function Ticker() {\n"
                        "+  useEffect(() => {\n"
                        "+    window.addEventListener('resize', onResize);\n"
                        "+  }, []);\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/api/routes.py",
                    patch=(
                        "@@ -1,2 +1,3 @@\n"
                        " def get_profile(db_session, uid: str):\n"
                        "+    return db_session.execute('SELECT * FROM users')\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/billing/invoice.py",
                    patch=(
                        "@@ -1,2 +1,2 @@\n"
                        "-def build_invoice(user_id: str, include_tax: bool):\n"
                        "+def build_invoice(user_id: str):\n"
                        "     return user_id\n"
                    ),
                    status="modified",
                ),
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        blocks = (
            _make_block(
                tier="caller",
                file_path="src/checkout/handler.py",
                start_line=10,
                end_line=12,
                symbol_name="checkout_order",
                rendered_text="L11: return build_invoice(uid, include_tax=True)",
            ),
            _make_block(
                tier="architecture_doc",
                file_path="docs/adr-002.md",
                start_line=1,
                end_line=4,
                symbol_name="ADR-002",
                rendered_text="Routers in src/api must not invoke db_session directly.",
            ),
        )
        ctx = build_batch_context(
            batch=batch,
            max_user_tokens=4000,
            review_context=PRReviewContext(
                packed_bundle=PackedContextBundle(
                    blocks=blocks, rendered_context="", metrics=_empty_metrics()
                ),
                block_target_symbols={
                    ("src/checkout/handler.py", "checkout_order"): ("build_invoice",)
                },
            ),
        )

        drafts = [
            LLMFindingDraft(
                file_path="src/orders/loader.py",
                start_line=3,
                end_line=4,
                severity="WARNING",
                category="n_plus_one_query",
                title="N+1 query in loop",
                evidence="for oid in order_ids executes conn.execute per item",
            ),
            LLMFindingDraft(
                file_path="src/webhooks/client.py",
                start_line=2,
                end_line=2,
                severity="CRITICAL",
                category="blocking_io_in_async",
                title="Blocking requests.post inside async def",
                evidence="push_event uses blocking requests.post inside async def",
            ),
            LLMFindingDraft(
                file_path="src/analytics/matcher.py",
                start_line=3,
                end_line=4,
                severity="WARNING",
                category="algorithmic_complexity",
                title="O(N^2) membership check",
                evidence="for item in items checks item in seen list",
            ),
            LLMFindingDraft(
                file_path="src/web/Ticker.tsx",
                start_line=2,
                end_line=4,
                severity="WARNING",
                category="resource_or_listener_leak",
                title="Missing resize listener cleanup",
                evidence="useEffect registers window.addEventListener without cleanup",
            ),
            LLMFindingDraft(
                file_path="src/api/routes.py",
                start_line=2,
                end_line=2,
                severity="WARNING",
                category="architecture_violation",
                title="Router violates ADR-002",
                evidence="db_session.execute called directly in router violating ADR-002",
            ),
            LLMFindingDraft(
                file_path="src/billing/invoice.py",
                start_line=1,
                end_line=1,
                severity="CRITICAL",
                category="broken_caller_contract",
                title="Removed include_tax breaks checkout_order",
                evidence="build_invoice removed include_tax still passed by checkout_order",
            ),
            # Ungrounded ADR-only prose hallucination without anchored diff code MUST drop!
            LLMFindingDraft(
                file_path="src/api/routes.py",
                start_line=2,
                end_line=2,
                severity="WARNING",
                category="architecture_violation",
                title="Hallucinated ADR violation",
                evidence="Violates ADR-002 layering policy only without naming symbols",
            ),
        ]

        valid, dropped, ungrounded = validate_perf_arch_findings(
            drafts,
            batch=batch,
            batch_context=ctx,
        )
        assert dropped == 1
        assert ungrounded == 1
        assert len(valid) == 6
        assert [f.candidate_id for f in valid] == [f"b000-log-00{i}" for i in range(6)]
        assert all(f.role == "logic_performance_auditor" for f in valid)

    def test_left_deleted_secret_literal_masked_in_both_auditors(self) -> None:
        deleted_secret = "deleted_legacy_token_abc12345"
        patch_text = "\n".join(
            [
                "@@ -1,3 +1,3 @@",
                " def connect():",
                f'-    LEGACY_TOKEN = "{deleted_secret}"',
                "+    LEGACY_TOKEN = os.environ['LEGACY_TOKEN']",
                "     return LEGACY_TOKEN",
            ]
        )
        parsed = SmartDiffParser().parse_patch_entries(
            [PatchFileEntry(filename="src/conn.py", patch=patch_text, status="modified")]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        draft = LLMFindingDraft(
            file_path="src/conn.py",
            start_line=2,
            end_line=2,
            severity="INFO",
            category="broken_caller_contract",
            title=f"Replaced {deleted_secret}",
            evidence=f"LEGACY_TOKEN replaced {deleted_secret}",
            suggestion=LLMCodeSuggestionDraft(
                start_line=2,
                end_line=2,
                replacement_code=(
                    f'    LEGACY_TOKEN = os.environ.get("LEGACY_TOKEN", "{deleted_secret}")'
                ),
                explanation=f"Do not fall back to {deleted_secret}",
            ),
        )

        perf_valid, perf_dropped, _ = validate_perf_arch_findings([draft], batch=batch)
        sec_valid, sec_dropped, _ = validate_security_findings([draft], batch=batch)
        for res, dropped_cnt in ((perf_valid, perf_dropped), (sec_valid, sec_dropped)):
            assert dropped_cnt == 0
            assert len(res) == 1
            assert deleted_secret not in res[0].title
            assert deleted_secret not in res[0].evidence
            assert res[0].suggestion is not None
            assert deleted_secret not in res[0].suggestion.replacement_code
            assert deleted_secret not in (res[0].suggestion.explanation or "")

    def test_python_4space_and_ts_tsx_2_or_4space_indentation_scaling(self) -> None:
        # 1. Python 2-space nested suggestion scaled to 4-space + blank start_line anchored
        py_full = (
            "def fetch_all(conn, ids: list[str]):\n"
            "\n"
            "    for item_id in ids:\n"
            "        conn.execute(item_id)\n"
        )
        py_patch = "\n".join(
            [
                "@@ -1,1 +1,4 @@",
                " def fetch_all(conn, ids: list[str]):",
                "+",
                "+    for item_id in ids:",
                "+        conn.execute(item_id)",
            ]
        )
        # 2. TypeScript 2-space file receiving 4-space LLM suggestion -> scaled to 2-space
        ts2_full = (
            "async function loadAll(ids: string[]) {\n"
            "  const items = [];\n"
            "  for (const id of ids) {\n"
            "    items.push(await fetchItem(id));\n"
            "  }\n"
            "  return items;\n"
            "}\n"
        )
        ts2_patch = "\n".join(
            [
                "@@ -1,3 +1,7 @@",
                " async function loadAll(ids: string[]) {",
                "   const items = [];",
                "+  for (const id of ids) {",
                "+    items.push(await fetchItem(id));",
                "+  }",
                "   return items;",
                " }",
            ]
        )
        # 3. TSX 4-space file preserving 4-space nested indent
        tsx4_full = (
            "export function Watch({ onTick }: { onTick: () => void }) {\n"
            "    useEffect(() => {\n"
            "        const timer = setInterval(onTick, 1000);\n"
            "    }, [onTick]);\n"
            "    return <div />;\n"
            "}\n"
        )
        tsx4_patch = "\n".join(
            [
                "@@ -1,2 +1,6 @@",
                " export function Watch({ onTick }: { onTick: () => void }) {",
                "+    useEffect(() => {",
                "+        const timer = setInterval(onTick, 1000);",
                "+    }, [onTick]);",
                "     return <div />;",
                " }",
            ]
        )
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(filename="src/loader.py", patch=py_patch, status="modified"),
                PatchFileEntry(filename="src/loadAll.ts", patch=ts2_patch, status="modified"),
                PatchFileEntry(filename="src/Watch.tsx", patch=tsx4_patch, status="modified"),
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]

        drafts = [
            LLMFindingDraft(
                file_path="src/loader.py",
                start_line=2,
                end_line=4,
                severity="WARNING",
                category="n_plus_one_query",
                title="Batch query",
                evidence="for item_id in ids executes conn.execute",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2,
                    end_line=4,
                    replacement_code="  if ids:\n    conn.executemany(ids)",
                ),
            ),
            LLMFindingDraft(
                file_path="src/loadAll.ts",
                start_line=3,
                end_line=5,
                severity="WARNING",
                category="blocking_io_in_async",
                title="Sequential await loop",
                evidence="for (const id of ids) awaits fetchItem",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=3,
                    end_line=5,
                    replacement_code=(
                        "    const loaded = await Promise.all(\n"
                        "        ids.map((id) => fetchItem(id))\n"
                        "    );\n"
                        "    items.push(...loaded);"
                    ),
                ),
            ),
            LLMFindingDraft(
                file_path="src/Watch.tsx",
                start_line=2,
                end_line=4,
                severity="WARNING",
                category="resource_or_listener_leak",
                title="Timer leak",
                evidence="setInterval inside useEffect lacks clearInterval",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2,
                    end_line=4,
                    replacement_code=(
                        "  useEffect(() => {\n"
                        "      const timer = setInterval(onTick, 1000);\n"
                        "      return () => clearInterval(timer);\n"
                        "  }, [onTick]);"
                    ),
                ),
            ),
            # Mixed tabs/spaces drops suggestion only, keeping finding
            LLMFindingDraft(
                file_path="src/loader.py",
                start_line=3,
                end_line=4,
                severity="INFO",
                category="n_plus_one_query",
                title="Mixed tab suggestion",
                evidence="conn.execute(item_id)",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=3,
                    end_line=4,
                    replacement_code="if ids:\n\tconn.executemany(ids)",
                ),
            ),
        ]

        valid, dropped, _ = validate_perf_arch_findings(drafts, batch=batch)
        assert dropped == 0
        assert len(valid) == 4

        py_sugg = valid[0].suggestion
        assert py_sugg is not None
        assert py_sugg.replacement_code == "    if ids:\n        conn.executemany(ids)"
        assert _is_syntax_safe(py_full, 2, 4, py_sugg.replacement_code, lang="py") is True

        ts_sugg = valid[1].suggestion
        assert ts_sugg is not None
        assert ts_sugg.replacement_code == (
            "  const loaded = await Promise.all(\n"
            "    ids.map((id) => fetchItem(id))\n"
            "  );\n"
            "  items.push(...loaded);"
        )
        assert _is_syntax_safe(ts2_full, 3, 5, ts_sugg.replacement_code, lang="ts") is True

        tsx_sugg = valid[2].suggestion
        assert tsx_sugg is not None
        assert tsx_sugg.replacement_code == (
            "    useEffect(() => {\n"
            "        const timer = setInterval(onTick, 1000);\n"
            "        return () => clearInterval(timer);\n"
            "    }, [onTick]);"
        )
        assert _is_syntax_safe(tsx4_full, 2, 4, tsx_sugg.replacement_code, lang="tsx") is True

        assert valid[3].suggestion is None

    @pytest.mark.asyncio
    async def test_run_perf_arch_auditor_invokes_factory_and_filters_cross_batch(self) -> None:
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/a.py",
                    patch="@@ -1,1 +1,2 @@\n def a():\n+    return 1\n",
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/b.py",
                    patch="@@ -1,1 +1,2 @@\n def b():\n+    return 2\n",
                    status="modified",
                ),
            ]
        )
        batches, _ = split_parsed_diff_into_batches(parsed, max_batch_tokens=25)
        assert len(batches) == 2
        b0 = batches[0]
        ctx0 = build_batch_context(batch=b0, max_user_tokens=400)
        msgs = build_perf_arch_auditor_messages(ctx0)
        assert isinstance(msgs[0], SystemMessage)
        assert isinstance(msgs[1], HumanMessage)

        async def _invoker(
            role: str,
            model_name: str,
            schema: type[BaseModel],
            messages: list[BaseMessage],
            config: Any,
        ) -> StructuredInvocationResult[Any]:
            assert role == "logic_performance_auditor"
            assert schema is LLMAuditorDraftOutput
            return StructuredInvocationResult(
                parsed=LLMAuditorDraftOutput(
                    findings=[
                        LLMFindingDraft(
                            file_path="src/b.py",
                            start_line=2,
                            end_line=2,
                            severity="WARNING",
                            category="n_plus_one_query",
                            title="Cross-batch leak",
                            evidence="return 2",
                        )
                    ]
                ),
                model_name=model_name,
                input_tokens=50,
                output_tokens=15,
                total_tokens=65,
                estimated_cost_usd=0.0001,
                latency_ms=4.0,
                attempts=1,
            )

        factory = LLMFactory(
            settings=Settings(_env_file=None, langfuse_tracing_enabled=False),
            structured_invoker=_invoker,
        )
        findings, metrics, dropped = await run_perf_arch_auditor(
            batch=b0,
            batch_context=ctx0,
            llm_factory=factory,
        )
        assert findings == []
        assert dropped == 1
        assert metrics.role == "logic_performance_auditor"
        assert metrics.batch_index == b0.batch_index

    def test_left_deleted_symbol_caller_link_only_in_perf_arch(self) -> None:
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/billing/invoice.py",
                    patch=(
                        "@@ -1,2 +1,2 @@\n"
                        "-def legacy_build_invoice(user_id: str, include_tax: bool):\n"
                        "+def build_invoice(user_id: str):\n"
                        "     return user_id\n"
                    ),
                    status="modified",
                )
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        caller = _make_block(
            tier="caller",
            file_path="src/checkout/handler.py",
            start_line=10,
            end_line=12,
            symbol_name="checkout_order",
            rendered_text="L11: return legacy_build_invoice(uid, include_tax=True)",
        )
        ctx = build_batch_context(
            batch=batch,
            max_user_tokens=2000,
            review_context=PRReviewContext(
                packed_bundle=PackedContextBundle(
                    blocks=(caller,), rendered_context="", metrics=_empty_metrics()
                ),
                block_target_symbols={
                    ("src/checkout/handler.py", "checkout_order"): ("legacy_build_invoice",)
                },
            ),
        )
        draft = LLMFindingDraft(
            file_path="src/billing/invoice.py",
            start_line=1,
            end_line=1,
            severity="CRITICAL",
            category="broken_caller_contract",
            title="Renamed symbol breaks checkout_order",
            evidence="checkout_order still invokes legacy_build_invoice",
        )
        perf_valid, perf_dropped, perf_ungrounded = validate_perf_arch_findings(
            [draft], batch=batch, batch_context=ctx
        )
        sec_valid, sec_dropped, sec_ungrounded = validate_security_findings(
            [draft], batch=batch, batch_context=ctx
        )
        assert len(perf_valid) == 1
        assert perf_dropped == 0
        assert perf_ungrounded == 0
        assert sec_valid == []
        assert sec_dropped == 1
        assert sec_ungrounded == 1

    def test_long_function_body_links_caller_via_enclosing_symbol(self) -> None:
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/billing/pricing.py",
                    patch=(
                        "@@ -24,2 +24,3 @@\n"
                        "     if subtotal <= 0:\n"
                        "+        return None\n"
                        "     return subtotal\n"
                    ),
                    status="modified",
                )
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        enclosing = _make_block(
            tier="enclosing_symbol",
            file_path="src/billing/pricing.py",
            start_line=1,
            end_line=30,
            symbol_name="calculate_total",
            rendered_text="L1: def calculate_total(subtotal: float) -> float:",
        )
        caller = _make_block(
            tier="caller",
            file_path="src/checkout/service.py",
            start_line=14,
            end_line=18,
            symbol_name="finalize_checkout",
            rendered_text="L15: return calculate_total(subtotal) + 1.0",
        )
        ctx = build_batch_context(
            batch=batch,
            max_user_tokens=2000,
            review_context=PRReviewContext(
                packed_bundle=PackedContextBundle(
                    blocks=(enclosing, caller), rendered_context="", metrics=_empty_metrics()
                ),
                block_target_symbols={
                    ("src/checkout/service.py", "finalize_checkout"): ("calculate_total",)
                },
            ),
        )
        draft = LLMFindingDraft(
            file_path="src/billing/pricing.py",
            start_line=25,
            end_line=25,
            severity="CRITICAL",
            category="broken_caller_contract",
            title="Returning None breaks finalize_checkout",
            evidence="Returning None breaks finalize_checkout",
        )
        valid, dropped, ungrounded = validate_perf_arch_findings(
            [draft], batch=batch, batch_context=ctx
        )
        assert len(valid) == 1
        assert dropped == 0
        assert ungrounded == 0

    def test_already_aligned_multiline_dedent_preserved_for_tsx_and_python(self) -> None:
        tsx_full = (
            "import { useEffect } from 'react';\n"
            "export function ViewportPanel({ onResize }: { onResize: () => void }) {\n"
            "  useEffect(() => {\n"
            "    window.addEventListener('resize', onResize);\n"
            "  }, [onResize]);\n"
            "  return <div />;\n"
            "}\n"
        )
        tsx_patch = "\n".join(
            [
                "@@ -2,4 +2,5 @@",
                " export function ViewportPanel({ onResize }: { onResize: () => void }) {",
                "   useEffect(() => {",
                "+    window.addEventListener('resize', onResize);",
                "   }, [onResize]);",
                "   return <div />;",
            ]
        )
        py_full = (
            "def load_items(items: list[str]) -> list[str]:\n"
            "    out: list[str] = []\n"
            "    for item in items:\n"
            "        out.append(item)\n"
            "    return out\n"
        )
        py_patch = "\n".join(
            [
                "@@ -1,4 +1,5 @@",
                " def load_items(items: list[str]) -> list[str]:",
                "     out: list[str] = []",
                "+    for item in items:",
                "+        out.append(item)",
                "     return out",
            ]
        )
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/ViewportPanel.tsx", patch=tsx_patch, status="modified"
                ),
                PatchFileEntry(filename="src/items.py", patch=py_patch, status="modified"),
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        tsx_code = (
            "    window.addEventListener('resize', onResize);\n"
            "    return () => window.removeEventListener('resize', onResize);\n"
            "  }, [onResize]);"
        )
        py_code = "        out.extend(items)\n    return out"
        drafts = [
            LLMFindingDraft(
                file_path="src/ViewportPanel.tsx",
                start_line=4,
                end_line=5,
                severity="WARNING",
                category="resource_or_listener_leak",
                title="Add listener cleanup",
                evidence="window.addEventListener('resize', onResize)",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=4, end_line=5, replacement_code=tsx_code
                ),
            ),
            LLMFindingDraft(
                file_path="src/items.py",
                start_line=4,
                end_line=5,
                severity="INFO",
                category="algorithmic_complexity",
                title="Batch append",
                evidence="out.append(item)",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=4, end_line=5, replacement_code=py_code
                ),
            ),
        ]
        valid, dropped, _ = validate_perf_arch_findings(drafts, batch=batch)
        assert dropped == 0
        assert len(valid) == 2
        assert valid[0].suggestion is not None
        assert valid[0].suggestion.replacement_code == tsx_code
        assert _is_syntax_safe(tsx_full, 4, 5, tsx_code, lang="tsx") is True
        assert valid[1].suggestion is not None
        assert valid[1].suggestion.replacement_code == py_code
        assert _is_syntax_safe(py_full, 4, 5, py_code, lang="py") is True

    def test_indentation_scaling_edge_cases_and_fallbacks(self) -> None:
        tsx4_full = (
            "/**\n"
            " * Watch resize events.\n"
            " */\n"
            "export function Watch({ onTick }: { onTick: () => void }) {\n"
            "    useEffect(() => {\n"
            "        const timer = setInterval(onTick, 1000);\n"
            "    }, [onTick]);\n"
            "    return <div />;\n"
            "}\n"
        )
        tsx4_patch = "\n".join(
            [
                "@@ -1,4 +1,9 @@",
                " /**",
                "  * Watch resize events.",
                "  */",
                " export function Watch({ onTick }: { onTick: () => void }) {",
                "+    useEffect(() => {",
                "+        const timer = setInterval(onTick, 1000);",
                "+    }, [onTick]);",
                "     return <div />;",
                " }",
            ]
        )
        ts2_even_patch = "\n".join(
            [
                "@@ -1,3 +1,3 @@",
                "     if (ready) {",
                "+    runTask(a);",
                "     }",
            ]
        )
        py_cont_full = (
            "def check(a: bool, b: bool) -> int:\n"
            "    if (\n"
            "      a\n"
            "    ):\n"
            "        x = 1\n"
            "        return x\n"
            "    return 0\n"
        )
        py_cont_patch = "\n".join(
            [
                "@@ -1,5 +1,7 @@",
                " def check(a: bool, b: bool) -> int:",
                "+    if (",
                "+      a",
                "+    ):",
                "+        x = 1",
                "         return x",
                "     return 0",
            ]
        )
        py_top_patch = "@@ -1,2 +1,3 @@\n+def top(x: int):\n+    return x\n"
        ts_tab_patch = "@@ -1,2 +1,3 @@\n function f() {\n+\treturn 1;\n }\n"
        go_patch = "@@ -1,2 +1,3 @@\n func f() {\n+\treturn\n }\n"
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(filename="src/Watch.tsx", patch=tsx4_patch, status="modified"),
                PatchFileEntry(filename="src/even.ts", patch=ts2_even_patch, status="modified"),
                PatchFileEntry(filename="src/cont.py", patch=py_cont_patch, status="modified"),
                PatchFileEntry(filename="src/top.py", patch=py_top_patch, status="modified"),
                PatchFileEntry(filename="src/tabbed.ts", patch=ts_tab_patch, status="modified"),
                PatchFileEntry(filename="src/app.go", patch=go_patch, status="modified"),
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]

        cont_code = "    if (\n      a and b\n    ):\n        x = 2"
        drafts = [
            # 1. 4-space TSX with JSDoc + 2-space LLM suggestion -> scaled 2->4
            LLMFindingDraft(
                file_path="src/Watch.tsx",
                start_line=5,
                end_line=7,
                severity="WARNING",
                category="resource_or_listener_leak",
                title="Timer leak",
                evidence="setInterval(onTick, 1000)",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=5,
                    end_line=7,
                    replacement_code=(
                        "  useEffect(() => {\n"
                        "    const timer = setInterval(onTick, 1000);\n\n"
                        "    return () => clearInterval(timer);\n"
                        "  }, [onTick]);"
                    ),
                ),
            ),
            # 2. Uniform depth-2 [4, 4, 4] TS hunk -> does NOT double 2-space suggestion to 4
            LLMFindingDraft(
                file_path="src/even.ts",
                start_line=2,
                end_line=2,
                severity="INFO",
                category="blocking_io_in_async",
                title="Even depth TS",
                evidence="runTask(a);",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2,
                    end_line=2,
                    replacement_code="  if (ready) {\n    runTask(a);\n  }",
                ),
            ),
            # 3. 4-space Python already-aligned parenthesized continuation -> NOT mis-scaled
            LLMFindingDraft(
                file_path="src/cont.py",
                start_line=2,
                end_line=5,
                severity="INFO",
                category="algorithmic_complexity",
                title="Continuation check",
                evidence="x = 1",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2, end_line=5, replacement_code=cont_code
                ),
            ),
            # 4. Top-level def with orig_indent == "" in space-indented .py -> tab body dropped
            LLMFindingDraft(
                file_path="src/top.py",
                start_line=1,
                end_line=2,
                severity="INFO",
                category="n_plus_one_query",
                title="Tab body on top-level def",
                evidence="def top(x: int):",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=1, end_line=2, replacement_code="def top(x: int):\n\treturn x"
                ),
            ),
            # 5. Tab-indented .ts file + blank suggestion dropped
            LLMFindingDraft(
                file_path="src/tabbed.ts",
                start_line=2,
                end_line=2,
                severity="INFO",
                category="blocking_io_in_async",
                title="Tabbed TS",
                evidence="return 1;",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2, end_line=2, replacement_code="   \n   "
                ),
            ),
            # 6. Non-Py/TS (.go) skips scaling cleanly
            LLMFindingDraft(
                file_path="src/app.go",
                start_line=2,
                end_line=2,
                severity="INFO",
                category="blocking_io_in_async",
                title="Go file",
                evidence="return",
                suggestion=LLMCodeSuggestionDraft(
                    start_line=2, end_line=2, replacement_code="\treturn"
                ),
            ),
        ]
        valid, dropped, _ = validate_perf_arch_findings(drafts, batch=batch)
        assert dropped == 0
        assert len(valid) == 6

        tsx_sugg = valid[0].suggestion
        assert tsx_sugg is not None
        assert tsx_sugg.replacement_code == (
            "    useEffect(() => {\n"
            "        const timer = setInterval(onTick, 1000);\n\n"
            "        return () => clearInterval(timer);\n"
            "    }, [onTick]);"
        )
        assert _is_syntax_safe(tsx4_full, 5, 7, tsx_sugg.replacement_code, lang="tsx") is True

        even_sugg = valid[1].suggestion
        assert even_sugg is not None
        assert even_sugg.replacement_code == "    if (ready) {\n      runTask(a);\n    }"

        cont_sugg = valid[2].suggestion
        assert cont_sugg is not None
        assert cont_sugg.replacement_code == cont_code
        assert _is_syntax_safe(py_cont_full, 2, 5, cont_sugg.replacement_code, lang="py") is True

        assert valid[3].suggestion is None
        assert valid[4].suggestion is None
        assert valid[5].suggestion is not None
        assert valid[5].suggestion.replacement_code == "\treturn"

    def test_cross_hunk_suggestion_dropped_while_finding_kept(self) -> None:
        patch = "\n".join(
            [
                "@@ -1,2 +1,3 @@",
                " def first():",
                "+    return 1",
                " ",
                "@@ -20,2 +21,3 @@",
                " def second():",
                "+    return 2",
            ]
        )
        parsed = SmartDiffParser().parse_patch_entries(
            [PatchFileEntry(filename="src/multi.py", patch=patch, status="modified")]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        draft = LLMFindingDraft(
            file_path="src/multi.py",
            start_line=2,
            end_line=2,
            severity="WARNING",
            category="n_plus_one_query",
            title="Cross hunk suggestion",
            evidence="return 1",
            suggestion=LLMCodeSuggestionDraft(
                start_line=22, end_line=22, replacement_code="    return 99"
            ),
        )
        valid, dropped, _ = validate_perf_arch_findings([draft], batch=batch)
        assert dropped == 0
        assert len(valid) == 1
        assert valid[0].suggestion is None

    def test_nfr3_ts_backtick_cross_file_and_camelcase_error_key_sparing(self) -> None:
        ts_secret = "ts_backtick_secret_987654"
        py_secret = "correct horse battery staple"
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/auth.ts",
                    patch=(
                        "@@ -1,1 +1,5 @@\n"
                        " export const cfg = {\n"
                        f"+  apiKey: `{ts_secret}`,\n"
                        "+  dynKey: `${process.env.API_KEY}`,\n"
                        '+  errorPassword: "Password must be 8+ chars",\n'
                        "+};\n"
                    ),
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/db_cfg.py",
                    patch=f'@@ -1,1 +1,2 @@\n+DB_PASSWORD = "{py_secret}"\n',
                    status="modified",
                ),
                PatchFileEntry(
                    filename="src/service.py",
                    patch="@@ -1,1 +1,2 @@\n+def connect(pw: str):\n+    return pw\n",
                    status="modified",
                ),
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        drafts = [
            LLMFindingDraft(
                file_path="src/auth.ts",
                start_line=2,
                end_line=4,
                severity="CRITICAL",
                category=f"secret_{ts_secret}",
                title=f"Leaked {ts_secret} and errorPassword='Password must be 8+ chars'",
                evidence=(
                    f"apiKey: `{ts_secret}` dynKey: `${{process.env.API_KEY}}` "
                    'errorPassword: "Password must be 8+ chars"'
                ),
            ),
            LLMFindingDraft(
                file_path="src/service.py",
                start_line=1,
                end_line=2,
                severity="CRITICAL",
                category="hardcoded_secret",
                title=f"Cross-file secret {py_secret}",
                evidence=f"connect uses {py_secret}",
            ),
        ]
        valid, dropped, _ = validate_perf_arch_findings(drafts, batch=batch)
        assert dropped == 0
        assert len(valid) == 2
        assert ts_secret not in valid[0].category
        assert ts_secret not in valid[0].title
        assert ts_secret not in valid[0].evidence
        assert "${process.env.API_KEY}" in valid[0].evidence
        assert "Password must be 8+ chars" in valid[0].evidence
        assert py_secret not in valid[1].title
        assert py_secret not in valid[1].evidence

    def test_per_item_malformed_draft_rejection_keeps_valid_siblings(self) -> None:
        parsed = SmartDiffParser().parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/ok.py",
                    patch="@@ -1,1 +1,2 @@\n def ok():\n+    return 1\n",
                    status="modified",
                )
            ]
        )
        batch = split_parsed_diff_into_batches(parsed)[0][0]
        drafts = [
            LLMFindingDraft(
                file_path="src/ok.py",
                start_line=2,
                end_line=2,
                severity="high",
                category="n_plus_one_query",
                title="Bad severity alias",
                evidence="return 1",
            ),
            LLMFindingDraft(
                file_path="src/ok.py",
                start_line=2,
                end_line=2,
                severity="BOGUS",
                category="n_plus_one_query",
                title="Bogus severity",
                evidence="return 1",
            ),
            LLMFindingDraft(
                file_path="src/ok.py",
                start_line=2,
                end_line=1,
                severity="WARNING",
                category="n_plus_one_query",
                title="Reversed lines",
                evidence="return 1",
            ),
            LLMFindingDraft.model_construct(
                file_path="src/ok.py",
                start_line=True,
                end_line=2,
                severity="WARNING",
                category="n_plus_one_query",
                title="Bool line",
                evidence="return 1",
            ),
            LLMFindingDraft(
                file_path="src/ok.py",
                start_line=2,
                end_line=2,
                severity="WARNING",
                category="n_plus_one_query",
                title="Valid sibling",
                evidence="return 1",
            ),
        ]
        valid, dropped, _ = validate_perf_arch_findings(drafts, batch=batch)
        assert dropped == 4
        assert len(valid) == 1
        assert valid[0].title == "Valid sibling"
