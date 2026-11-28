"""Unit tests for TokenBudgetPacker priority, overlap merging, hard cap & anti-gaming KPI."""

from __future__ import annotations

import pytest

from codesight.diff.file_filter import estimate_tokens
from codesight.rag.token_packer import (
    DEFAULT_AUX_TOKEN_BUDGET,
    MIN_TRUNCATED_SLICE_TOKENS,
    TRUNCATION_MARKER,
    ContextCandidateSlice,
    TokenBudgetPacker,
)


def _make_lines(prefix: str, start: int, end: int, *, pad: int = 40) -> str:
    return "\n".join(f"{prefix}_{ln}: " + ("x" * pad) for ln in range(start, end + 1))


def test_packer_enforces_tier_priority_enclosing_then_callers_callees_then_docs() -> None:
    """Enclosing symbols always precede direct callers/callees, which precede architecture docs."""
    candidates = [
        ContextCandidateSlice(
            tier="architecture_doc",
            file_path="docs/adr/0001-auth.md",
            source_key="sha-doc",
            start_line=1,
            end_line=4,
            content="# ADR 0001\nAll endpoints must verify JWT.",
            symbol_name="ADR 0001",
            symbol_kind="section",
            signature="ADR 0001",
            relevance_score=0.92,
            full_file_tokens=900,
        ),
        ContextCandidateSlice(
            tier="callee",
            file_path="src/crypto.py",
            source_key="sha-crypto",
            start_line=10,
            end_line=14,
            content="def decode_jwt(raw: str) -> dict:\n    return {}",
            symbol_name="decode_jwt",
            symbol_kind="function",
            signature="def decode_jwt(raw: str) -> dict",
            relevance_score=3.5,
            full_file_tokens=1200,
        ),
        ContextCandidateSlice(
            tier="caller",
            file_path="src/routes/users.py",
            source_key="sha-router",
            start_line=20,
            end_line=24,
            content="def get_user(token: str):\n    return verify_token(token)",
            symbol_name="get_user",
            symbol_kind="function",
            signature="def get_user(token: str)",
            relevance_score=4.5,
            full_file_tokens=1400,
        ),
        ContextCandidateSlice(
            tier="enclosing_symbol",
            file_path="src/auth.py",
            source_key="head-sha",
            start_line=1,
            end_line=6,
            content="def verify_token(token: str) -> bool:\n    return bool(decode_jwt(token))",
            symbol_name="verify_token",
            symbol_kind="function",
            signature="def verify_token(token: str) -> bool",
            relevance_score=1.0,
            full_file_tokens=1600,
        ),
    ]

    packer = TokenBudgetPacker(budget_tokens=DEFAULT_AUX_TOKEN_BUDGET)
    bundle = packer.pack(candidates)

    assert [b.tier for b in bundle.blocks] == [
        "enclosing_symbol",
        "caller",
        "callee",
        "architecture_doc",
    ]
    assert bundle.metrics.packed_aux_tokens == estimate_tokens(bundle.rendered_context)
    assert bundle.metrics.packed_aux_tokens <= DEFAULT_AUX_TOKEN_BUDGET


