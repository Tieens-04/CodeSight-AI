"""Static Python AST complexity analysis, delta CC calculation, and P_untested detector."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final, Literal

from tree_sitter import Node, Parser

from codesight.ast_engine._python_ast import (
    NESTED_SCOPE_TYPES,
    PYTHON_LANGUAGE,
    iter_python_definitions,
    node_line_span,
    node_text,
    to_utf8_bytes,
)
from codesight.ast_engine.models import (
    ComplexityDelta,
    ComplexityDiffSummary,
    FunctionComplexity,
    TestCompanionReport,
)
from codesight.diff.file_filter import CompiledGlob, compile_posix_glob, normalize_diff_path
from codesight.diff.models import DiffLine, ParsedFileDiff, ParsedPullRequestDiff

_CC_BRANCH_TYPES: Final[frozenset[str]] = frozenset(
    {
        "if_statement",
        "elif_clause",
        "conditional_expression",
        "for_statement",
        "while_statement",
        "except_clause",
        "boolean_operator",
        "for_in_clause",
    }
)
_COMPREHENSION_PARENTS: Final[frozenset[str]] = frozenset(
    {
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
    }
)
_NESTING_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {
        "if_statement",
        "for_statement",
        "while_statement",
        "try_statement",
        "with_statement",
        "match_statement",
    }
)
_IGNORED_PARAM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "positional_separator",
        "keyword_separator",
        "comment",
    }
)

DEFAULT_SOURCE_GLOBS: Final[tuple[str, ...]] = ("src/**",)
DEFAULT_TEST_GLOBS: Final[tuple[str, ...]] = (
    "tests/**",
    "test/**",
    "**/__tests__/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.*",
    "**/*.spec.*",
)

_PYTHON_EXTENSIONS: Final[frozenset[str]] = frozenset({".py", ".pyi"})
_C_STYLE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mts",
        ".cts",
        ".mjs",
        ".cjs",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".kt",
    }
)

_DOCSTRING_OPEN_RE: Final[re.Pattern[str]] = re.compile(
    r'^(?:[rRuUbBfF]{0,2})(?P<quote>"""|\'\'\')(?P<rest>.*)$'
)
_EXPR_OPENER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[=(\[{,:+\-*/%&|^~<>]|(?:\b(?:return|yield|raise|assert|in|and|or|not|is|else|elif|if)\b))\s*$"
)


def _decorator_base_name(decorator_text: str) -> str:
    return decorator_text.split("(", 1)[0].strip()


def _is_overload_stub(func_node: Node, decorators: Sequence[str], source_bytes: bytes) -> bool:
    if not any(
        _decorator_base_name(dec) in {"@overload", "@typing.overload"} for dec in decorators
    ):
        return False
    body = func_node.child_by_field_name("body")
    if body is None:
        return False
    stmts = [child for child in body.named_children if child.type != "comment"]
    for stmt in stmts:
        if stmt.type in {"pass_statement", "ellipsis"}:
            continue
        if stmt.type == "expression_statement":
            inner = [child for child in stmt.named_children if child.type != "comment"]
            if len(inner) == 1 and inner[0].type in {"ellipsis", "string"}:
                continue
        if node_text(stmt, source_bytes).strip() == "...":
            continue
        return False
    return True


def _is_bare_wildcard_case(case_node: Node, source_bytes: bytes) -> bool:
    if case_node.child_by_field_name("guard") is not None:
        return False
    patterns = [child for child in case_node.children if child.type == "case_pattern"]
    return len(patterns) == 1 and node_text(patterns[0], source_bytes).strip() == "_"


def compute_python_nodes_cc(
    nodes: Sequence[Node],
    source_bytes: bytes,
    *,
    line_range: tuple[int, int] | None = None,
) -> int:
    """Compute McCabe Cyclomatic Complexity over Python AST nodes (isolating nested scopes).

    Counts base 1 plus branching nodes: ``if``, ``elif``, ternary, ``for``/``async for``,
    ``while``, ``except``/``except*``, non-bare-wildcard ``case``, boolean ``and``/``or``,
    and comprehension ``for``/``if`` clauses. When ``line_range`` is provided, only branch
    nodes starting within that inclusive 1-based line window increment complexity.
    """
    cc = 1
    stack: list[Node] = list(reversed(nodes))
    while stack:
        current = stack.pop()
        in_range = line_range is None or (
            line_range[0] <= current.start_point[0] + 1 <= line_range[1]
        )
        if in_range:
            node_type = current.type
            if node_type in _CC_BRANCH_TYPES:
                cc += 1
            elif node_type == "if_clause":
                parent = current.parent
                if parent is not None and parent.type in _COMPREHENSION_PARENTS:
                    cc += 1
            elif node_type == "case_clause":
                if not _is_bare_wildcard_case(current, source_bytes):
                    cc += 1

        for child in reversed(current.children):
            if child.type in NESTED_SCOPE_TYPES:
                continue
            stack.append(child)
    return cc


def compute_max_nesting_depth(body_node: Node | None) -> int:
    """Compute maximum control-flow block nesting depth inside a function body."""
    if body_node is None:
        return 0

    max_depth = 0
    stack: list[tuple[Node, int]] = [(body_node, 0)]
    while stack:
        current, depth = stack.pop()
        for child in reversed(current.children):
            if child.type in NESTED_SCOPE_TYPES:
                continue
            child_depth = depth + 1 if child.type in _NESTING_BLOCK_TYPES else depth
            if child_depth > max_depth:
                max_depth = child_depth
            stack.append((child, child_depth))
    return max_depth


def _leading_param_name(param_node: Node, source_bytes: bytes) -> str | None:
    if param_node.type == "identifier":
        return node_text(param_node, source_bytes)
    if param_node.type in {"list_splat_pattern", "dictionary_splat_pattern"}:
        return None
    name_child = param_node.child_by_field_name("name")
    if name_child is not None and name_child.type == "identifier":
        return node_text(name_child, source_bytes)
    for child in param_node.named_children:
        if child.type == "identifier":
            return node_text(child, source_bytes)
    return None


def count_python_parameters(
    func_node: Node,
    source_bytes: bytes,
    *,
    is_method: bool,
    decorators: Sequence[str],
) -> int:
    """Count declared Python parameters, ignoring `/` `*` separators and `self`/`cls`."""
    params_node = func_node.child_by_field_name("parameters")
    if params_node is None:
        return 0

    params = [
        child for child in params_node.named_children if child.type not in _IGNORED_PARAM_TYPES
    ]
    if not params:
        return 0

    is_staticmethod = any(
        _decorator_base_name(dec) in {"@staticmethod", "@builtins.staticmethod"}
        for dec in decorators
    )
    if is_method and not is_staticmethod:
        first_name = _leading_param_name(params[0], source_bytes)
        if first_name in {"self", "cls"}:
            return len(params) - 1
    return len(params)


class PythonComplexityAnalyzer:
    """Compute McCabe Cyclomatic Complexity, nesting depth, parameter count, and line counts."""

    def analyze_source(self, source: str | bytes) -> tuple[FunctionComplexity, ...]:
        """Analyze every non-overload function and method in `source`."""
        metrics, _ = self._analyze_with_status(source)
        return metrics

    def _analyze_with_status(
        self, source: str | bytes
    ) -> tuple[tuple[FunctionComplexity, ...], bool]:
        source_bytes = to_utf8_bytes(source)
        if not source_bytes.strip():
            return (), False

        parser = Parser(PYTHON_LANGUAGE)
        tree = parser.parse(source_bytes)
        root = tree.root_node
        results: list[FunctionComplexity] = []

        for event in iter_python_definitions(root, source_bytes):
            if event.definition.type != "function_definition":
                continue
            if _is_overload_stub(event.definition, event.decorators, source_bytes):
                continue

            is_method = event.enclosing_kind == "class"
            symbol_kind: Literal["function", "method"] = "method" if is_method else "function"
            start_line, end_line = node_line_span(event.outer_node, source_bytes)
            line_count = max(1, end_line - start_line + 1)
            body_nodes: list[Node] = [event.body_node] if event.body_node is not None else []
            cc = compute_python_nodes_cc(body_nodes, source_bytes)
            nesting = compute_max_nesting_depth(event.body_node)
            param_count = count_python_parameters(
                event.definition,
                source_bytes,
                is_method=is_method,
                decorators=event.decorators,
            )

            results.append(
                FunctionComplexity(
                    symbol_name=event.symbol_name,
                    qualified_name=".".join(event.scope_stack),
                    symbol_kind=symbol_kind,
                    start_line=start_line,
                    end_line=end_line,
                    line_count=line_count,
                    cyclomatic_complexity=cc,
                    max_nesting_depth=nesting,
                    parameter_count=param_count,
                    is_async=event.is_async,
                    has_syntax_error=event.outer_node.has_error,
                )
            )
        return tuple(results), root.has_error


def _index_by_occurrence(
    items: Sequence[FunctionComplexity],
) -> dict[tuple[str, int], FunctionComplexity]:
    counts: dict[str, int] = {}
    indexed: dict[tuple[str, int], FunctionComplexity] = {}
    for item in items:
        occ = counts.get(item.qualified_name, 0)
        counts[item.qualified_name] = occ + 1
        indexed[(item.qualified_name, occ)] = item
    return indexed


def _touches_changed_lines(
    fn: FunctionComplexity,
    sorted_changed_lines: Sequence[int],
) -> bool:
    idx = bisect_left(sorted_changed_lines, fn.start_line)
    return idx < len(sorted_changed_lines) and sorted_changed_lines[idx] <= fn.end_line


def compute_complexity_delta(
    before_source: str | bytes | None,
    after_source: str | bytes | None,
    *,
    changed_right_lines: AbstractSet[int] | None = None,
    high_cc_threshold: int = 10,
) -> ComplexityDiffSummary:
    """Compute per-function Cyclomatic Complexity deltas between before and after revisions."""
    if isinstance(high_cc_threshold, bool) or high_cc_threshold < 0:
        raise ValueError("high_cc_threshold must be a non-negative integer.")
    analyzer = PythonComplexityAnalyzer()
    before_list, before_has_error = (
        analyzer._analyze_with_status(before_source) if before_source is not None else ((), False)
    )
    after_list, after_has_error = (
        analyzer._analyze_with_status(after_source) if after_source is not None else ((), False)
    )
    has_syntax_error = before_has_error or after_has_error
    sorted_changed_lines = sorted(changed_right_lines) if changed_right_lines is not None else None

    before_map = _index_by_occurrence(before_list)
    after_map = _index_by_occurrence(after_list)

    ordered_keys: list[tuple[str, int]] = list(after_map.keys())
    for key in before_map:
        if key not in after_map:
            ordered_keys.append(key)

    deltas: list[ComplexityDelta] = []
    for key in ordered_keys:
        before_fn = before_map.get(key)
        after_fn = after_map.get(key)

        if (before_fn is not None and before_fn.has_syntax_error) or (
            after_fn is not None and after_fn.has_syntax_error
        ):
            continue

        if before_fn is None and after_fn is not None:
            if sorted_changed_lines is None or _touches_changed_lines(
                after_fn, sorted_changed_lines
            ):
                deltas.append(
                    ComplexityDelta(
                        symbol_name=after_fn.symbol_name,
                        qualified_name=after_fn.qualified_name,
                        change_kind="added",
                        before_cc=0,
                        after_cc=after_fn.cyclomatic_complexity,
                        delta_cc=after_fn.cyclomatic_complexity,
                        before_metrics=None,
                        after_metrics=after_fn,
                    )
                )
        elif after_fn is None and before_fn is not None:
            if after_has_error:
                continue
            deltas.append(
                ComplexityDelta(
                    symbol_name=before_fn.symbol_name,
                    qualified_name=before_fn.qualified_name,
                    change_kind="removed",
                    before_cc=before_fn.cyclomatic_complexity,
                    after_cc=0,
                    delta_cc=-before_fn.cyclomatic_complexity,
                    before_metrics=before_fn,
                    after_metrics=None,
                )
            )
        elif before_fn is not None and after_fn is not None:
            cc_changed = before_fn.cyclomatic_complexity != after_fn.cyclomatic_complexity
            touched = sorted_changed_lines is not None and _touches_changed_lines(
                after_fn, sorted_changed_lines
            )
            if cc_changed or touched:
                deltas.append(
                    ComplexityDelta(
                        symbol_name=after_fn.symbol_name,
                        qualified_name=after_fn.qualified_name,
                        change_kind="modified",
                        before_cc=before_fn.cyclomatic_complexity,
                        after_cc=after_fn.cyclomatic_complexity,
                        delta_cc=after_fn.cyclomatic_complexity - before_fn.cyclomatic_complexity,
                        before_metrics=before_fn,
                        after_metrics=after_fn,
                    )
                )

    scoped_after = [
        fn
        for fn in after_list
        if not fn.has_syntax_error
        and (sorted_changed_lines is None or _touches_changed_lines(fn, sorted_changed_lines))
    ]
    net_delta = sum(d.delta_cc for d in deltas)
    max_delta = max((d.delta_cc for d in deltas), default=0)
    max_after = max((fn.cyclomatic_complexity for fn in scoped_after), default=0)
    high_cc = tuple(fn for fn in scoped_after if fn.cyclomatic_complexity > high_cc_threshold)
    return ComplexityDiffSummary(
        deltas=tuple(deltas),
        net_delta_cc=net_delta,
        max_delta_cc=max_delta,
        max_after_cc=max_after,
        high_complexity_functions=high_cc,
        has_syntax_error=has_syntax_error,
    )


def _compile_globs(patterns: Sequence[str]) -> tuple[CompiledGlob, ...]:
    return tuple(compile_posix_glob(pat) for pat in patterns)


def _matches_any(normalized_path: str, matchers: Sequence[CompiledGlob]) -> bool:
    return any(matcher.matches(normalized_path) for matcher in matchers)


def _detect_unclosed_expr_triple_quote(stripped: str) -> str | None:
    idx = 0
    length = len(stripped)
    in_single_or_double: str | None = None
    in_triple: str | None = None

    while idx < length:
        if in_triple is not None:
            if stripped[idx] == "\\":
                idx += 2
                continue
            if stripped.startswith(in_triple, idx):
                in_triple = None
                idx += 3
                continue
            idx += 1
            continue

        if in_single_or_double is not None:
            if stripped[idx] == "\\":
                idx += 2
                continue
            if stripped[idx] == in_single_or_double:
                in_single_or_double = None
            idx += 1
            continue

        if stripped[idx] == "#":
            break
        if stripped.startswith('"""', idx):
            in_triple = '"""'
            idx += 3
        elif stripped.startswith("'''", idx):
            in_triple = "'''"
            idx += 3
        elif stripped[idx] in ("'", '"'):
            in_single_or_double = stripped[idx]
            idx += 1
        else:
            idx += 1

    return in_triple


def _find_triple_quote_positions(stripped: str, quote: str) -> list[int]:
    """Return indices of `quote` outside matched quotes and inline `#` comments."""
    if quote not in stripped:
        return []
    other_triple = "'''" if quote == '"""' else '"""'
    positions: list[int] = []
    idx = 0
    length = len(stripped)
    while idx < length:
        ch = stripped[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == "#":
            break
        if stripped.startswith(quote, idx):
            positions.append(idx)
            idx += 3
            continue
        if stripped.startswith(other_triple, idx):
            close = stripped.find(other_triple, idx + 3)
            if close != -1:
                idx = close + 3
                continue
        if ch in ("'", '"'):
            scan = idx + 1
            closed_at = -1
            while scan < length:
                if stripped[scan] == "\\":
                    scan += 2
                    continue
                if stripped[scan] == ch:
                    closed_at = scan
                    break
                scan += 1
            if closed_at != -1:
                idx = closed_at + 1
                continue
        idx += 1
    return positions


def _is_closing_triple_quote_line(
    stripped: str,
    first_pos: int,
    prev_nonempty: str | None,
) -> bool:
    before = stripped[:first_pos].strip()
    after = stripped[first_pos + 3 :].strip()

    if before:
        if len(before) <= 2 and set(before.lower()) <= {"r", "u", "b", "f"}:
            return False
        return _EXPR_OPENER_RE.search(before) is None

    if after and not after.startswith(("#", ")", "]", "}", ",", ".")):
        return False

    return prev_nonempty is None or not prev_nonempty.endswith(":")


def _infer_hunk_initial_docstring_quote(
    lines: Sequence[DiffLine],
    new_start: int,
) -> str | None:
    if new_start <= 1:
        return None

    active_lines = [line.content.strip() for line in lines if line.kind in {"context", "add"}]
    best: tuple[int, str] | None = None
    for quote in ('"""', "'''"):
        total = 0
        first_odd: tuple[int, str, int, str | None] | None = None
        prev_nonempty: str | None = None
        for idx, stripped in enumerate(active_lines):
            if stripped and not stripped.startswith("#"):
                positions = _find_triple_quote_positions(stripped, quote)
                total += len(positions)
                if len(positions) % 2 == 1 and first_odd is None:
                    first_odd = (idx, stripped, positions[0], prev_nonempty)
            if stripped:
                prev_nonempty = stripped
        if total % 2 == 1 and first_odd is not None:
            idx, stripped, pos, prev = first_odd
            if _is_closing_triple_quote_line(stripped, pos, prev) and (
                best is None or idx < best[0]
            ):
                best = (idx, quote)
    return best[1] if best is not None else None


def _count_added_logic_lines(file_diff: ParsedFileDiff, normalized_path: str) -> int:
    suffix = Path(normalized_path).suffix.lower()
    is_python = suffix in _PYTHON_EXTENSIONS
    is_c_style = suffix in _C_STYLE_EXTENSIONS
    use_hash_comments = is_python or not is_c_style
    use_c_comments = is_c_style or not is_python

    added_logic = 0
    for hunk in file_diff.hunks:
        initial_doc_quote = (
            _infer_hunk_initial_docstring_quote(hunk.lines, hunk.new_start) if is_python else None
        )
        multiline_mode: Literal["docstring", "expr"] | None = (
            "docstring" if initial_doc_quote is not None else None
        )
        multiline_quote: str | None = initial_doc_quote
        in_block_comment = False

        for diff_line in hunk.lines:
            if diff_line.kind not in {"context", "add"}:
                continue
            is_add = diff_line.kind == "add"
            stripped = diff_line.content.strip()

            if in_block_comment:
                if "*/" in stripped:
                    in_block_comment = False
                continue

            if multiline_mode == "docstring":
                if multiline_quote is not None and multiline_quote in stripped:
                    multiline_mode = None
                    multiline_quote = None
                continue

            if multiline_mode == "expr":
                if is_add and stripped:
                    added_logic += 1
                if multiline_quote is not None and multiline_quote in stripped:
                    multiline_mode = None
                    multiline_quote = None
                continue

            if not stripped:
                continue

            if use_hash_comments and stripped.startswith("#"):
                continue

            if use_c_comments and stripped.startswith("//"):
                continue

            if use_c_comments and stripped.startswith("/*"):
                if "*/" not in stripped[2:]:
                    in_block_comment = True
                continue

            if is_python:
                doc_match = _DOCSTRING_OPEN_RE.match(stripped)
                if doc_match is not None:
                    quote = doc_match.group("quote")
                    rest = doc_match.group("rest")
                    if quote not in rest:
                        multiline_mode = "docstring"
                        multiline_quote = quote
                        continue
                    after_close = rest.split(quote, 1)[1].strip()
                    if not after_close or after_close.startswith("#"):
                        continue

                unclosed_expr = _detect_unclosed_expr_triple_quote(stripped)
                if unclosed_expr is not None:
                    multiline_mode = "expr"
                    multiline_quote = unclosed_expr

            if is_add:
                added_logic += 1

    return added_logic


def detect_missing_test_companion(
    pr_diff: ParsedPullRequestDiff | Sequence[ParsedFileDiff],
    *,
    logic_line_threshold: int = 50,
    source_globs: Sequence[str] = DEFAULT_SOURCE_GLOBS,
    test_globs: Sequence[str] = DEFAULT_TEST_GLOBS,
) -> TestCompanionReport:
    """Detect PRs adding > `logic_line_threshold` source logic lines without companion tests."""
    if isinstance(logic_line_threshold, bool) or logic_line_threshold < 0:
        raise ValueError("logic_line_threshold must be a non-negative integer.")
    files = pr_diff.files if isinstance(pr_diff, ParsedPullRequestDiff) else pr_diff
    test_matchers = _compile_globs(test_globs)
    source_matchers = _compile_globs(source_globs)

    added_source_logic = 0
    added_test_logic = 0
    source_files: list[str] = []
    test_files: list[str] = []

    for parsed_file in files:
        if parsed_file.change_type == "deleted":
            continue
        norm_path = normalize_diff_path(parsed_file.file_path).rstrip("/")
        if not norm_path:
            continue

        logic_lines = _count_added_logic_lines(parsed_file, norm_path)
        if _matches_any(norm_path, test_matchers):
            added_test_logic += logic_lines
            test_files.append(norm_path)
        elif _matches_any(norm_path, source_matchers):
            added_source_logic += logic_lines
            source_files.append(norm_path)

    is_untested = added_source_logic > logic_line_threshold and added_test_logic == 0
    return TestCompanionReport(
        added_source_logic_lines=added_source_logic,
        added_test_logic_lines=added_test_logic,
        threshold=logic_line_threshold,
        is_untested=is_untested,
        source_files=tuple(source_files),
        test_files=tuple(test_files),
    )


test_companion_detector = detect_missing_test_companion
detect_missing_test_companion.__test__ = False  # type: ignore[attr-defined]
