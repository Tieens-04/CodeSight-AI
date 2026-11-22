"""Unit tests for ASTParserRegistry routing and safe RAM-only Hunk Context Fallback."""

from __future__ import annotations

import pytest
from tree_sitter import Language

from codesight.ast_engine import (
    ASTParserRegistry,
    HunkContextSlice,
    PRFileContext,
)
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser


@pytest.mark.parametrize(
    ("file_path", "expected_lang"),
    [
        ("src/app/main.py", "python"),
        ("src\\app\\MAIN.PY", "python"),
        ("src/services/auth.ts", "typescript"),
        ("src/services/esm_client.mts", "typescript"),
        ("src/services/cjs_client.cts", "typescript"),
        ("src/components/UserCard.tsx", "tsx"),
        ("src\\components\\Modal.TSX", "tsx"),
        # Ambient declaration stubs MUST fall back
        ("src/types/env.d.ts", None),
        ("src/types/esm.d.mts", None),
        ("src/types/cjs.d.cts", None),
        ("src/types/GLOBAL.D.TS", None),
        ("src/app/stubs.pyi", None),
        # JS and non-AST formats MUST fall back
        ("src/legacy/helper.js", None),
        ("src/legacy/View.jsx", None),
        ("src/legacy/util.mjs", None),
        ("db/schema.sql", None),
        ("docker-compose.yml", None),
        ("Dockerfile", None),
        ("README.md", None),
    ],
)
def test_registry_resolve_language_and_tree_sitter_language(
    file_path: str,
    expected_lang: str | None,
) -> None:
    registry = ASTParserRegistry()

    assert registry.resolve_language(file_path) == expected_lang
    assert registry.supports(file_path) is (expected_lang is not None)

    ts_lang = registry.get_tree_sitter_language(file_path)
    if expected_lang is None:
        assert ts_lang is None
    else:
        assert isinstance(ts_lang, Language)


def test_registry_parse_file_routes_py_ts_tsx_and_returns_none_for_unsupported() -> None:
    registry = ASTParserRegistry()

    py_parsed = registry.parse_file("def ping():\n    return 1\n", file_path="src/ping.py")
    assert py_parsed is not None
    assert py_parsed.language == "python"
    assert [c.symbol_name for c in py_parsed.chunks] == ["ping"]

    ts_parsed = registry.parse_file(
        "export const add = (a: number, b: number) => a + b;\n",
        file_path="src/math.mts",
    )
    assert ts_parsed is not None
    assert ts_parsed.language == "typescript"
    assert [c.symbol_name for c in ts_parsed.chunks] == ["add"]

    tsx_parsed = registry.parse_file(
        "export const Badge = () => <Icon />;\n",
        file_path="src/Badge.tsx",
    )
    assert tsx_parsed is not None
    assert tsx_parsed.language == "tsx"
    assert tsx_parsed.chunks[0].called_symbols == ("Icon",)

    assert (
        registry.parse_file("export declare function x(): void;\n", file_path="src/x.d.ts") is None
    )
    assert registry.parse_file("SELECT * FROM users;\n", file_path="db/query.sql") is None
    assert registry.resolve_language("") is None