def test_packer_merges_same_tier_and_trims_lower_tier_overlapping_spans() -> None:
    """Overlapping spans on the same (file_path, source_key) emit zero duplicate source lines."""
    # Enclosing covers L10..L15; Caller on exact same version covers L13..L18 -> trim to L16..L18
    enclosing_lines = "\n".join(f"line_{i} = {i}" for i in range(10, 16))
    caller_lines = "\n".join(f"line_{i} = {i}" for i in range(13, 19))
    # Two overlapping caller chunks on another file: L1..L5 and L4..L8 -> merge to L1..L8
    caller_a = "\n".join(f"other_{i} = {i}" for i in range(1, 6))
    caller_b = "\n".join(f"other_{i} = {i}" for i in range(4, 9))
    # Subsumed callee completely inside enclosing L10..L15 -> dropped cleanly
    subsumed = "\n".join(f"line_{i} = {i}" for i in range(11, 14))

    candidates = [
        ContextCandidateSlice(
            tier="enclosing_symbol",
            file_path="src/auth.py",
            source_key="same-sha",
            start_line=10,
            end_line=15,
            content=enclosing_lines,
            symbol_name="verify_token",
            symbol_kind="function",
            signature="def verify_token()",
            relevance_score=1.0,
            full_file_tokens=1000,
        ),
        ContextCandidateSlice(
            tier="callee",
            file_path="src/auth.py",
            source_key="same-sha",
            start_line=11,
            end_line=13,
            content=subsumed,
            symbol_name="inner_helper",
            symbol_kind="function",
            signature="def inner_helper()",
            relevance_score=2.0,
            full_file_tokens=1000,
        ),
        ContextCandidateSlice(
            tier="caller",
            file_path="src/auth.py",
            source_key="same-sha",
            start_line=13,
            end_line=18,
            content=caller_lines,
            symbol_name="outer_wrapper",
            symbol_kind="function",
            signature="def outer_wrapper()",
            relevance_score=3.0,
            full_file_tokens=1000,
        ),
        ContextCandidateSlice(
            tier="caller",
            file_path="src/routes.py",
            source_key="routes-sha",
            start_line=1,
            end_line=5,
            content=caller_a,
            symbol_name="route_a",
            symbol_kind="function",
            signature="def route_a()",
            relevance_score=2.0,
            full_file_tokens=1200,
        ),
        ContextCandidateSlice(
            tier="caller",
            file_path="src/routes.py",
            source_key="routes-sha",
            start_line=4,
            end_line=8,
            content=caller_b,
            symbol_name="route_b",
            symbol_kind="function",
            signature="def route_b()",
            relevance_score=4.0,
            full_file_tokens=1200,
        ),
    ]

    bundle = TokenBudgetPacker(budget_tokens=3500).pack(candidates)

    # Zero duplicate L13/L14/L15 or L4/L5 across rendered_context
    for ln in range(10, 19):
        assert bundle.rendered_context.count(f"L{ln}: line_{ln} = {ln}") == 1
    for ln in range(1, 9):
        assert bundle.rendered_context.count(f"L{ln}: other_{ln} = {ln}") == 1

    # Enclosing stays L10..L15, trimmed caller becomes L16..L18, merged routes caller becomes L1..L8
    by_file_tier = {(b.file_path, b.tier): (b.start_line, b.end_line) for b in bundle.blocks}
    assert by_file_tier[("src/auth.py", "enclosing_symbol")] == (10, 15)
    assert by_file_tier[("src/auth.py", "caller")] == (16, 18)
    assert by_file_tier[("src/routes.py", "caller")] == (1, 8)


def test_packer_hard_caps_header_inclusive_budget_and_truncates_enclosing_around_focus() -> None:
    """Oversized enclosing symbol truncates cleanly on line boundaries keeping header & focus."""
    lines = [
        "def process_payment(order_id: str, amount: int) -> dict:",
        '    """Process payment safely."""',
        *[
            f"    step_{i} = compute_step({i}, order_id)  # padding {'a' * 30}"
            for i in range(3, 121)
        ],
    ]
    lines[69] = "    CRITICAL_CHANGED_LINE = charge_card(order_id, amount)"
    content = "\n".join(lines)

    candidate = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/billing.py",
        source_key="head",
        start_line=1,
        end_line=120,
        content=content,
        symbol_name="process_payment",
        symbol_kind="function",
        signature="def process_payment(order_id: str, amount: int) -> dict",
        relevance_score=1.0,
        full_file_tokens=3000,
        focus_lines=(70,),
    )

    budget = 450
    bundle = TokenBudgetPacker(budget_tokens=budget).pack([candidate])

    assert len(bundle.blocks) == 1
    assert bundle.blocks[0].truncated is True
    assert bundle.metrics.truncated_blocks_count == 1
    assert estimate_tokens(bundle.rendered_context) <= budget
    assert bundle.metrics.packed_aux_tokens <= budget
    assert TRUNCATION_MARKER in bundle.rendered_context
    assert "L1: def process_payment" in bundle.rendered_context
    assert "L70:     CRITICAL_CHANGED_LINE" in bundle.rendered_context


