"""Shared tree-sitter-typescript/tsx utilities and iterative AST definition walker."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node

from codesight.ast_engine._ast_utils import node_text

TYPESCRIPT_LANGUAGE: Final[Language] = Language(tstypescript.language_typescript())
TSX_LANGUAGE: Final[Language] = Language(tstypescript.language_tsx())

type _EnclosingKind = Literal["module", "class", "function"]
type _ExportCtx = tuple[Node, Node] | None
type _WalkItem = tuple[Node, tuple[str, ...], _EnclosingKind, tuple[Node, ...], _ExportCtx]

_CALLABLE_EXPR_TYPES: Final[frozenset[str]] = frozenset(
    {
        "arrow_function",
        "function_expression",
        "generator_function",
    }
)
_REACT_HOC_NAMES: Final[frozenset[str]] = frozenset({"memo", "forwardRef"})
_MAX_HOC_UNWRAP_DEPTH: Final[int] = 4
_PREFIX_WRAPPER_TYPES: Final[frozenset[str]] = frozenset(
    {
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
        "instantiation_expression",
    }
)
_CALL_TARGET_WRAPPER_TYPES: Final[frozenset[str]] = _PREFIX_WRAPPER_TYPES | frozenset(
    {"await_expression"}
)

TS_DIRECT_SCOPE_BOUNDARY_TYPES: Final[frozenset[str]] = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "ambient_declaration",
    }
)


@dataclass(frozen=True, slots=True)
class TypeScriptDefinitionEvent:
    """One TypeScript/TSX symbol definition encountered in source pre-order."""

    definition: Node
    outer_node: Node
    start_node: Node
    header_start_node: Node
    symbol_name: str
    symbol_kind: Literal["function", "method", "class", "interface", "type_alias"]
    scope_stack: tuple[str, ...]
    enclosing_kind: _EnclosingKind
    decorators: tuple[str, ...]
    decorator_nodes: tuple[Node, ...]
    body_node: Node | None
    callable_node: Node | None
    wrapper_nodes: tuple[Node, ...]
    is_async: bool
    is_callable: bool


def get_typescript_grammar(language: Literal["typescript", "tsx"]) -> Language:
    """Return the pre-compiled tree-sitter Language handle for TypeScript or TSX."""
    return TSX_LANGUAGE if language == "tsx" else TYPESCRIPT_LANGUAGE


def _unwrap_expression_wrappers(node: Node, *, allow_await: bool = False) -> Node:
    """Unwrap parens and TS type assertions (`as`, `satisfies`, `<T>`, `!`, `<T>` instantiation)."""
    prefix_types = _CALL_TARGET_WRAPPER_TYPES if allow_await else _PREFIX_WRAPPER_TYPES
    current = node
    while True:
        if current.type in prefix_types:
            inner = next((c for c in current.named_children if c.type != "comment"), None)
            if inner is None:
                break
            current = inner
            continue
        if current.type == "type_assertion":
            named = [c for c in current.named_children if c.type != "comment"]
            if len(named) >= 2:
                current = named[-1]
                continue
        break
    return current


def extract_ts_callee_targets(
    target: Node | None,
    source_bytes: bytes,
    *,
    enclosing_class: str | None = None,
) -> list[tuple[int, str]]:
    """Extract `(start_byte, callee_name)` pairs (bare + receiver-qualified) from TS/TSX calls."""
    if target is None:
        return []
    current = _unwrap_expression_wrappers(target, allow_await=True)
    if current.type == "identifier":
        name = node_text(current, source_bytes)
        return [(current.start_byte, name)] if name else []
    if current.type == "member_expression":
        prop = current.child_by_field_name("property")
        if prop is None:
            return []
        prop_name = node_text(prop, source_bytes)
        if not prop_name:
            return []
        matches: list[tuple[int, str]] = [(prop.start_byte, prop_name)]
        obj = current.child_by_field_name("object")
        if obj is not None:
            unwrapped_obj = _unwrap_expression_wrappers(obj, allow_await=True)
            if unwrapped_obj.type in {"this", "super"}:
                if enclosing_class:
                    matches.append((prop.start_byte, f"{enclosing_class}.{prop_name}"))
            elif unwrapped_obj.type == "identifier":
                receiver = node_text(unwrapped_obj, source_bytes)
                if receiver in {"this", "super"}:
                    if enclosing_class:
                        matches.append((prop.start_byte, f"{enclosing_class}.{prop_name}"))
                elif receiver:
                    matches.append((prop.start_byte, f"{receiver}.{prop_name}"))
        return matches
    return []


def extract_ts_callee_target(
    target: Node | None,
    source_bytes: bytes,
) -> tuple[int, str] | None:
    """Extract primary `(start_byte, callee_name)` from a TypeScript/TSX call target."""
    matches = extract_ts_callee_targets(target, source_bytes)
    return matches[0] if matches else None


def unwrap_callable_initializer(
    value_node: Node | None,
    source_bytes: bytes,
) -> tuple[Node, tuple[Node, ...]] | None:
    """Unwrap arrow/function expressions, TS type wrappers, and React.memo/forwardRef HOCs."""
    if value_node is None:
        return None

    wrappers: list[Node] = []
    current = value_node
    for _ in range(_MAX_HOC_UNWRAP_DEPTH + 1):
        current = _unwrap_expression_wrappers(current)
        if current.type in _CALLABLE_EXPR_TYPES:
            return current, tuple(wrappers)
        if current.type == "call_expression":
            extracted = extract_ts_callee_target(
                current.child_by_field_name("function"), source_bytes
            )
            callee = extracted[1] if extracted is not None else None
            if callee in _REACT_HOC_NAMES:
                args = current.child_by_field_name("arguments")
                if args is None:
                    return None
                first_arg = next((c for c in args.named_children if c.type != "comment"), None)
                if first_arg is None:
                    return None
                wrappers.append(current)
                current = first_arg
                continue
        return None
    return None


def is_ts_scope_boundary(node: Node, source_bytes: bytes) -> bool:
    """Return True iff `node` introduces its own extracted TypeScript/TSX symbol scope."""
    if node.type in TS_DIRECT_SCOPE_BOUNDARY_TYPES:
        return True
    if node.type == "method_definition":
        return node.parent is not None and node.parent.type == "class_body"
    if node.type == "public_field_definition":
        val = node.child_by_field_name("value")
        return unwrap_callable_initializer(val, source_bytes) is not None
    if node.type == "variable_declarator":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.type == "identifier":
            val = node.child_by_field_name("value")
            return unwrap_callable_initializer(val, source_bytes) is not None
    return False


def _earliest_start_node(outer_node: Node, decs: tuple[Node, ...]) -> Node:
    """Return the node with the smallest `start_byte` among `outer_node` and `decs`."""
    earliest = outer_node
    for dec in decs:
        if dec.start_byte < earliest.start_byte:
            earliest = dec
    return earliest


def _has_async_modifier(node: Node) -> bool:
    return any(child.type == "async" for child in node.children)


def _first_non_decorator_child(node: Node) -> Node:
    for child in node.children:
        if child.type not in {"decorator", "comment"}:
            return child
    return node


def _extract_decorator_nodes(node: Node) -> tuple[Node, ...]:
    return tuple(child for child in node.children if child.type == "decorator")


def _decorator_texts(decorator_nodes: tuple[Node, ...], source_bytes: bytes) -> tuple[str, ...]:
    return tuple(node_text(dec, source_bytes).strip() for dec in decorator_nodes)


def _push_class_body_children(
    stack: list[_WalkItem],
    class_body: Node,
    scope_stack: tuple[str, ...],
) -> None:
    paired: list[tuple[Node, tuple[Node, ...]]] = []
    pending_decs: list[Node] = []
    for child in class_body.children:
        if child.type == "decorator":
            pending_decs.append(child)
            continue
        if child.type == "comment":
            continue
        if child.is_named:
            paired.append((child, tuple(pending_decs)))
            pending_decs = []
    for member, decs in reversed(paired):
        stack.append((member, scope_stack, "class", decs, None))


def _push_body_children(
    stack: list[_WalkItem],
    body_node: Node | None,
    scope_stack: tuple[str, ...],
    enclosing_kind: _EnclosingKind,
) -> None:
    if body_node is None:
        return
    for child in reversed(body_node.children):
        stack.append((child, scope_stack, enclosing_kind, (), None))


def _emit_variable_declarators(
    decl_node: Node,
    *,
    export_ctx: _ExportCtx,
    export_decs: tuple[Node, ...],
    scope_stack: tuple[str, ...],
    enclosing_kind: _EnclosingKind,
    source_bytes: bytes,
    stack: list[_WalkItem],
) -> list[TypeScriptDefinitionEvent]:
    declarators = [
        child for child in decl_node.named_children if child.type == "variable_declarator"
    ]
    single = len(declarators) == 1
    outer_export, export_token = export_ctx if export_ctx is not None else (None, None)
    events: list[TypeScriptDefinitionEvent] = []
    descend_targets: list[tuple[Node, tuple[str, ...], _EnclosingKind]] = []

    for declarator in declarators:
        name_node = declarator.child_by_field_name("name")
        value_node = declarator.child_by_field_name("value")
        unwrapped = (
            unwrap_callable_initializer(value_node, source_bytes)
            if name_node is not None and name_node.type == "identifier"
            else None
        )
        if unwrapped is None or name_node is None:
            if value_node is not None:
                descend_targets.append((value_node, scope_stack, enclosing_kind))
            continue

        symbol_name = node_text(name_node, source_bytes)
        if not symbol_name:
            continue

        callable_node, wrapper_nodes = unwrapped
        callable_body = callable_node.child_by_field_name("body")
        new_scope = (*scope_stack, symbol_name)
        outer_node = (
            outer_export
            if (single and outer_export is not None)
            else (decl_node if single else declarator)
        )
        header_start = (
            export_token
            if (single and export_token is not None)
            else (decl_node if single else declarator)
        )
        decs = export_decs if single else ()
        start_node = _earliest_start_node(outer_node, decs)
        events.append(
            TypeScriptDefinitionEvent(
                definition=declarator,
                outer_node=outer_node,
                start_node=start_node,
                header_start_node=header_start,
                symbol_name=symbol_name,
                symbol_kind="function",
                scope_stack=new_scope,
                enclosing_kind=enclosing_kind,
                decorators=_decorator_texts(decs, source_bytes),
                decorator_nodes=decs,
                body_node=callable_body,
                callable_node=callable_node,
                wrapper_nodes=wrapper_nodes,
                is_async=_has_async_modifier(callable_node),
                is_callable=True,
            )
        )
        if callable_body is not None:
            descend_targets.append((callable_body, new_scope, "function"))

    for target, target_scope, target_kind in reversed(descend_targets):
        _push_body_children(stack, target, target_scope, target_kind)
    return events


def iter_typescript_definitions(
    root: Node,
    source_bytes: bytes,
) -> Iterator[TypeScriptDefinitionEvent]:
    """Yield every TypeScript/TSX symbol definition iteratively without Python recursion."""
    stack: list[_WalkItem] = [(child, (), "module", (), None) for child in reversed(root.children)]

    while stack:
        node, scope_stack, enclosing_kind, pending_decs, export_ctx = stack.pop()

        if node.type == "export_statement":
            export_decs = (*pending_decs, *_extract_decorator_nodes(node))
            export_token = next((c for c in node.children if c.type == "export"), node)
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                stack.append((decl, scope_stack, enclosing_kind, export_decs, (node, export_token)))
                continue

            value_node = node.child_by_field_name("value")
            unwrapped = unwrap_callable_initializer(value_node, source_bytes)
            if unwrapped is not None:
                callable_node, wrapper_nodes = unwrapped
                callable_body = callable_node.child_by_field_name("body")
                callable_name = callable_node.child_by_field_name("name")
                symbol_name = (
                    node_text(callable_name, source_bytes)
                    if callable_name is not None
                    else "default"
                ) or "default"
                new_scope = (*scope_stack, symbol_name)
                yield TypeScriptDefinitionEvent(
                    definition=callable_node,
                    outer_node=node,
                    start_node=_earliest_start_node(node, export_decs),
                    header_start_node=export_token,
                    symbol_name=symbol_name,
                    symbol_kind="function",
                    scope_stack=new_scope,
                    enclosing_kind=enclosing_kind,
                    decorators=_decorator_texts(export_decs, source_bytes),
                    decorator_nodes=export_decs,
                    body_node=callable_body,
                    callable_node=callable_node,
                    wrapper_nodes=wrapper_nodes,
                    is_async=_has_async_modifier(callable_node),
                    is_callable=True,
                )
                _push_body_children(stack, callable_body, new_scope, "function")
                continue

            _push_body_children(stack, node, scope_stack, enclosing_kind)
            continue

        if node.type in {"lexical_declaration", "variable_declaration"}:
            yield from _emit_variable_declarators(
                node,
                export_ctx=export_ctx,
                export_decs=pending_decs,
                scope_stack=scope_stack,
                enclosing_kind=enclosing_kind,
                source_bytes=source_bytes,
                stack=stack,
            )
            continue

        if node.type in {"function_declaration", "generator_function_declaration"}:
            body_node = node.child_by_field_name("body")
            if body_node is None:
                continue
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbol_name = node_text(name_node, source_bytes)
            elif export_ctx is not None:
                symbol_name = "default"
            else:
                continue
            if not symbol_name:
                continue
            outer_node = export_ctx[0] if export_ctx is not None else node
            header_start = export_ctx[1] if export_ctx is not None else node
            new_scope = (*scope_stack, symbol_name)
            yield TypeScriptDefinitionEvent(
                definition=node,
                outer_node=outer_node,
                start_node=_earliest_start_node(outer_node, pending_decs),
                header_start_node=header_start,
                symbol_name=symbol_name,
                symbol_kind="function",
                scope_stack=new_scope,
                enclosing_kind=enclosing_kind,
                decorators=_decorator_texts(pending_decs, source_bytes),
                decorator_nodes=pending_decs,
                body_node=body_node,
                callable_node=node,
                wrapper_nodes=(),
                is_async=_has_async_modifier(node),
                is_callable=True,
            )
            _push_body_children(stack, body_node, new_scope, "function")
            continue

        if node.type in {"class_declaration", "abstract_class_declaration"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbol_name = node_text(name_node, source_bytes)
            elif export_ctx is not None:
                symbol_name = "default"
            else:
                continue
            if not symbol_name:
                continue
            all_decs = (*pending_decs, *_extract_decorator_nodes(node))
            body_node = node.child_by_field_name("body")
            outer_node = export_ctx[0] if export_ctx is not None else node
            header_start = (
                export_ctx[1] if export_ctx is not None else _first_non_decorator_child(node)
            )
            new_scope = (*scope_stack, symbol_name)
            yield TypeScriptDefinitionEvent(
                definition=node,
                outer_node=outer_node,
                start_node=_earliest_start_node(outer_node, all_decs),
                header_start_node=header_start,
                symbol_name=symbol_name,
                symbol_kind="class",
                scope_stack=new_scope,
                enclosing_kind=enclosing_kind,
                decorators=_decorator_texts(all_decs, source_bytes),
                decorator_nodes=all_decs,
                body_node=body_node,
                callable_node=None,
                wrapper_nodes=(),
                is_async=False,
                is_callable=False,
            )
            if body_node is not None:
                _push_class_body_children(stack, body_node, new_scope)
            continue

        if node.type == "method_definition":
            if enclosing_kind != "class":
                _push_body_children(stack, node, scope_stack, enclosing_kind)
                continue
            body_node = node.child_by_field_name("body")
            name_node = node.child_by_field_name("name")
            if body_node is None or name_node is None:
                continue
            symbol_name = node_text(name_node, source_bytes)
            if not symbol_name:
                continue
            all_decs = (*pending_decs, *_extract_decorator_nodes(node))
            new_scope = (*scope_stack, symbol_name)
            yield TypeScriptDefinitionEvent(
                definition=node,
                outer_node=node,
                start_node=_earliest_start_node(node, all_decs),
                header_start_node=_first_non_decorator_child(node),
                symbol_name=symbol_name,
                symbol_kind="method",
                scope_stack=new_scope,
                enclosing_kind="class",
                decorators=_decorator_texts(all_decs, source_bytes),
                decorator_nodes=all_decs,
                body_node=body_node,
                callable_node=node,
                wrapper_nodes=(),
                is_async=_has_async_modifier(node),
                is_callable=True,
            )
            _push_body_children(stack, body_node, new_scope, "function")
            continue

        if node.type == "public_field_definition":
            name_node = node.child_by_field_name("name")
            unwrapped = unwrap_callable_initializer(node.child_by_field_name("value"), source_bytes)
            if name_node is None or unwrapped is None:
                continue
            symbol_name = node_text(name_node, source_bytes)
            if not symbol_name:
                continue
            callable_node, wrapper_nodes = unwrapped
            callable_body = callable_node.child_by_field_name("body")
            all_decs = (*pending_decs, *_extract_decorator_nodes(node))
            new_scope = (*scope_stack, symbol_name)
            yield TypeScriptDefinitionEvent(
                definition=node,
                outer_node=node,
                start_node=_earliest_start_node(node, all_decs),
                header_start_node=_first_non_decorator_child(node),
                symbol_name=symbol_name,
                symbol_kind="method",
                scope_stack=new_scope,
                enclosing_kind="class",
                decorators=_decorator_texts(all_decs, source_bytes),
                decorator_nodes=all_decs,
                body_node=callable_body,
                callable_node=callable_node,
                wrapper_nodes=wrapper_nodes,
                is_async=_has_async_modifier(callable_node),
                is_callable=True,
            )
            _push_body_children(stack, callable_body, new_scope, "function")
            continue

        if node.type in {"interface_declaration", "type_alias_declaration"}:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            symbol_name = node_text(name_node, source_bytes)
            if not symbol_name:
                continue
            outer_node = export_ctx[0] if export_ctx is not None else node
            header_start = export_ctx[1] if export_ctx is not None else node
            is_iface = node.type == "interface_declaration"
            yield TypeScriptDefinitionEvent(
                definition=node,
                outer_node=outer_node,
                start_node=_earliest_start_node(outer_node, pending_decs),
                header_start_node=header_start,
                symbol_name=symbol_name,
                symbol_kind="interface" if is_iface else "type_alias",
                scope_stack=(*scope_stack, symbol_name),
                enclosing_kind=enclosing_kind,
                decorators=_decorator_texts(pending_decs, source_bytes),
                decorator_nodes=pending_decs,
                body_node=node.child_by_field_name("body") if is_iface else None,
                callable_node=None,
                wrapper_nodes=(),
                is_async=False,
                is_callable=False,
            )
            continue

        if node.type in {"internal_module", "module"}:
            ns_name_node = node.child_by_field_name("name")
            ns_body = node.child_by_field_name("body")
            ns_text = node_text(ns_name_node, source_bytes).strip() if ns_name_node else ""
            ns_parts = tuple(part for part in ns_text.split(".") if part)
            next_scope = (*scope_stack, *ns_parts) if ns_parts else scope_stack
            _push_body_children(stack, ns_body, next_scope, "module")
            continue

        if node.type in {"ambient_declaration", "function_signature", "abstract_method_signature"}:
            continue

        _push_body_children(stack, node, scope_stack, enclosing_kind)