def test_registry_resolve_pr_file_ast_mode_filters_touched_chunks_and_computes_delta() -> None:
    before_src = "\n".join(
        [
            "export const untouched = (x: number) => x;",
            "",
            "export function processOrder(amount: number): number {",
            "  return amount;",
            "}",
            "",
        ]
    )
    after_src = "\n".join(
        [
            "export const untouched = (x: number) => x;",
            "",
            "export function processOrder(amount: number): number {",
            "  if (amount > 100 && amount < 1000) {",
            "    return applyDiscount(amount);",
            "  }",
            "  return amount;",
            "}",
            "",
        ]
    )
    patch = "\n".join(
        [
            "@@ -3,3 +3,6 @@ export function processOrder(amount: number): number {",
            " export function processOrder(amount: number): number {",
            "+  if (amount > 100 && amount < 1000) {",
            "+    return applyDiscount(amount);",
            "+  }",
            "   return amount;",
            " }",
        ]
    )
    pr_diff = SmartDiffParser().parse_patch_entries(
        [PatchFileEntry(filename="src/orders.ts", patch=patch, status="modified")]
    )
    file_diff = pr_diff.files[0]

    ctx = ASTParserRegistry().resolve_pr_file(
        file_diff,
        after_source=after_src,
        before_source=before_src,
    )

    assert isinstance(ctx, PRFileContext)
    assert ctx.mode == "ast"
    assert ctx.language == "typescript"
    assert ctx.fallback_reason is None
    assert ctx.hunk_slices == ()
    assert ctx.parsed_file is not None
    assert [c.symbol_name for c in ctx.touched_chunks] == ["processOrder"]
    assert ctx.complexity is not None
    assert ctx.complexity.net_delta_cc == 2
    assert ctx.complexity.deltas[0].qualified_name == "processOrder"


def test_registry_resolve_pr_file_falls_back_to_hunk_context_without_crashing() -> None:
    sql_patch = "\n".join(
        [
            "@@ -10,3 +10,5 @@ CREATE TABLE users (",
            "   id UUID PRIMARY KEY,",
            "+  email TEXT NOT NULL,",
            "+  created_at TIMESTAMPTZ DEFAULT now(),",
            "   active BOOLEAN",
        ]
    )
    pr_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(filename="db/schema.sql", patch=sql_patch, status="modified"),
            PatchFileEntry(filename="src/types/env.d.ts", patch=sql_patch, status="modified"),
            PatchFileEntry(filename="src/orders.ts", patch=sql_patch, status="modified"),
            PatchFileEntry(filename="src/old.py", patch=sql_patch, status="deleted"),
        ]
    )
    registry = ASTParserRegistry()

    # 1. Unsupported extension (.sql) -> safe Hunk Context Fallback
    sql_ctx = registry.resolve_pr_file(pr_diff.files[0], after_source="CREATE TABLE users ();")
    assert sql_ctx.mode == "hunk_fallback"
    assert sql_ctx.fallback_reason == "unsupported_extension"
    assert sql_ctx.language is None
    assert sql_ctx.parsed_file is None
    assert len(sql_ctx.hunk_slices) == 1
    hunk_slice = sql_ctx.hunk_slices[0]
    assert isinstance(hunk_slice, HunkContextSlice)
    assert hunk_slice.file_path == "db/schema.sql"
    assert hunk_slice.hunk_id == 1
    assert hunk_slice.start_line == 10
    assert hunk_slice.end_line == 14
    assert "CREATE TABLE users" in hunk_slice.section_header
    assert "email TEXT NOT NULL" in hunk_slice.annotated_text
    # NFR-3 type safety: HunkContextSlice must NEVER be serializable as a Qdrant code payload
    assert not hasattr(hunk_slice, "to_qdrant_payload")

    # 2. Ambient .d.ts -> safe Hunk Context Fallback
    dts_ctx = registry.resolve_pr_file(pr_diff.files[1], after_source="declare const x: string;")
    assert dts_ctx.mode == "hunk_fallback"
    assert dts_ctx.fallback_reason == "unsupported_extension"

    # 3. Supported extension (.ts) with missing after_source -> safe Hunk Context Fallback
    missing_ctx = registry.resolve_pr_file(pr_diff.files[2], after_source=None)
    assert missing_ctx.mode == "hunk_fallback"
    assert missing_ctx.fallback_reason == "missing_source"
    assert missing_ctx.language == "typescript"
    assert len(missing_ctx.hunk_slices) == 1

    # 4. Deleted supported file (.py) -> safe Hunk Context Fallback
    deleted_ctx = registry.resolve_pr_file(pr_diff.files[3], after_source="def gone(): pass")
    assert deleted_ctx.mode == "hunk_fallback"
    assert deleted_ctx.fallback_reason == "deleted_file"


