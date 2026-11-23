"""Tree-sitter Python AST parser extracting NFR-3 compliant CodeSymbolChunk units."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Final

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
from codesight.ast_engine._python_ast import (
    NESTED_SCOPE_TYPES,
    PYTHON_LANGUAGE,
    iter_python_definitions,
)
from codesight.ast_engine.complexity import compute_python_nodes_cc
from codesight.ast_engine.models import (
    CodeSymbolChunk,
    ParsedASTFile,
    SymbolKind,
    sanitize_signature,
)
from codesight.diff.file_filter import normalize_diff_path

logger = structlog.get_logger("codesight.ast_engine")

_IMPORT_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "import_statement",
        "import_from_statement",
        "future_import_statement",
    }
)
_COMPOUND_SPLITTABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "for_statement",
        "while_statement",
        "try_statement",
        "except_clause",
        "finally_clause",
        "with_statement",
        "match_statement",
        "case_clause",
    }
)
_ALLOWED_DOCSTRING_PREFIX_CHARS: Final[frozenset[str]] = frozenset({"r", "u"})
_FASTAPI_DI_CALLABLES: Final[frozenset[str]] = frozenset({"Depends", "Security"})
_PYTHON_DEFAULT_PARAM_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {"default_parameter", "typed_default_parameter"}
)


def _qualify_import(module_prefix: str, symbol: str) -> str:
    if not module_prefix:
        return symbol
    if module_prefix.endswith("."):
        return f"{module_prefix}{symbol}"
    return f"{module_prefix}.{symbol}"


def _extract_named_import_symbols(import_node: Node, source_bytes: bytes) -> list[str]:
    symbols: list[str] = []
    for child in import_node.children_by_field_name("name"):
        if child.type in {"dotted_name", "identifier"}:
            sym = node_text(child, source_bytes)
        elif child.type == "aliased_import":
            orig = child.child_by_field_name("name")
            sym = node_text(orig, source_bytes) if orig is not None else ""
        else:
            sym = ""
        if sym:
            symbols.append(sym)
    return symbols


def _extract_import_names(import_node: Node, source_bytes: bytes) -> list[str]:
    if import_node.type == "import_statement":
        return _extract_named_import_symbols(import_node, source_bytes)

    if import_node.type == "future_import_statement":
        return [
            _qualify_import("__future__", sym)
            for sym in _extract_named_import_symbols(import_node, source_bytes)
        ]

    if import_node.type == "import_from_statement":
        mod_node = import_node.child_by_field_name("module_name")
        module_prefix = node_text(mod_node, source_bytes) if mod_node is not None else ""
        if any(child.type == "wildcard_import" for child in import_node.children):
            return [_qualify_import(module_prefix, "*")]
        return [
            _qualify_import(module_prefix, sym)
            for sym in _extract_named_import_symbols(import_node, source_bytes)
        ]
    return []


def _collect_imports_from_node(
    root: Node,
    source_bytes: bytes,
    *,
    descend_into_definitions: bool,
) -> tuple[str, ...]:
    collected: list[str] = []
    seen: set[str] = set()
    stack: list[Node] = [root]
    while stack:
        current = stack.pop()
        if current.type in _IMPORT_NODE_TYPES:
            for item in _extract_import_names(current, source_bytes):
                append_unique(collected, seen, item)
            continue

        for child in reversed(current.children):
            if not descend_into_definitions and child.type in NESTED_SCOPE_TYPES:
                continue
            stack.append(child)
    return tuple(collected)


def _extract_plain_string_text(string_node: Node, source_bytes: bytes) -> str | None:
    if string_node.type == "string":
        start_node = next((c for c in string_node.children if c.type == "string_start"), None)
        if start_node is not None:
            prefix = node_text(start_node, source_bytes).strip("\"'").lower()
            if not set(prefix) <= _ALLOWED_DOCSTRING_PREFIX_CHARS:
                return None
        if any(child.type == "interpolation" for child in string_node.children):
            return None
        return "".join(
            node_text(part, source_bytes)
            for part in string_node.children
            if part.type == "string_content"
        )

    if string_node.type == "concatenated_string":
        parts: list[str] = []
        for child in string_node.named_children:
            if child.type == "comment":
                continue
            part_text = _extract_plain_string_text(child, source_bytes)
            if part_text is None:
                return None
            parts.append(part_text)
        return "".join(parts) if parts else None

    return None


def _extract_docstring(body_node: Node | None, source_bytes: bytes) -> str | None:
    if body_node is None:
        return None
    for stmt in body_node.named_children:
        if stmt.type == "comment":
            continue
        if stmt.type != "expression_statement":
            return None
        exprs = [child for child in stmt.named_children if child.type != "comment"]
        if len(exprs) != 1 or exprs[0].type not in {"string", "concatenated_string"}:
            return None
        text = _extract_plain_string_text(exprs[0], source_bytes)
        if text is None:
            return None
        return inspect.cleandoc(text).strip()
    return None


def _extract_callable_identifiers(
    node: Node | None,
    source_bytes: bytes,
    *,
    enclosing_class: str | None = None,
) -> list[tuple[int, str]]:
    if node is None:
        return []
    if node.type == "identifier":
        name = node_text(node, source_bytes)
        return [(node.start_byte, name)] if name else []
    if node.type == "attribute":
        attr = node.child_by_field_name("attribute")
        if attr is None:
            return []
        attr_name = node_text(attr, source_bytes)
        if not attr_name:
            return []
        matches: list[tuple[int, str]] = [(attr.start_byte, attr_name)]
        obj = node.child_by_field_name("object")
        if obj is not None and obj.type == "identifier":
            receiver = node_text(obj, source_bytes)
            if receiver in {"self", "cls"}:
                if enclosing_class:
                    matches.append((attr.start_byte, f"{enclosing_class}.{attr_name}"))
            elif receiver:
                matches.append((attr.start_byte, f"{receiver}.{attr_name}"))
        return matches
    return []


def _extract_callee_names(
    call_node: Node,
    source_bytes: bytes,
    *,
    enclosing_class: str | None = None,
) -> list[tuple[int, str]]:
    return _extract_callable_identifiers(
        call_node.child_by_field_name("function"),
        source_bytes,
        enclosing_class=enclosing_class,
    )


def _extract_fastapi_di_targets(
    call_node: Node,
    callee_names: Sequence[tuple[int, str]],
    source_bytes: bytes,
    *,
    enclosing_class: str | None = None,
) -> list[tuple[int, str]]:
    """Extract the injected callable passed to FastAPI `Depends(fn)` or `Security(fn)`."""
    if not callee_names or callee_names[0][1] not in _FASTAPI_DI_CALLABLES:
        return []
    args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return []
    target_expr: Node | None = None
    for child in args_node.named_children:
        if child.type == "comment":
            continue
        if child.type == "keyword_argument":
            kw_name = child.child_by_field_name("name")
            if kw_name is not None and node_text(kw_name, source_bytes) == "dependency":
                target_expr = child.child_by_field_name("value")
                break
            continue
        target_expr = child
        break
    return _extract_callable_identifiers(
        target_expr,
        source_bytes,
        enclosing_class=enclosing_class,
    )


def _extract_called_symbols(
    nodes: Sequence[Node],
    source_bytes: bytes,
    *,
    enclosing_class: str | None = None,
    line_range: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    matches: list[tuple[int, str]] = []
    stack: list[Node] = list(reversed(nodes))
    while stack:
        current = stack.pop()
        if current.type == "call":
            call_line = current.start_point[0] + 1
            if line_range is None or (line_range[0] <= call_line <= line_range[1]):
                extracted = _extract_callee_names(
                    current,
                    source_bytes,
                    enclosing_class=enclosing_class,
                )
                if extracted:
                    matches.extend(extracted)
                    matches.extend(
                        _extract_fastapi_di_targets(
                            current,
                            extracted,
                            source_bytes,
                            enclosing_class=enclosing_class,
                        )
                    )
        for child in reversed(current.children):
            if child.type in NESTED_SCOPE_TYPES:
                continue
            stack.append(child)

    matches.sort(key=lambda item: item[0])
    called: list[str] = []
    seen: set[str] = set()
    for _, name in matches:
        append_unique(called, seen, name)
    return tuple(called)


def _collect_definition_call_scope_nodes(
    definition: Node,
    outer_node: Node,
    body_node: Node | None,
) -> tuple[Node, ...]:
    """Collect AST subtrees belonging directly to a symbol (decorators, header, body)."""
    nodes: list[Node] = []
    if outer_node.type == "decorated_definition":
        nodes.extend(child for child in outer_node.children if child.type == "decorator")
    if definition.type == "function_definition":
        params = definition.child_by_field_name("parameters")
        if params is not None:
            nodes.append(params)
    elif definition.type == "class_definition":
        superclasses = definition.child_by_field_name("superclasses")
        if superclasses is not None:
            nodes.append(superclasses)
    if body_node is not None:
        nodes.append(body_node)
    return tuple(nodes)


def _collect_python_param_edits(
    params_node: Node,
    start_byte: int,
    end_byte: int,
) -> list[tuple[int, int, bytes]]:
    edits: list[tuple[int, int, bytes]] = []
    if params_node.start_byte >= end_byte or params_node.end_byte <= start_byte:
        return edits
    open_paren = next((c for c in params_node.children if c.type == "("), None)
    close_paren = next((c for c in reversed(params_node.children) if c.type == ")"), None)
    if open_paren is None or close_paren is None:
        return edits
    named_params = [c for c in params_node.named_children if c.type != "comment"]
    if named_params:
        lead_start = max(open_paren.end_byte, start_byte)
        lead_end = min(named_params[0].start_byte, end_byte)
        if lead_end > lead_start:
            edits.append((lead_start, lead_end, b""))
        tail_start = max(named_params[-1].end_byte, start_byte)
        tail_end = min(close_paren.start_byte, end_byte)
        if tail_end > tail_start:
            edits.append((tail_start, tail_end, b""))
    else:
        empty_start = max(open_paren.end_byte, start_byte)
        empty_end = min(close_paren.start_byte, end_byte)
        if empty_end > empty_start:
            edits.append((empty_start, empty_end, b""))
    return edits


def _strip_python_header_span(
    definition: Node,
    start_byte: int,
    end_byte: int,
    source_bytes: bytes,
    *,
    skip_node: Node | None = None,
) -> str:
    """Extract NFR-3 safe Python header stripping comments and parameter default values."""
    if end_byte <= start_byte:
        return ""

    skip_id = skip_node.id if skip_node is not None else None
    params_node = definition.child_by_field_name("parameters")
    params_id = params_node.id if params_node is not None else None
    edits: list[tuple[int, int, bytes]] = []
    if params_node is not None:
        edits.extend(_collect_python_param_edits(params_node, start_byte, end_byte))

    stack: list[Node] = [
        child
        for child in reversed(definition.children)
        if child.id != skip_id and child.start_byte < end_byte and child.end_byte > start_byte
    ]
    while stack:
        current = stack.pop()
        if current.type == "comment":
            c_start = max(current.start_byte, start_byte)
            c_end = min(current.end_byte, end_byte)
            if c_end > c_start:
                edits.append((c_start, c_end, b""))
            continue
        parent = current.parent
        if (
            params_id is not None
            and parent is not None
            and parent.id == params_id
            and current.type in _PYTHON_DEFAULT_PARAM_NODE_TYPES
        ):
            eq = next((c for c in current.children if c.type == "="), None)
            if eq is not None:
                prev_end = max(
                    (
                        c.end_byte
                        for c in current.children
                        if c.end_byte <= eq.start_byte and c.type != "comment"
                    ),
                    default=current.start_byte,
                )
                eq_start = max(prev_end, start_byte)
                eq_end = min(current.end_byte, end_byte)
                if eq_end > eq_start:
                    edits.append((eq_start, eq_end, b""))
                for child in reversed(current.children):
                    if (
                        child.id != skip_id
                        and child.end_byte <= eq.start_byte
                        and child.end_byte > start_byte
                    ):
                        stack.append(child)
                continue
        for child in reversed(current.children):
            if child.id != skip_id and child.start_byte < end_byte and child.end_byte > start_byte:
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


def _extract_signature(definition: Node, body_node: Node | None, source_bytes: bytes) -> str:
    """Extract NFR-3 safe single-line header excluding body, parameter defaults, and comments."""
    colon_start = next((c.start_byte for c in definition.children if c.type == ":"), None)
    if colon_start is not None:
        header_end = colon_start
    elif body_node is not None:
        header_end = body_node.start_byte
    else:
        header_end = definition.end_byte

    raw_header = _strip_python_header_span(
        definition,
        definition.start_byte,
        header_end,
        source_bytes,
        skip_node=body_node,
    )
    return sanitize_signature(raw_header)


class PythonASTParser:
    """Parse Python source in RAM via `tree-sitter-python` into NFR-3 CodeSymbolChunks."""

    def __init__(self, *, max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES) -> None:
        if isinstance(max_chunk_lines, bool) or max_chunk_lines < MIN_CHUNK_LINES:
            raise ValueError(f"max_chunk_lines must be an integer >= {MIN_CHUNK_LINES}.")
        self._max_chunk_lines = max_chunk_lines

    def parse(self, source: str | bytes, *, file_path: str = "<memory>") -> ParsedASTFile:
        """Extract module imports and scope-isolated symbol chunks from Python source."""
        parsed, _, _ = self._parse_tree(source, file_path=file_path)
        return parsed

    def _parse_tree(
        self,
        source: str | bytes,
        *,
        file_path: str = "<memory>",
    ) -> tuple[ParsedASTFile, Node | None, bytes]:
        """Parse once and return `(ParsedASTFile, root_node, source_bytes)` for reuse."""
        norm_path = normalize_diff_path(file_path).rstrip("/") or "<memory>"
        source_bytes = to_utf8_bytes(source)
        if not source_bytes.strip():
            empty = ParsedASTFile(
                file_path=norm_path,
                language="python",
                imports=(),
                chunks=(),
                has_syntax_error=False,
            )
            return empty, None, source_bytes

        parser = Parser(PYTHON_LANGUAGE)
        tree = parser.parse(source_bytes)
        root = tree.root_node
        has_syntax_error = root.has_error
        if has_syntax_error:
            logger.warning(
                "ast_python_syntax_error_detected",
                file_path=norm_path,
                has_syntax_error=True,
            )

        module_imports = _collect_imports_from_node(
            root,
            source_bytes,
            descend_into_definitions=False,
        )
        scope_imports: dict[tuple[str, ...], tuple[str, ...]] = {(): module_imports}
        scope_classes: dict[tuple[str, ...], str | None] = {(): None}
        chunks: list[CodeSymbolChunk] = []

        for event in iter_python_definitions(root, source_bytes):
            parent_imports = scope_imports.get(event.scope_stack[:-1], module_imports)
            parent_class = scope_classes.get(event.scope_stack[:-1])
            merged_imports = list(parent_imports)
            seen_imports = set(parent_imports)
            if event.body_node is not None:
                local_imports = _collect_imports_from_node(
                    event.body_node,
                    source_bytes,
                    descend_into_definitions=False,
                )
                for item in local_imports:
                    append_unique(merged_imports, seen_imports, item)
            imports_tuple = tuple(merged_imports)
            scope_imports[event.scope_stack] = imports_tuple
            if event.definition.type == "class_definition":
                scope_classes[event.scope_stack] = event.symbol_name
                enclosing_class_for_calls: str | None = None
            else:
                scope_classes[event.scope_stack] = parent_class
                enclosing_class_for_calls = parent_class

            qualified_name = ".".join(event.scope_stack)
            start_line, end_line = node_line_span(event.outer_node, source_bytes)
            signature = _extract_signature(event.definition, event.body_node, source_bytes)
            docstring = _extract_docstring(event.body_node, source_bytes)
            call_scope_nodes = _collect_definition_call_scope_nodes(
                event.definition,
                event.outer_node,
                event.body_node,
            )
            body_nodes: list[Node] = [event.body_node] if event.body_node is not None else []

            if event.definition.type == "class_definition":
                chunks.append(
                    CodeSymbolChunk(
                        file_path=norm_path,
                        language="python",
                        symbol_name=event.symbol_name,
                        qualified_name=qualified_name,
                        symbol_kind="class",
                        start_line=start_line,
                        end_line=end_line,
                        signature=signature,
                        docstring=docstring,
                        decorators=event.decorators,
                        imports=imports_tuple,
                        called_symbols=_extract_called_symbols(
                            call_scope_nodes,
                            source_bytes,
                            enclosing_class=enclosing_class_for_calls,
                        ),
                        cyclomatic_complexity=compute_python_nodes_cc(body_nodes, source_bytes),
                        is_async=False,
                        is_subchunk=False,
                        subchunk_index=None,
                    )
                )
                continue

            symbol_kind: SymbolKind = "method" if event.enclosing_kind == "class" else "function"
            line_count = max(1, end_line - start_line + 1)

            if event.body_node is None or line_count <= self._max_chunk_lines:
                chunks.append(
                    CodeSymbolChunk(
                        file_path=norm_path,
                        language="python",
                        symbol_name=event.symbol_name,
                        qualified_name=qualified_name,
                        symbol_kind=symbol_kind,
                        start_line=start_line,
                        end_line=end_line,
                        signature=signature,
                        docstring=docstring,
                        decorators=event.decorators,
                        imports=imports_tuple,
                        called_symbols=_extract_called_symbols(
                            call_scope_nodes,
                            source_bytes,
                            enclosing_class=enclosing_class_for_calls,
                        ),
                        cyclomatic_complexity=compute_python_nodes_cc(body_nodes, source_bytes),
                        is_async=event.is_async,
                        is_subchunk=False,
                        subchunk_index=None,
                    )
                )
            else:
                spans = collect_splittable_spans(
                    event.body_node,
                    source_bytes,
                    self._max_chunk_lines,
                    block_type="block",
                    compound_splittable_types=_COMPOUND_SPLITTABLE_TYPES,
                )
                ranges = pack_spans_into_ranges(
                    spans,
                    outer_start_line=start_line,
                    outer_end_line=end_line,
                    max_lines=self._max_chunk_lines,
                )
                for idx, (sub_start, sub_end) in enumerate(ranges):
                    chunks.append(
                        CodeSymbolChunk(
                            file_path=norm_path,
                            language="python",
                            symbol_name=event.symbol_name,
                            qualified_name=qualified_name,
                            symbol_kind=symbol_kind,
                            start_line=sub_start,
                            end_line=sub_end,
                            signature=signature,
                            docstring=docstring if idx == 0 else None,
                            decorators=event.decorators,
                            imports=imports_tuple,
                            called_symbols=_extract_called_symbols(
                                call_scope_nodes,
                                source_bytes,
                                enclosing_class=enclosing_class_for_calls,
                                line_range=(sub_start, sub_end),
                            ),
                            cyclomatic_complexity=compute_python_nodes_cc(
                                body_nodes,
                                source_bytes,
                                line_range=(sub_start, sub_end),
                            ),
                            is_async=event.is_async,
                            is_subchunk=True,
                            subchunk_index=idx,
                        )
                    )

        parsed = ParsedASTFile(
            file_path=norm_path,
            language="python",
            imports=module_imports,
            chunks=tuple(chunks),
            has_syntax_error=has_syntax_error,
        )
        return parsed, root, source_bytes