def test_packer_greedy_fits_smaller_sibling_before_truncating_oversized_caller() -> None:
    """Within one tier, smaller complete callers fit before leftover budget truncates huge ones."""
    huge_caller = ContextCandidateSlice(
        tier="caller",
        file_path="src/huge_caller.py",
        source_key="sha-huge",
        start_line=1,
        end_line=80,
        content=_make_lines("huge", 1, 80, pad=45),
        symbol_name="huge_endpoint",
        symbol_kind="function",
        signature="def huge_endpoint()",
        relevance_score=5.0,
        full_file_tokens=2000,
    )
    small_caller = ContextCandidateSlice(
        tier="caller",
        file_path="src/small_caller.py",
        source_key="sha-small",
        start_line=1,
        end_line=5,
        content="def small_endpoint():\n    return verify_token('abc')\n",
        symbol_name="small_endpoint",
        symbol_kind="function",
        signature="def small_endpoint()",
        relevance_score=4.0,
        full_file_tokens=1500,
    )

    budget = 350
    bundle = TokenBudgetPacker(budget_tokens=budget, min_truncated_tokens=120).pack(
        [huge_caller, small_caller]
    )

    assert estimate_tokens(bundle.rendered_context) <= budget
    assert any(b.symbol_name == "small_endpoint" and not b.truncated for b in bundle.blocks)
    assert any(b.symbol_name == "huge_endpoint" and b.truncated for b in bundle.blocks)


def test_packer_does_not_truncate_below_min_truncated_tokens_threshold() -> None:
    """When leftover budget < min_truncated_tokens, oversized lower-tier items are dropped."""
    enclosing = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/a.py",
        source_key="head",
        start_line=1,
        end_line=12,
        content=_make_lines("enc", 1, 12, pad=36),
        symbol_name="target_fn",
        symbol_kind="function",
        signature="def target_fn()",
        relevance_score=1.0,
        full_file_tokens=1000,
    )
    oversized_doc = ContextCandidateSlice(
        tier="architecture_doc",
        file_path="docs/architecture/design.md",
        source_key="sha-doc",
        start_line=1,
        end_line=40,
        content=_make_lines("doc", 1, 40, pad=40),
        symbol_name="Architecture Rules",
        symbol_kind="section",
        signature="Architecture Rules",
        relevance_score=0.9,
        full_file_tokens=2000,
    )

    enc_only = TokenBudgetPacker(budget_tokens=3500).pack([enclosing])
    tight_budget = enc_only.metrics.packed_aux_tokens + (MIN_TRUNCATED_SLICE_TOKENS - 40)

    bundle = TokenBudgetPacker(
        budget_tokens=tight_budget,
        min_truncated_tokens=MIN_TRUNCATED_SLICE_TOKENS,
    ).pack([enclosing, oversized_doc])

    assert [b.tier for b in bundle.blocks] == ["enclosing_symbol"]
    assert bundle.metrics.dropped_blocks_count == 1
    assert estimate_tokens(bundle.rendered_context) <= tight_budget