def test_registry_contains_unexpected_parser_failure_without_crashing_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ASTParserRegistry()

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated grammar crash")

    monkeypatch.setattr(registry._ts_parser, "_parse_tree", _boom)

    # parse_file degrades gracefully to ParsedASTFile(has_syntax_error=True)
    degraded = registry.parse_file("export const x = 1;", file_path="src/crash.ts")
    assert degraded is not None
    assert degraded.chunks == ()
    assert degraded.has_syntax_error is True

    # resolve_pr_file degrades gracefully to hunk_fallback
    patch = "@@ -1,1 +1,2 @@\n export const x = 1;\n+export const y = 2;"
    file_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/crash.ts", patch=patch, status="modified")]
        )
        .files[0]
    )
    ctx = registry.resolve_pr_file(
        file_diff,
        after_source="export const x = 1;\nexport const y = 2;",
    )
    assert ctx.mode == "hunk_fallback"
    assert ctx.fallback_reason == "parse_failure"
    assert len(ctx.hunk_slices) == 1

    # Also verify AST mode with pure context-only diff (no changed_right_lines)
    empty_patch_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/clean.py",
                    patch="@@ -1,1 +1,1 @@\n def ok(): pass",
                    status="modified",
                )
            ]
        )
        .files[0]
    )
    clean_ctx = ASTParserRegistry().resolve_pr_file(empty_patch_diff, after_source="def ok(): pass")
    assert clean_ctx.mode == "ast"
    assert clean_ctx.touched_chunks == ()


def test_registry_resolve_pr_file_detects_u3_deletion_only_hunk_inside_function() -> None:
    """Regression test for F-02: -U3 deletion-only hunk inside a function must mark it touched."""
    before_src = "\n".join(
        [
            "export function untouched(): number {",
            "  return 1;",
            "}",
            "",
            "export async function withdraw(user: User, amount: number): Promise<void> {",
            "  await authorize(user);",
            "  await transfer(amount);",
            "}",
            "",
        ]
    )
    after_src = "\n".join(
        [
            "export function untouched(): number {",
            "  return 1;",
            "}",
            "",
            "export async function withdraw(user: User, amount: number): Promise<void> {",
            "  await transfer(amount);",
            "}",
            "",
        ]
    )
    patch = "\n".join(
        [
            "@@ -4,5 +4,4 @@",
            " ",
            " export async function withdraw(user: User, amount: number): Promise<void> {",
            "-  await authorize(user);",
            "   await transfer(amount);",
            " }",
        ]
    )
    file_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/bank.ts", patch=patch, status="modified")]
        )
        .files[0]
    )
    ctx = ASTParserRegistry().resolve_pr_file(
        file_diff,
        after_source=after_src,
        before_source=before_src,
    )
    assert ctx.mode == "ast"
    assert [c.symbol_name for c in ctx.touched_chunks] == ["withdraw"]
    assert ctx.complexity is not None
    assert [d.qualified_name for d in ctx.complexity.deltas] == ["withdraw"]
    assert ctx.complexity.deltas[0].change_kind == "modified"

    # Deletion strictly between top-level functions must NOT falsely mark neighbors touched
    between_patch = "\n".join(
        [
            "@@ -2,5 +2,4 @@",
            "   return 1;",
            " }",
            "-const UNUSED_CONST = 42;",
            " ",
            " export async function withdraw(user: User, amount: number): Promise<void> {",
        ]
    )
    between_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/bank.ts", patch=between_patch, status="modified")]
        )
        .files[0]
    )
    between_ctx = ASTParserRegistry().resolve_pr_file(
        between_diff,
        after_source=after_src,
        before_source=after_src,
    )
    assert between_ctx.mode == "ast"
    assert between_ctx.touched_chunks == ()
    assert between_ctx.complexity is not None and between_ctx.complexity.deltas == ()

    # Empty source in AST mode stays safe with zero chunks
    empty_ctx = ASTParserRegistry().resolve_pr_file(
        between_diff,
        after_source="   \n",
        before_source=None,
    )
    assert empty_ctx.mode == "ast"
    assert empty_ctx.touched_chunks == ()
    assert empty_ctx.complexity is not None and empty_ctx.complexity.deltas == ()


