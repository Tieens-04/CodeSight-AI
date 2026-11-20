"""Tree-sitter TypeScript & TSX AST parser extracting NFR-3 CodeSymbolChunk units."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Final, Literal

import structlog
from tree_sitter import Node, Parser

from codesight.ast_engine._ast_utils import (
    DEFAULT_MAX_CHUNK_LINES,
    MIN_CHUNK_LINES,
    append_unique,
    collect_splittable_spans,
    node_line_span,
    node_text,
    pack_spans_into_ranges,
    to_utf8_bytes,
)
from codesight.ast_engine._typescript_ast import (
    TypeScriptDefinitionEvent,
    extract_ts_callee_target,
    get_typescript_grammar,
    is_ts_scope_boundary,
    iter_typescript_definitions,
)
from codesight.ast_engine.complexity import compute_typescript_nodes_cc
from codesight.ast_engine.models import (
    CodeSymbolChunk,
    ParsedASTFile,
    sanitize_signature,
)
from codesight.diff.file_filter import normalize_diff_path

logger = structlog.get_logger("codesight.ast_engine")

_TS_COMPOUND_SPLITTABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "if_statement",
        "else_clause",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "switch_body",
        "switch_case",
        "switch_default",
        "try_statement",
        "catch_clause",
        "finally_clause",
    }
)
_BRACE_TYPE_NODES: Final[frozenset[str]] = frozenset(
    {
        "object_type",
        "mapped_type_clause",
    }
)
_SENSITIVE_TYPE_ALIAS_NODES: Final[frozenset[str]] = _BRACE_TYPE_NODES | frozenset(
    {
        "literal_type",
        "string",
        "template_string",
        "template_literal_type",
        "template_type",
    }
)
_PARAM_DEFAULT_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "required_parameter",
        "optional_parameter",
        "object_assignment_pattern",
        "assignment_pattern",
    }
)


def _extract_string_literal(node: Node | None, source_bytes: bytes) -> str:
    if node is None:
        return ""
    fragments = [
        node_text(child, source_bytes) for child in node.children if child.type == "string_fragment"
    ]
    if fragments:
        return "".join(fragments)
    raw = node_text(node, source_bytes).strip()
    if len(raw) >= 2 and raw[0] in {'"', "'", "`"} and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _extract_import_names(import_node: Node, source_bytes: bytes) -> list[str]:
    source_mod = _extract_string_literal(import_node.child_by_field_name("source"), source_bytes)
    if not source_mod:
        return []

    clause = next((c for c in import_node.children if c.type == "import_clause"), None)
    if clause is None:
        return [source_mod]

    names: list[str] = []
    for child in clause.named_children:
        if child.type == "identifier":
            ident = node_text(child, source_bytes)
            if ident:
                names.append(f"{source_mod}.{ident}")
        elif child.type == "namespace_import":
            names.append(f"{source_mod}.*")
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type == "import_specifier":
                    orig = spec.child_by_field_name("name")
                    sym = node_text(orig, source_bytes) if orig is not None else ""
                    if sym:
                        names.append(f"{source_mod}.{sym}")
    return names or [source_mod]


def _collect_ts_imports(root: Node, source_bytes: bytes) -> tuple[str, ...]:
    collected: list[str] = []
    seen: set[str] = set()
    stack: list[Node] = list(reversed(root.children))
    while stack:
        current = stack.pop()
        if current.type == "import_statement":
            for item in _extract_import_names(current, source_bytes):
                append_unique(collected, seen, item)
        elif current.type in {"internal_module", "module"}:
            body = current.child_by_field_name("body")
            if body is not None:
                stack.extend(reversed(body.children))
    return tuple(collected)


def _extract_jsdoc(start_node: Node, start_line: int, source_bytes: bytes) -> str | None:
    anchor = start_node
    while (
        anchor.prev_named_sibling is None
        and anchor.parent is not None
        and anchor.parent.start_byte == anchor.start_byte
    ):
        anchor = anchor.parent
    prev = anchor.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    prev_end_line = prev.end_point[0] + 1
    if start_line - prev_end_line > 2:
        return None
    raw = node_text(prev, source_bytes).strip()
    if not raw.startswith("/**") or not raw.endswith("*/") or len(raw) < 5:
        return None
    inner = raw[3:-2]
    cleaned_lines: list[str] = []
    for line in inner.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("*"):
            stripped = stripped[1:]
            if stripped.startswith(" "):
                stripped = stripped[1:]
        cleaned_lines.append(stripped)
    doc = inspect.cleandoc("\n".join(cleaned_lines)).strip()
    return doc or None


def _contains_sensitive_type_alias_value(node: Node | None) -> bool:
    if node is None:
        return False
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if (current.is_named and current.type in _SENSITIVE_TYPE_ALIAS_NODES) or current.type in {
            "{",
            "${",
        }:
            return True
        for child in current.children:
            if child.type != "comment":
                stack.append(child)
    return False


def _sanitize_ts_header_span(
    root_node: Node,
    start_byte: int,
    end_byte: int,
    source_bytes: bytes,
    *,
    skip_node: Node | None = None,
) -> str:
    """Extract NFR-3 safe header span stripping comments, decorators, defaults, and object types."""
    if end_byte <= start_byte:
        return ""

    edits: list[tuple[int, int, bytes]] = []
    stack: list[Node] = [
        child
        for child in reversed(root_node.children)
        if child is not skip_node and child.start_byte < end_byte and child.end_byte > start_byte
    ]
    while stack:
        current = stack.pop()
        if current.type in {"comment", "decorator"}:
            c_start = max(current.start_byte, start_byte)
            c_end = min(current.end_byte, end_byte)
            if c_end > c_start:
                edits.append((c_start, c_end, b""))
            continue
        if current.type in _BRACE_TYPE_NODES:
            o_start = max(current.start_byte, start_byte)
            o_end = min(current.end_byte, end_byte)
            if o_end > o_start:
                edits.append((o_start, o_end, b"{...}"))
            continue
        if current.type in _PARAM_DEFAULT_NODE_TYPES:
            eq = next((c for c in current.children if c.type == "="), None)
            if eq is not None:
                eq_start = max(eq.start_byte, start_byte)
                while eq_start > start_byte and source_bytes[eq_start - 1] in (0x20, 0x09):
                    eq_start -= 1
                eq_end = min(current.end_byte, end_byte)
                if eq_end > eq_start:
                    edits.append((eq_start, eq_end, b""))
                for child in reversed(current.children):
                    if (
                        child is not skip_node
                        and child.end_byte <= eq.start_byte
                        and child.end_byte > start_byte
                    ):
                        stack.append(child)
                continue
        for child in reversed(current.children):
            if (
                child is not skip_node
                and child.start_byte < end_byte
                and child.end_byte > start_byte
            ):
                stack.append(child)

    if not edits:
        return source_bytes[start_byte:end_byte].decode("utf-8", errors="replace")

    edits.sort(key=lambda item: (item[0], -item[1]))
    parts: list[bytes] = []
    cursor = start_byte
    for e_start, e_end, replacement in edits:
        if e_start < cursor:
            continue
        if e_start > cursor:
            parts.append(source_bytes[cursor:e_start])
        if replacement:
            parts.append(replacement)
        cursor = max(cursor, e_end)
    if cursor < end_byte:
        parts.append(source_bytes[cursor:end_byte])
    return b"".join(parts).decode("utf-8", errors="replace")


def _balance_wrapper_parens(header: str) -> str:
    depth = 0
    for ch in header:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
    if depth > 0:
        return header + (")" * depth)
    return header


def _extract_ts_signature(event: TypeScriptDefinitionEvent, source_bytes: bytes) -> str:
    """Extract NFR-3 safe single-line TypeScript/TSX header excluding body, defaults, & comments."""
    start_byte = event.header_start_node.start_byte
    skip_node: Node | None = event.body_node

    if event.symbol_kind == "type_alias":
        value_node = event.definition.child_by_field_name("value")
        if _contains_sensitive_type_alias_value(value_node):
            eq_tok = next((c for c in event.definition.children if c.type == "="), None)
            header_end = eq_tok.start_byte if eq_tok is not None else event.definition.end_byte
            skip_node = value_node
        else:
            header_end = event.definition.end_byte
            skip_node = None
        raw = _sanitize_ts_header_span(
            event.outer_node,
            start_byte,
            header_end,
            source_bytes,
            skip_node=skip_node,
        ).strip()
        if raw.endswith(";"):
            raw = raw[:-1].rstrip()
        return sanitize_signature(raw)

    if event.callable_node is not None and event.callable_node.type == "arrow_function":
        arrow_tok = next((c for c in event.callable_node.children if c.type == "=>"), None)
        if arrow_tok is not None:
            header_end = arrow_tok.start_byte
        elif event.body_node is not None:
            header_end = event.body_node.start_byte
        else:
            header_end = event.outer_node.end_byte
    elif event.body_node is not None:
        header_end = event.body_node.start_byte
    else:
        header_end = event.outer_node.end_byte

    raw = _sanitize_ts_header_span(
        event.outer_node,
        start_byte,
        header_end,
        source_bytes,
        skip_node=skip_node,
    ).strip()
    if raw.endswith(";"):
        raw = raw[:-1].rstrip()
    if event.is_callable or event.wrapper_nodes:
        raw = _balance_wrapper_parens(raw)
    return sanitize_signature(raw)


def _extract_jsx_component_name(jsx_node: Node, source_bytes: bytes) -> tuple[int, str] | None:
    name_node = jsx_node.child_by_field_name("name")
    if name_node is None:
        return None
    if name_node.type == "identifier":
        name = node_text(name_node, source_bytes)
        if name and "A" <= name[0] <= "Z":
            return name_node.start_byte, name
        return None
    if name_node.type == "member_expression":
        prop = name_node.child_by_field_name("property")
        if prop is not None:
            prop_name = node_text(prop, source_bytes)
            if prop_name and "A" <= prop_name[0] <= "Z":
                return prop.start_byte, prop_name
    return None


def _extract_ts_called_symbols(
    nodes: Sequence[Node],
    source_bytes: bytes,
    *,
    wrapper_nodes: tuple[Node, ...] = (),
    line_range: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    matches: list[tuple[int, str]] = []

    for wrapper in wrapper_nodes:
        w_line = wrapper.start_point[0] + 1
        if line_range is None or (line_range[0] <= w_line <= line_range[1]):
            extracted = extract_ts_callee_target(
                wrapper.child_by_field_name("function"), source_bytes
            )
            if extracted is not None:
                matches.append(extracted)

    stack: list[Node] = list(reversed(nodes))
    while stack:
        current = stack.pop()
        cur_line = current.start_point[0] + 1
        in_range = line_range is None or (line_range[0] <= cur_line <= line_range[1])

        if in_range:
            if current.type == "call_expression":
                extracted = extract_ts_callee_target(
                    current.child_by_field_name("function"), source_bytes
                )
                if extracted is not None:
                    matches.append(extracted)
            elif current.type == "new_expression":
                extracted = extract_ts_callee_target(
                    current.child_by_field_name("constructor"), source_bytes
                )
                if extracted is not None:
                    matches.append(extracted)
            elif current.type in {"jsx_opening_element", "jsx_self_closing_element"}:
                extracted = _extract_jsx_component_name(current, source_bytes)
                if extracted is not None:
                    matches.append(extracted)

        for child in reversed(current.children):
            if is_ts_scope_boundary(child, source_bytes):
                continue
            stack.append(child)

    matches.sort(key=lambda item: item[0])
    called: list[str] = []
    seen: set[str] = set()
    for _, name in matches:
        append_unique(called, seen, name)
    return tuple(called)


def _collect_ts_body_spans(
    body_node: Node,
    source_bytes: bytes,
    max_lines: int,
) -> list[tuple[int, int]]:
    """Collect AST spans for oversized TS/TSX callables, including concise JSX & curried arrows."""
    current = body_node
    while True:
        if current.type == "parenthesized_expression":
            inner = next((c for c in current.named_children if c.type != "comment"), None)
            if inner is None:
                break
            current = inner
            continue
        if current.type == "arrow_function":
            inner_body = current.child_by_field_name("body")
            if inner_body is None:
                break
            current = inner_body
            continue
        break

    if current.type == "statement_block":
        return collect_splittable_spans(
            current,
            source_bytes,
            max_lines,
            block_type="statement_block",
            compound_splittable_types=_TS_COMPOUND_SPLITTABLE_TYPES,
        )
    if current.type in {"jsx_element", "jsx_fragment"}:
        return [
            node_line_span(child, source_bytes)
            for child in current.named_children
            if child.type not in {"jsx_opening_element", "jsx_closing_element", "comment"}
        ]
    return []


def _collect_ts_call_scope_nodes(
    event: TypeScriptDefinitionEvent,
    source_bytes: bytes,
) -> tuple[Node, ...]:
    nodes: list[Node] = list(event.decorator_nodes)
    if event.callable_node is not None:
        params = event.callable_node.child_by_field_name("parameters")
        if params is not None:
            nodes.append(params)
        if event.body_node is not None:
            nodes.append(event.body_node)
    elif event.symbol_kind == "class":
        for child in event.definition.children:
            if child.type == "class_heritage":
                nodes.append(child)
        if event.body_node is not None:
            pending_decs: list[Node] = []
            for child in event.body_node.children:
                if child.type == "decorator":
                    pending_decs.append(child)
                    continue
                if child.type == "comment":
                    continue
                if child.is_named:
                    if not is_ts_scope_boundary(child, source_bytes):
                        nodes.extend(pending_decs)
                        nodes.append(child)
                    pending_decs = []
    return tuple(nodes)


class TypeScriptASTParser:
    """Parse TypeScript (`.ts`/`.mts`/`.cts`) and TSX (`.tsx`) in RAM into NFR-3 chunks."""

    def __init__(self, *, max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES) -> None:
        if isinstance(max_chunk_lines, bool) or max_chunk_lines < MIN_CHUNK_LINES:
            raise ValueError(f"max_chunk_lines must be an integer >= {MIN_CHUNK_LINES}.")
        self._max_chunk_lines = max_chunk_lines

    def parse(
        self,
        source: str | bytes,
        *,
        file_path: str = "<memory>",
        language: Literal["typescript", "tsx"] | None = None,
    ) -> ParsedASTFile:
        """Extract imports and scope-isolated symbol chunks from TypeScript or TSX source."""
        parsed, _, _ = self._parse_tree(source, file_path=file_path, language=language)
        return parsed

    def _parse_tree(
        self,
        source: str | bytes,
        *,
        file_path: str = "<memory>",
        language: Literal["typescript", "tsx"] | None = None,
    ) -> tuple[ParsedASTFile, Node | None, bytes]:
        """Parse once and return `(ParsedASTFile, root_node, source_bytes)` for reuse."""
        norm_path = normalize_diff_path(file_path).rstrip("/") or "<memory>"
        resolved_lang: Literal["typescript", "tsx"] = (
            language
            if language is not None
            else ("tsx" if norm_path.lower().endswith(".tsx") else "typescript")
        )
        source_bytes = to_utf8_bytes(source)
        if not source_bytes.strip():
            empty = ParsedASTFile(
                file_path=norm_path,
                language=resolved_lang,
                imports=(),
                chunks=(),
                has_syntax_error=False,
            )
            return empty, None, source_bytes

        parser = Parser(get_typescript_grammar(resolved_lang))
        tree = parser.parse(source_bytes)
        root = tree.root_node
        has_syntax_error = root.has_error
        if has_syntax_error:
            logger.warning(
                "ast_typescript_syntax_error_detected",
                file_path=norm_path,
                language=resolved_lang,
                has_syntax_error=True,
            )

        module_imports = _collect_ts_imports(root, source_bytes)
        chunks: list[CodeSymbolChunk] = []

        for event in iter_typescript_definitions(root, source_bytes):
            qualified_name = ".".join(event.scope_stack)
            start_line, _ = node_line_span(event.start_node, source_bytes)
            _, end_line = node_line_span(event.outer_node, source_bytes)
            end_line = max(start_line, end_line)
            signature = _extract_ts_signature(event, source_bytes)
            docstring = _extract_jsdoc(event.start_node, start_line, source_bytes)
            call_scope_nodes = _collect_ts_call_scope_nodes(event, source_bytes)
            body_nodes: list[Node] = [event.body_node] if event.body_node is not None else []
            line_count = max(1, end_line - start_line + 1)

            if (
                event.is_callable
                and event.body_node is not None
                and line_count > self._max_chunk_lines
            ):
                spans = _collect_ts_body_spans(event.body_node, source_bytes, self._max_chunk_lines)
                ranges = pack_spans_into_ranges(
                    spans,
                    outer_start_line=start_line,
                    outer_end_line=end_line,
                    max_lines=self._max_chunk_lines,
                )
                is_split = True
            else:
                ranges = [(start_line, end_line)]
                is_split = False

            for idx, (sub_start, sub_end) in enumerate(ranges):
                win = (sub_start, sub_end) if is_split else None
                cc = (
                    compute_typescript_nodes_cc(body_nodes, source_bytes, line_range=win)
                    if (event.is_callable or event.symbol_kind == "class")
                    else 1
                )
                chunks.append(
                    CodeSymbolChunk(
                        file_path=norm_path,
                        language=resolved_lang,
                        symbol_name=event.symbol_name,
                        qualified_name=qualified_name,
                        symbol_kind=event.symbol_kind,
                        start_line=sub_start,
                        end_line=sub_end,
                        signature=signature,
                        docstring=docstring if idx == 0 else None,
                        decorators=event.decorators,
                        imports=module_imports,
                        called_symbols=_extract_ts_called_symbols(
                            call_scope_nodes,
                            source_bytes,
                            wrapper_nodes=event.wrapper_nodes,
                            line_range=win,
                        ),
                        cyclomatic_complexity=cc,
                        is_async=event.is_async,
                        is_subchunk=is_split,
                        subchunk_index=idx if is_split else None,
                    )
                )

        parsed = ParsedASTFile(
            file_path=norm_path,
            language=resolved_lang,
            imports=module_imports,
            chunks=tuple(chunks),
            has_syntax_error=has_syntax_error,
        )
        return parsed, root, source_bytes