def test_packer_anti_gaming_baseline_deduplicates_same_file_and_exceeds_45_pct_savings() -> None:
    """Five symbols inside one file count full_file_tokens ONCE, yet realistic PR saves >= 45%."""
    shared_file_tokens = 2400
    candidates = [
        ContextCandidateSlice(
            tier="enclosing_symbol",
            file_path="src/services/auth_service.py",
            source_key="head-auth",
            start_line=40,
            end_line=52,
            content=_make_lines("auth", 40, 52, pad=24),
            symbol_name="verify_session",
            symbol_kind="function",
            signature="def verify_session(token: str) -> Session",
            relevance_score=1.0,
            full_file_tokens=2600,
        ),
        *[
            ContextCandidateSlice(
                tier="caller",
                file_path="src/api/routes.py",
                source_key="main-routes",
                start_line=10 + idx * 15,
                end_line=16 + idx * 15,
                content=_make_lines(f"route_{idx}", 10 + idx * 15, 16 + idx * 15, pad=20),
                symbol_name=f"endpoint_{idx}",
                symbol_kind="function",
                signature=f"def endpoint_{idx}()",
                relevance_score=4.0 - idx * 0.1,
                full_file_tokens=shared_file_tokens,
            )
            for idx in range(5)
        ],
        ContextCandidateSlice(
            tier="callee",
            file_path="src/crypto/jwt_helper.py",
            source_key="main-jwt",
            start_line=5,
            end_line=14,
            content=_make_lines("jwt", 5, 14, pad=20),
            symbol_name="decode_jwt_claims",
            symbol_kind="function",
            signature="def decode_jwt_claims(token: str) -> dict",
            relevance_score=3.8,
            full_file_tokens=1800,
        ),
        ContextCandidateSlice(
            tier="architecture_doc",
            file_path="docs/adr/0002-session-security.md",
            source_key="main-adr",
            start_line=1,
            end_line=10,
            content=_make_lines("adr", 1, 10, pad=20),
            symbol_name="Session Security Invariants",
            symbol_kind="section",
            signature="Session Security Invariants",
            relevance_score=0.84,
            full_file_tokens=1500,
        ),
    ]

    diff_text = (
        "--- a/src/services/auth_service.py\n"
        "+++ b/src/services/auth_service.py\n"
        "@@ -42,3 +42,4 @@ def verify_session\n"
        " L42:   claims = decode_jwt_claims(token)\n"
        "+L43:   if claims.get('revoked'):\n"
        "+L44:       raise PermissionError('revoked')\n"
    )

    bundle = TokenBudgetPacker(budget_tokens=3500).pack(
        candidates,
        diff_text=diff_text,
        modified_file_tokens={"src/services/auth_service.py": 2600},
        skipped_noise_tokens=0,
    )

    # Anti-gaming check: 4 unique files ONLY (2600 + 2400 + 1800 + 1500 = 8300, never 5*2400)
    assert bundle.metrics.unique_baseline_files == 4
    assert bundle.metrics.baseline_tokens == 2600 + 2400 + 1800 + 1500
    assert bundle.metrics.packed_aux_tokens <= 3500
    assert bundle.metrics.codesight_tokens == (
        bundle.metrics.diff_tokens + bundle.metrics.packed_aux_tokens
    )
    assert bundle.metrics.tokens_saved_pct >= 45.0
    assert bundle.metrics.aux_tokens_saved_pct >= 45.0


def test_packer_handles_empty_tiny_file_and_invalid_init_args() -> None:
    """Empty inputs return 0% savings, tiny negative-savings clamp to 0%, bad args raise."""
    packer = TokenBudgetPacker()
    empty_bundle = packer.pack([])
    assert empty_bundle.blocks == ()
    assert empty_bundle.rendered_context == ""
    assert empty_bundle.metrics.tokens_saved_pct == 0.0
    assert empty_bundle.metrics.baseline_tokens == 0

    tiny = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/tiny.py",
        source_key="head",
        start_line=1,
        end_line=1,
        content="x = 1",
        symbol_name="tiny",
        symbol_kind="function",
        signature="def tiny()",
        relevance_score=1.0,
        full_file_tokens=2,
    )
    tiny_bundle = packer.pack([tiny], diff_tokens=10)
    assert tiny_bundle.metrics.saved_tokens == 0
    assert tiny_bundle.metrics.tokens_saved_pct == 0.0

    with pytest.raises(ValueError):
        TokenBudgetPacker(budget_tokens=0)
    with pytest.raises(ValueError):
        TokenBudgetPacker(budget_tokens=True)
    with pytest.raises(ValueError):
        TokenBudgetPacker(min_truncated_tokens=0)
    with pytest.raises(ValueError):
        packer.pack([], skipped_noise_tokens=-1)
    with pytest.raises(ValueError):
        packer.pack([], diff_tokens=-5)