def test_registry_resolve_pr_file_validates_threshold_handles_tsx_diff_and_ts_to_tsx_rename() -> (
    None
):
    """Regression tests for F-07, F-08, and F-09."""
    tsx_before = "\n".join(
        [
            "export const Card = ({ active }: { active: boolean }) => {",
            "  return <Badge />;",
            "};",
            "",
        ]
    )
    tsx_after = "\n".join(
        [
            "export const Card = ({ active }: { active: boolean }) => {",
            "  return active ? <ActiveBadge /> : <Badge />;",
            "};",
            "",
        ]
    )
    tsx_patch = "\n".join(
        [
            "@@ -1,3 +1,3 @@",
            " export const Card = ({ active }: { active: boolean }) => {",
            "-  return <Badge />;",
            "+  return active ? <ActiveBadge /> : <Badge />;",
            " };",
        ]
    )
    tsx_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/ui/Card.tsx", patch=tsx_patch, status="modified")]
        )
        .files[0]
    )
    registry = ASTParserRegistry()

    # F-07: invalid high_cc_threshold must raise ValueError instead of falling back to parse_failure
    with pytest.raises(ValueError, match="high_cc_threshold"):
        registry.resolve_pr_file(tsx_diff, after_source=tsx_after, high_cc_threshold=-1)
    with pytest.raises(ValueError, match="high_cc_threshold"):
        registry.resolve_pr_file(tsx_diff, after_source=tsx_after, high_cc_threshold=True)

    # F-08: real .tsx diff routes through TSX grammar for chunks + complexity delta
    tsx_ctx = registry.resolve_pr_file(
        tsx_diff,
        after_source=tsx_after,
        before_source=tsx_before,
    )
    assert tsx_ctx.mode == "ast"
    assert tsx_ctx.language == "tsx"
    assert tsx_ctx.parsed_file is not None
    assert tsx_ctx.parsed_file.has_syntax_error is False
    assert [c.symbol_name for c in tsx_ctx.touched_chunks] == ["Card"]
    assert tsx_ctx.complexity is not None
    assert tsx_ctx.complexity.has_syntax_error is False
    assert tsx_ctx.complexity.net_delta_cc == 1

    # F-09: cross-extension rename `.ts` -> `.tsx` parses before_source with `.ts` grammar
    rename_before = "export const identity = <T>(val: T): T => val;\n"
    rename_after = "export const identity = <T,>(val: T): T => (val ? val : val);\n"
    rename_patch = (
        "@@ -1,1 +1,1 @@\n"
        "-export const identity = <T>(val: T): T => val;\n"
        "+export const identity = <T,>(val: T): T => (val ? val : val);"
    )
    rename_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [
                PatchFileEntry(
                    filename="src/utils/identity.tsx",
                    previous_filename="src/utils/identity.ts",
                    patch=rename_patch,
                    status="renamed",
                )
            ]
        )
        .files[0]
    )
    rename_ctx = registry.resolve_pr_file(
        rename_diff,
        after_source=rename_after,
        before_source=rename_before,
    )
    assert rename_ctx.mode == "ast"
    assert rename_ctx.complexity is not None
    assert rename_ctx.complexity.has_syntax_error is False
    assert rename_ctx.complexity.net_delta_cc == 1


