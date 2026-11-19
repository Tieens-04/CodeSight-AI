"""Shared tree-sitter-python utilities and iterative AST definition walker."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

import tree_sitter_python as tspython
from tree_sitter import Language, Node

PYTHON_LANGUAGE: Final[Language] = Language(tspython.language())

NESTED_SCOPE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "function_definition",
        "class_definition",
        "decorated_definition",
    }
)


@dataclass(frozen=True, slots=True)
class PythonDefinitionEvent:
    """One Python class or function definition encountered in source pre-order."""

    definition: Node
    outer_node: Node
    symbol_name: str
    scope_stack: tuple[str, ...]
    enclosing_kind: Literal["module", "class", "function"]
    decorators: tuple[str, ...]
    body_node: Node | None
    is_async: bool


def to_utf8_bytes(source: str | bytes) -> bytes:
    """Convert Python source text or bytes into UTF-8 bytes without touching disk."""
    if isinstance(source, bytes):
        return source
    return source.encode("utf-8")


def node_text(node: Node, source_bytes: bytes) -> str:
    """Decode a tree-sitter AST node span directly from UTF-8 byte offsets."""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def node_line_span(node: Node, source_bytes: bytes) -> tuple[int, int]:
    """Return 1-based inclusive `(start_line, end_line)` for an AST node."""
    start_line = node.start_point[0] + 1
    end_row, end_col = node.end_point
    if (
        end_col == 0
        and end_row > node.start_point[0]
        and node.end_byte > node.start_byte
        and source_bytes[node.end_byte - 1] == 0x0A
    ):
        end_line = end_row
    else:
        end_line = end_row + 1
    return start_line, max(start_line, end_line)


def extract_decorator_texts(decorated_node: Node | None, source_bytes: bytes) -> tuple[str, ...]:
    """Extract decorator strings in declaration order from a `decorated_definition`."""
    if decorated_node is None or decorated_node.type != "decorated_definition":
        return ()
    return tuple(
        node_text(child, source_bytes).strip()
        for child in decorated_node.children
        if child.type == "decorator"
    )


def iter_python_definitions(
    root: Node,
    source_bytes: bytes,
) -> Iterator[PythonDefinitionEvent]:
    """Yield every named Python class/function definition iteratively without recursion."""
    stack: list[tuple[Node, tuple[str, ...], Literal["module", "class", "function"]]] = [
        (child, (), "module") for child in reversed(root.children)
    ]
    while stack:
        node, scope_stack, enclosing_kind = stack.pop()
        if node.type == "decorated_definition":
            decorators = extract_decorator_texts(node, source_bytes)
            inner = node.child_by_field_name("definition")
            if inner is None or inner.type not in {"class_definition", "function_definition"}:
                continue
            definition = inner
            outer_node = node
        elif node.type in {"class_definition", "function_definition"}:
            decorators = ()
            definition = node
            outer_node = node
        else:
            for child in reversed(node.children):
                stack.append((child, scope_stack, enclosing_kind))
            continue

        name_node = definition.child_by_field_name("name")
        if name_node is None:
            continue
        symbol_name = node_text(name_node, source_bytes)
        if not symbol_name:
            continue

        new_scope = (*scope_stack, symbol_name)
        body_node = definition.child_by_field_name("body")
        is_async = definition.type == "function_definition" and any(
            child.type == "async" for child in definition.children
        )
        yield PythonDefinitionEvent(
            definition=definition,
            outer_node=outer_node,
            symbol_name=symbol_name,
            scope_stack=new_scope,
            enclosing_kind=enclosing_kind,
            decorators=decorators,
            body_node=body_node,
            is_async=is_async,
        )
        if body_node is not None:
            next_enclosing: Literal["class", "function"] = (
                "class" if definition.type == "class_definition" else "function"
            )
            for child in reversed(body_node.children):
                stack.append((child, new_scope, next_enclosing))