def test_packer_splits_lower_tier_around_middle_higher_tier_and_truncates_top_without_focus() -> (
    None
):
    """Higher-tier span inside lower-tier span splits cleanly into prefix+suffix with 0 dupes."""
    enclosing = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/core.py",
        source_key="sha-core",
        start_line=15,
        end_line=20,
        content=_make_lines("core", 15, 20, pad=10),
        symbol_name="inner_fn",
        symbol_kind="function",
        signature="def inner_fn()",
        relevance_score=1.0,
        full_file_tokens=1500,
    )
    surrounding_caller = ContextCandidateSlice(
        tier="caller",
        file_path="src/core.py",
        source_key="sha-core",
        start_line=10,
        end_line=25,
        content=_make_lines("core", 10, 25, pad=10),
        symbol_name="OuterClass",
        symbol_kind="class",
        signature="class OuterClass",
        relevance_score=3.0,
        full_file_tokens=1500,
    )

    bundle = TokenBudgetPacker(budget_tokens=3500).pack([enclosing, surrounding_caller])
    ranges = sorted((b.tier, b.start_line, b.end_line) for b in bundle.blocks)
    assert ranges == [
        ("caller", 10, 14),
        ("caller", 21, 25),
        ("enclosing_symbol", 15, 20),
    ]
    for ln in range(10, 26):
        assert bundle.rendered_context.count(f"L{ln}: core_{ln}:") == 1

    # Oversized enclosing without focus_lines truncates cleanly from top on whole-line boundaries
    huge_no_focus = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="web/src/App.tsx",
        source_key="head-ts",
        start_line=1,
        end_line=90,
        content=_make_lines("tsx", 1, 90, pad=40),
        symbol_name="App",
        symbol_kind="function",
        signature="export function App()",
        relevance_score=1.0,
        full_file_tokens=4000,
    )
    trunc_bundle = TokenBudgetPacker(budget_tokens=300).pack(
        [huge_no_focus],
        skipped_noise_tokens=500,
    )
    assert trunc_bundle.blocks[0].truncated is True
    assert estimate_tokens(trunc_bundle.rendered_context) <= 300
    assert "L1: tsx_1:" in trunc_bundle.rendered_context
    assert trunc_bundle.metrics.baseline_tokens == 4500
    assert trunc_bundle.metrics.tokens_saved_pct >= 80.0


def test_packer_multi_focus_keeps_interior_lines_and_only_owns_rendered_lines() -> None:
    """Multi-focus truncation preserves interior lines and avoids erasing elided helpers."""
    lines = [
        "class AuthService:",
        '    """Handles token verification."""',
        *[f"    # padding line {i} {'x' * 36}" for i in range(3, 121)],
    ]
    lines[19] = "    CHANGED_START = parse_header(raw)"  # L20
    lines[20] = "    INTERIOR_GUARD_1 = validate_nonce(CHANGED_START)"  # L21
    lines[21] = "    INTERIOR_GUARD_2 = check_clock_skew(INTERIOR_GUARD_1)"  # L22
    lines[22] = "    INTERIOR_GUARD_3 = normalize_scope(INTERIOR_GUARD_2)"  # L23
    lines[23] = "    CHANGED_END = issue_claim(INTERIOR_GUARD_3)"  # L24

    enclosing = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/auth_service.py",
        source_key="head-sha",
        start_line=1,
        end_line=120,
        content="\n".join(lines),
        symbol_name="AuthService",
        symbol_kind="class",
        signature="class AuthService",
        relevance_score=10.0,
        full_file_tokens=2500,
        focus_lines=(20, 24),
    )

    bundle = TokenBudgetPacker(budget_tokens=320).pack([enclosing])
    assert len(bundle.blocks) == 1
    assert bundle.blocks[0].truncated is True
    assert estimate_tokens(bundle.rendered_context) <= 320
    # Interior context lines L21..L23 between L20 and L24 MUST be preserved, never replaced by '...'
    for expected in (
        "L20:     CHANGED_START",
        "L21:     INTERIOR_GUARD_1",
        "L22:     INTERIOR_GUARD_2",
        "L23:     INTERIOR_GUARD_3",
        "L24:     CHANGED_END",
    ):
        assert expected in bundle.rendered_context

    # Elided gap test: wide_enclosing L1..L100 focused at L95 leaves L10..L11 elided behind '...'.
    # A small Tier-2 helper at L10..L11 on the same (file_path, source_key) fits in residual bytes
    # and MUST NOT be erased by owned_lines over-marking.
    wide_lines = [
        "class WideService:",
        '    """Wide service."""',
        *[f"    # wide padding {i} {'z' * 190}" for i in range(3, 101)],
    ]
    wide_lines[94] = "    FOCUS_LINE = helper_gap()"
    wide_enclosing = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/wide.py",
        source_key="head-wide",
        start_line=1,
        end_line=100,
        content="\n".join(wide_lines),
        symbol_name="WideService",
        symbol_kind="class",
        signature="class WideService",
        relevance_score=10.0,
        full_file_tokens=4000,
        focus_lines=(95,),
    )
    tiny_gap_callee = ContextCandidateSlice(
        tier="callee",
        file_path="src/wide.py",
        source_key="head-wide",
        start_line=10,
        end_line=11,
        content="def helper_gap():\n    return 1",
        symbol_name="helper_gap",
        symbol_kind="function",
        signature="def helper_gap()",
        relevance_score=5.0,
        full_file_tokens=4000,
    )
    gap_bundle = TokenBudgetPacker(budget_tokens=250, min_truncated_tokens=100).pack(
        [wide_enclosing, tiny_gap_callee]
    )
    assert estimate_tokens(gap_bundle.rendered_context) <= 250
    assert any(b.symbol_name == "helper_gap" for b in gap_bundle.blocks)
    assert "L10: def helper_gap():" in gap_bundle.rendered_context