def test_registry_resolve_pr_file_parses_after_source_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for F-07: resolve_pr_file avoids double tree-sitter parse on after_source."""
    from tree_sitter import Parser as TSParser

    parse_calls = 0
    original_parse = TSParser.parse

    def _counting_parse(self: TSParser, *args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TSParser, "parse", _counting_parse)

    patch = (
        "@@ -0,0 +1,3 @@\n"
        "+export function newlyAdded(x: number): number {\n"
        "+  return x > 0 ? x : 0;\n"
        "+}"
    )
    src = "export function newlyAdded(x: number): number {\n  return x > 0 ? x : 0;\n}\n"
    file_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/new_fn.ts", patch=patch, status="added")]
        )
        .files[0]
    )
    ctx = ASTParserRegistry().resolve_pr_file(file_diff, after_source=src, before_source=None)
    assert ctx.mode == "ast"
    assert [c.symbol_name for c in ctx.touched_chunks] == ["newlyAdded"]
    assert ctx.complexity is not None and ctx.complexity.net_delta_cc == 2
    assert parse_calls == 1


def test_count_syntax_error_nodes_and_ts_removed_symbols_with_localized_syntax_error() -> None:
    from tree_sitter import Parser as TSParser

    from codesight.ast_engine._ast_utils import count_syntax_error_nodes
    from codesight.ast_engine._python_ast import PYTHON_LANGUAGE
    from codesight.ast_engine._typescript_ast import TYPESCRIPT_LANGUAGE

    clean_ts = TSParser(TYPESCRIPT_LANGUAGE).parse(b"export function ok(): number { return 1; }\n")
    assert count_syntax_error_nodes(clean_ts.root_node) == 0

    broken_ts = TSParser(TYPESCRIPT_LANGUAGE).parse(
        b"export function ok(): number { return 1; }\nexport const broken = ;\n"
    )
    assert count_syntax_error_nodes(broken_ts.root_node) >= 1

    missing_py = TSParser(PYTHON_LANGUAGE).parse(b"def broken(:\n    pass\n")
    assert count_syntax_error_nodes(missing_py.root_node) >= 1

    # Localized TS syntax error in baseline must not blank out valid sibling removed_symbols
    before_ts = (
        "export function legacyHelper(): number {\n"
        "  return 1;\n"
        "}\n\n"
        "export function keepMe(): number {\n"
        "  return 2;\n"
        "}\n\n"
        "export const broken = ;\n"
    )
    after_ts_fixed = "export function keepMe(): number {\n  return 2;\n}\n"
    after_ts_same_err = (
        "export function keepMe(): number {\n  return 2;\n}\n\nexport const broken = ;\n"
    )
    patch = (
        "@@ -1,5 +1,1 @@\n"
        "-export function legacyHelper(): number {\n"
        "-  return 1;\n"
        "-}\n"
        "-\n"
        " export function keepMe(): number {\n"
    )
    file_diff = (
        SmartDiffParser()
        .parse_patch_entries(
            [PatchFileEntry(filename="src/helpers.ts", patch=patch, status="modified")]
        )
        .files[0]
    )
    registry = ASTParserRegistry()

    ctx_fixed = registry.resolve_pr_file(
        file_diff,
        after_source=after_ts_fixed,
        before_source=before_ts,
    )
    assert ctx_fixed.removed_symbols == ("legacyHelper",)

    ctx_same_err = registry.resolve_pr_file(
        file_diff,
        after_source=after_ts_same_err,
        before_source=before_ts,
    )
    assert ctx_same_err.removed_symbols == ("legacyHelper",)

    # Clean before + newly introduced syntax error breaking legacyHelper in after -> empty removed
    clean_before = (
        "export function legacyHelper(): number {\n"
        "  return 1;\n"
        "}\n\n"
        "export function keepMe(): number {\n"
        "  return 2;\n"
        "}\n"
    )
    broken_after = (
        "export function legacyHelper( {\n"
        "  return 1;\n"
        "}\n\n"
        "export function keepMe(): number {\n"
        "  return 2;\n"
        "}\n"
    )
    ctx_new_err = registry.resolve_pr_file(
        file_diff,
        after_source=broken_after,
        before_source=clean_before,
    )
    assert ctx_new_err.removed_symbols == ()