def test_packer_preserves_adjacent_raw_hunk_fallback_slices_without_overwriting() -> None:
    """Two raw diff hunk slices (use_line_numbers=False) must never merge or overwrite lines."""
    hunk_1 = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="deploy/config.yaml",
        source_key="hunk_fallback",
        start_line=10,
        end_line=14,
        content="\n".join(
            [
                "@@ -10,6 +10,5 @@ timeout_settings",
                "-old_a: 1",
                "-old_b: 2",
                "-old_c: 3",
                "-old_d: 4",
                " L10: timeout: 30",
                "+L11: retries: 5",
                " L12: keep_1: true",
                " L13: keep_2: true",
                " L14: HUNK_1_TAIL_SENTINEL",
            ]
        ),
        symbol_name="timeout_settings",
        symbol_kind="hunk",
        signature="timeout_settings",
        relevance_score=9.0,
        use_line_numbers=False,
    )
    hunk_2 = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="deploy/config.yaml",
        source_key="hunk_fallback",
        start_line=18,
        end_line=20,
        content="\n".join(
            [
                "@@ -18,2 +18,3 @@ pool_settings",
                " L18: pool_size: 16",
                "+L19: overflow: 8",
                " L20: HUNK_2_TAIL_SENTINEL",
            ]
        ),
        symbol_name="pool_settings",
        symbol_kind="hunk",
        signature="pool_settings",
        relevance_score=9.0,
        use_line_numbers=False,
    )

    bundle = TokenBudgetPacker(budget_tokens=3500).pack([hunk_1, hunk_2])
    assert len(bundle.blocks) == 2
    assert "HUNK_1_TAIL_SENTINEL" in bundle.rendered_context
    assert "HUNK_2_TAIL_SENTINEL" in bundle.rendered_context
    assert "@@ -18,2 +18,3 @@ pool_settings" in bundle.rendered_context


def test_packer_enforces_exact_utf8_byte_budget_with_multibyte_vietnamese_and_emoji() -> None:
    """Multi-byte UTF-8 Vietnamese/emoji content strictly respects estimate_tokens <= budget."""
    lines = [
        f"def kiem_tra_bao_mat_{i}() -> str:  # Kiểm tra chữ ký số & bảo mật 🔐🚀"
        for i in range(1, 81)
    ]
    cand = ContextCandidateSlice(
        tier="enclosing_symbol",
        file_path="src/bao_mat.py",
        source_key="head-utf8",
        start_line=1,
        end_line=80,
        content="\n".join(lines),
        symbol_name="kiem_tra_bao_mat",
        symbol_kind="function",
        signature="def kiem_tra_bao_mat() -> str",
        relevance_score=10.0,
        full_file_tokens=3000,
        focus_lines=(40, 42),
    )

    for budget in (150, 211, 333):
        bundle = TokenBudgetPacker(budget_tokens=budget, min_truncated_tokens=100).pack([cand])
        assert bundle.metrics.packed_aux_tokens == estimate_tokens(bundle.rendered_context)
        assert estimate_tokens(bundle.rendered_context) <= budget
        assert "L40: def kiem_tra_bao_mat_40" in bundle.rendered_context
        assert "L41: def kiem_tra_bao_mat_41" in bundle.rendered_context
        assert "L42: def kiem_tra_bao_mat_42" in bundle.rendered_context
