"""Unit tests for PythonASTParser and CodeSymbolChunk NFR-3 contracts."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest

from codesight.ast_engine.models import MAX_SIGNATURE_CHARS, CodeSymbolChunk, ParsedASTFile
from codesight.ast_engine.python_parser import PythonASTParser
from codesight.rag.models import TransientEmbeddingItem


def _load_audit_payloads() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = (
        repo_root
        / ".agents"
        / "skills"
        / "zerocode-ast-rag"
        / "scripts"
        / "audit_qdrant_zerocode.py"
    )
    spec = importlib.util.spec_from_file_location("audit_qdrant_zerocode", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit_payloads


AUDIT_PAYLOADS = _load_audit_payloads()

REALISTIC_MODULE = '''
"""Module docstring should not crash extraction."""
import os, sys as _sys
from typing import Any, Final
from .errors import RetryableTaskError as RTE
from ..core import *

DEFAULT_TIMEOUT: Final[int] = 10


def top_level_helper(raw: str) -> str:
    """Normalize token input."""
    cleaned = raw.strip()
    return helper_call(cleaned)


@dataclass(frozen=True)
class OrderProcessor(BaseProcessor):
    """Process incoming orders cleanly."""

    items: list[str] = field(default_factory=list)

    def validate(self, order_id: str) -> bool:
        """Validate order format."""
        return check_prefix(order_id)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "OrderProcessor":
        return cls()

    @staticmethod
    def health_check() -> str:
        return ping_service()

    @property
    def item_count(self) -> int:
        return len(self.items)

    @retry(
        attempts=3,
        backoff=2,
    )
    async def process_payment(
        self,
        user_id: str,
        amount: float,
    ) -> dict[str, Any]:
        """Execute payment workflow.

        Handles normalization via nested helper.
        """
        from decimal import Decimal

        def _normalize_amount(val: float) -> Decimal:
            rounded = round(val, 2)
            return Decimal(str(rounded))

        normalized = _normalize_amount(amount)
        saved = await self.repo.save(user_id, normalized)
        res = self.http_client.post("/pay").raise_for_status()
        return {"saved": saved, "status": res}
'''


def test_python_ast_parser_extracts_classes_methods_async_nested_decorators_and_imports() -> None:
    parser = PythonASTParser()
    parsed = parser.parse(REALISTIC_MODULE, file_path="./src/orders/processor.py")

    assert isinstance(parsed, ParsedASTFile)
    assert parsed.file_path == "src/orders/processor.py"
    assert parsed.language == "python"
    assert parsed.has_syntax_error is False
    assert parsed.imports == (
        "os",
        "sys",
        "typing.Any",
        "typing.Final",
        ".errors.RetryableTaskError",
        "..core.*",
    )

    by_qname = {chunk.qualified_name: chunk for chunk in parsed.chunks}
    assert list(by_qname.keys()) == [
        "top_level_helper",
        "OrderProcessor",
        "OrderProcessor.validate",
        "OrderProcessor.from_env",
        "OrderProcessor.health_check",
        "OrderProcessor.item_count",
        "OrderProcessor.process_payment",
        "OrderProcessor.process_payment._normalize_amount",
    ]

    helper = by_qname["top_level_helper"]
    assert helper.symbol_name == "top_level_helper"
    assert helper.symbol_kind == "function"
    assert helper.is_async is False
    assert helper.signature == "def top_level_helper(raw: str) -> str"
    assert helper.docstring == "Normalize token input."
    assert helper.called_symbols == ("strip", "helper_call")
    assert helper.start_line == 11
    assert helper.end_line == 14

    cls_chunk = by_qname["OrderProcessor"]
    assert cls_chunk.symbol_name == "OrderProcessor"
    assert cls_chunk.symbol_kind == "class"
    assert cls_chunk.decorators == ("@dataclass(frozen=True)",)
    assert cls_chunk.signature == "class OrderProcessor(BaseProcessor)"
    assert cls_chunk.docstring == "Process incoming orders cleanly."
    # Scope isolation: class chunk captures class decorator & body calls ONLY, not method calls.
    assert cls_chunk.called_symbols == ("dataclass", "field")

    process_chunk = by_qname["OrderProcessor.process_payment"]
    assert process_chunk.symbol_name == "process_payment"
    assert process_chunk.symbol_kind == "method"
    assert process_chunk.is_async is True
    assert len(process_chunk.decorators) == 1
    assert process_chunk.decorators[0].startswith("@retry(")
    assert process_chunk.signature == (
        "async def process_payment(self, user_id: str, amount: float) -> dict[str, Any]"
    )
    assert process_chunk.docstring == (
        "Execute payment workflow.\n\nHandles normalization via nested helper."
    )
    # Local import inside process_payment merges with module imports cleanly.
    assert "decimal.Decimal" in process_chunk.imports
    # Scope isolation: includes decorator call `retry`, excludes nested fn calls.
    assert process_chunk.called_symbols == (
        "retry",
        "_normalize_amount",
        "save",
        "post",
        "raise_for_status",
    )
    # Decorator span is included in start_line..end_line for GitHubBlobFetcher.
    assert process_chunk.start_line == 39
    assert process_chunk.end_line == 61

    nested_chunk = by_qname["OrderProcessor.process_payment._normalize_amount"]
    assert nested_chunk.symbol_name == "_normalize_amount"
    assert nested_chunk.symbol_kind == "function"
    assert nested_chunk.called_symbols == ("round", "Decimal", "str")
    # Nested function inherits enclosing function's local imports.
    assert "decimal.Decimal" in nested_chunk.imports


def test_python_ast_parser_handles_multibyte_utf8_vietnamese_offsets_safely() -> None:
    source = (
        "# Tiêu đề tiếng Việt có dấu 🚀✨ — kiểm tra offset UTF-8\n"
        'LABEL = "Giá trị khởi tạo với emoji 🔐"\n\n'
        "def kiem_tra_chu_ky(du_lieu: str) -> bool:\n"
        '    """Xác thực chữ ký bảo mật tiếng Việt."""\n'
        "    return xac_minh_hmac(du_lieu.encode('utf-8'))\n"
    )
    parser = PythonASTParser()
    parsed = parser.parse(source.encode("utf-8"), file_path="src/security/hmac_check.py")

    assert parsed.has_syntax_error is False
    assert len(parsed.chunks) == 1
    chunk = parsed.chunks[0]
    assert chunk.symbol_name == "kiem_tra_chu_ky"
    assert chunk.signature == "def kiem_tra_chu_ky(du_lieu: str) -> bool"
    assert chunk.docstring == "Xác thực chữ ký bảo mật tiếng Việt."
    assert chunk.called_symbols == ("xac_minh_hmac", "encode")
    assert chunk.start_line == 4
    assert chunk.end_line == 6


def test_python_ast_parser_splits_oversized_functions_and_keeps_classes_unsplit() -> None:
    # Case A: function > 150 lines of sequential statements
    seq_lines = ["def long_sequential(items: list[int]) -> int:", '    """Sum many items."""']
    for idx in range(185):
        seq_lines.append(f"    val_{idx} = compute_{idx}(items)")
    seq_lines.append("    return val_184")
    seq_source = "\n".join(seq_lines) + "\n"

    # Case B: function with a single giant compound `if` block > 180 lines
    compound_lines = [
        "def long_compound(flag: bool, items: list[int]) -> int:",
        "    if flag:",
    ]
    for idx in range(180):
        compound_lines.append(f"        step_{idx} = run_step_{idx}(items)")
    compound_lines.append("    return 1")
    compound_source = "\n".join(compound_lines) + "\n"

    # Case C: function containing a single >160-line literal statement (window fallback)
    literal_lines = ["def long_literal() -> list[int]:", "    big = ["]
    for idx in range(165):
        literal_lines.append(f"        {idx},")
    literal_lines.append("    ]")
    literal_lines.append("    return big")
    literal_source = "\n".join(literal_lines) + "\n"

    # Case D: class spanning > 180 lines across short methods -> class chunk must NOT split
    class_lines = ["class HugeService:", '    """Service with many small methods."""']
    for idx in range(65):
        class_lines.extend(
            [
                f"    def method_{idx}(self) -> int:",
                f"        return {idx}",
                "",
            ]
        )
    class_source = "\n".join(class_lines)

    parser = PythonASTParser(max_chunk_lines=150)

    seq_parsed = parser.parse(seq_source, file_path="src/seq.py")
    assert len(seq_parsed.chunks) >= 2
    assert all(c.symbol_name == "long_sequential" for c in seq_parsed.chunks)
    assert all(
        c.signature == "def long_sequential(items: list[int]) -> int" for c in seq_parsed.chunks
    )
    assert all(c.is_subchunk is True for c in seq_parsed.chunks)
    assert [c.subchunk_index for c in seq_parsed.chunks] == list(range(len(seq_parsed.chunks)))
    assert all(c.line_count <= 150 for c in seq_parsed.chunks)
    assert seq_parsed.chunks[0].docstring == "Sum many items."
    assert all(c.docstring is None for c in seq_parsed.chunks[1:])
    seq_point_ids = [c.build_point_id("org/repo", "deadbeef") for c in seq_parsed.chunks]
    assert len(set(seq_point_ids)) == len(seq_point_ids)

    compound_parsed = parser.parse(compound_source, file_path="src/compound.py")
    assert len(compound_parsed.chunks) >= 2
    assert all(c.is_subchunk is True and c.line_count <= 150 for c in compound_parsed.chunks)
    assert all(
        c.signature == "def long_compound(flag: bool, items: list[int]) -> int"
        for c in compound_parsed.chunks
    )

    literal_parsed = parser.parse(literal_source, file_path="src/literal.py")
    assert len(literal_parsed.chunks) >= 2
    assert all(c.is_subchunk is True and c.line_count <= 150 for c in literal_parsed.chunks)
    lit_ids = [c.build_point_id("org/repo", "cafebabe") for c in literal_parsed.chunks]
    assert len(set(lit_ids)) == len(lit_ids)

    class_parsed = parser.parse(class_source, file_path="src/huge_service.py")
    class_chunks = [c for c in class_parsed.chunks if c.symbol_kind == "class"]
    assert len(class_chunks) == 1
    assert class_chunks[0].is_subchunk is False
    assert class_chunks[0].line_count > 150


def test_qdrant_payload_nfr3_compliance_signature_truncation_and_uuidv5() -> None:
    long_params = ", ".join(f"param_{idx}: int" for idx in range(40))
    source = (
        f"def mega_signature({long_params}) -> bool:\n"
        '    """Sensitive docstring must stay in RAM only."""\n'
        "    return verify_all()\n"
    )
    parser = PythonASTParser()
    parsed = parser.parse(source, file_path="src/auth/mega.py")
    assert len(parsed.chunks) == 1
    chunk = parsed.chunks[0]

    assert len(chunk.signature) <= MAX_SIGNATURE_CHARS
    assert chunk.signature.endswith("...")
    assert "\n" not in chunk.signature

    point_id = chunk.build_point_id("Tieens-04/CodeSight-AI", "abc1234")
    expected_uuid = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"Tieens-04/CodeSight-AI:src/auth/mega.py:abc1234:mega_signature:{chunk.start_line}",
        )
    )
    assert point_id == expected_uuid

    embed_text = chunk.build_embedding_text()
    assert "src/auth/mega.py" in embed_text
    assert "mega_signature" in embed_text
    assert "Sensitive docstring must stay in RAM only." in embed_text
    assert "verify_all" in embed_text

    payload = chunk.to_qdrant_payload(
        repo_full_name="Tieens-04/CodeSight-AI",
        blob_sha="abc1234",
        repo_id="repo-uuid-1",
        indexed_at="2026-11-23T00:00:00Z",
    )
    assert "docstring" not in payload
    assert "decorators" not in payload
    assert "qualified_name" not in payload
    assert AUDIT_PAYLOADS([{"id": point_id, "payload": payload}]) == []


def test_python_ast_parser_resilience_on_empty_syntax_error_and_validation() -> None:
    with pytest.raises(ValueError, match="max_chunk_lines"):
        PythonASTParser(max_chunk_lines=5)
    with pytest.raises(ValueError, match="max_chunk_lines"):
        PythonASTParser(max_chunk_lines=True)

    parser = PythonASTParser()
    empty_parsed = parser.parse("   \n\n", file_path="")
    assert empty_parsed.file_path == "<memory>"
    assert empty_parsed.chunks == ()
    assert empty_parsed.has_syntax_error is False

    broken_source = (
        "def valid_fn(x: int) -> int:\n    return x + 1\n\ndef broken_fn(a, :\n    if a\n"
    )
    broken_parsed = parser.parse(broken_source, file_path="src/broken.py")
    assert broken_parsed.has_syntax_error is True
    assert any(c.symbol_name == "valid_fn" for c in broken_parsed.chunks)

    # Ensure CodeSymbolChunk direct constructor helper properties stay valid
    manual = CodeSymbolChunk(
        file_path="./src/a.py",
        language="python",
        symbol_name="fn",
        qualified_name="fn",
        symbol_kind="function",
        start_line=10,
        end_line=15,
        signature="def fn():",
    )
    assert manual.line_count == 6
    minimal_payload = manual.to_qdrant_payload(repo_full_name="org/repo", blob_sha="sha0")
    assert "repo_id" not in minimal_payload
    assert "indexed_at" not in minimal_payload
    assert (
        AUDIT_PAYLOADS(
            [{"id": manual.build_point_id("org/repo", "sha0"), "payload": minimal_payload}]
        )
        == []
    )


def test_python_ast_parser_additional_edge_cases_and_models() -> None:
    from codesight.ast_engine.models import sanitize_signature

    assert sanitize_signature("def x():", max_chars=0) == ""
    assert sanitize_signature("def x():", max_chars=3) == "def"

    chunk_with_decorators = CodeSymbolChunk(
        file_path="src/dec.py",
        language="python",
        symbol_name="run",
        qualified_name="run",
        symbol_kind="function",
        start_line=1,
        end_line=4,
        signature="def run()",
        decorators=("@traced",),
        imports=("os",),
        called_symbols=(),
        docstring=None,
    )
    emb = chunk_with_decorators.build_embedding_text()
    assert "decorators: @traced" in emb
    assert "imports: os" in emb
    assert "docstring:" not in emb
    assert "calls:" not in emb

    source = '''
from . import local_mod
from . import *
if TYPE_CHECKING:
    import typing

def dynamic_call_and_fstring_doc(items: list[str]) -> str:
    # comment before docstring
    f"""F-string doc for {items}."""
    get_handler()("payload")
    return "ok"

def no_docstring_numeric_first() -> int:
    42
    return 1

def oversized_with_comments_and_nested_else(flag: bool) -> int:
    """Doc."""
    # top comment
''' + "\n".join(
        [
            "    if flag:",
            *[f"        a_{i} = step_{i}()" for i in range(160)],
            "    else:",
            *[f"        b_{i} = alt_{i}()" for i in range(25)],
            "    return 0",
        ]
    )

    parser = PythonASTParser(max_chunk_lines=150)
    parsed = parser.parse(source, file_path="src/edge.py")
    assert parsed.imports == (".local_mod", ".*", "typing")

    by_name = [
        c for c in parsed.chunks if c.symbol_name == "oversized_with_comments_and_nested_else"
    ]
    assert len(by_name) >= 2
    assert all(c.is_subchunk and c.line_count <= 150 for c in by_name)

    dyn = next(c for c in parsed.chunks if c.symbol_name == "dynamic_call_and_fstring_doc")
    assert dyn.docstring is None
    assert dyn.called_symbols == ("get_handler",)

    num = next(c for c in parsed.chunks if c.symbol_name == "no_docstring_numeric_first")
    assert num.docstring is None

    # Line-accurate called_symbols inside window-split >160-line list literal
    window_lines = ["def long_literal_with_late_call() -> list[int]:", "    big = ["]
    for idx in range(158):
        window_lines.append(f"        {idx},")
    window_lines.extend(["        compute_late(),", "    ]", "    return big"])
    window_parsed = parser.parse("\n".join(window_lines), file_path="src/win_calls.py")
    assert len(window_parsed.chunks) == 2
    assert "compute_late" not in window_parsed.chunks[0].called_symbols
    assert "compute_late" in window_parsed.chunks[1].called_symbols


def test_signature_strips_trailing_and_inline_header_comments_nfr3() -> None:
    source = (
        "def login(user: str) -> bool:  # TODO: rotate AWS key AKIA123\n"
        "    return bool(user)\n\n"
        "class AuthClient(Base):  # internal: uses prod db password\n"
        "    pass\n\n"
        "def connect(\n"
        "    host: str,  # secret=hunter2\n"
        '    tag: str = "#keep_me",  # another secret\n'
        ") -> bool:  # trailing secret\n"
        "    return True\n\n"
        "class Child(\n"
        "    Base,  # base secret\n"
        "):  # after colon\n"
        "    pass\n"
    )
    parsed = PythonASTParser().parse(source, file_path="src/auth/client.py")
    by_name = {c.symbol_name: c for c in parsed.chunks}

    assert by_name["login"].signature == "def login(user: str) -> bool"
    assert by_name["AuthClient"].signature == "class AuthClient(Base)"
    assert by_name["connect"].signature == "def connect(host: str, tag: str) -> bool"
    assert by_name["Child"].signature == "class Child( Base, )"

    for name in ("login", "AuthClient", "Child"):
        payload = by_name[name].to_qdrant_payload(repo_full_name="org/repo", blob_sha="sha1")
        assert "#" not in payload["signature"]
        assert "AKIA123" not in payload["signature"]
        assert "password" not in payload["signature"]
        assert "secret" not in payload["signature"]
        assert (
            AUDIT_PAYLOADS(
                [{"id": by_name[name].build_point_id("org/repo", "sha1"), "payload": payload}]
            )
            == []
        )

    connect_payload = by_name["connect"].to_qdrant_payload(
        repo_full_name="org/repo", blob_sha="sha1"
    )
    assert "hunter2" not in connect_payload["signature"]
    assert "trailing" not in connect_payload["signature"]
    assert "#keep_me" not in connect_payload["signature"]
    assert (
        AUDIT_PAYLOADS(
            [
                {
                    "id": by_name["connect"].build_point_id("org/repo", "sha1"),
                    "payload": connect_payload,
                }
            ]
        )
        == []
    )


def _assert_contiguous_chunks(chunks: tuple[CodeSymbolChunk, ...], total_lines: int) -> None:
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == total_lines
    for idx, chunk in enumerate(chunks):
        assert 1 <= chunk.line_count <= 150
        if idx > 0:
            assert chunk.start_line == chunks[idx - 1].end_line + 1


def test_oversized_split_preserves_compound_header_calls_contiguous_lines_and_exact_cc() -> None:
    parser = PythonASTParser(max_chunk_lines=150)

    # Case 1: if-body ends at line 150, elif header sits exactly at line 151
    b1 = "\n".join(f"        x_{i} = {i}" for i in range(148))
    b2 = "\n".join(f"        y_{i} = {i}" for i in range(150))
    branch_src = (
        f"def big_branch(a):\n    if check_a(a):\n{b1}\n    elif check_b(a):\n{b2}\n    return a\n"
    )
    branch_parsed = parser.parse(branch_src, file_path="src/big_branch.py")
    _assert_contiguous_chunks(branch_parsed.chunks, branch_src.count("\n"))
    branch_calls = set().union(*(c.called_symbols for c in branch_parsed.chunks))
    assert {"check_a", "check_b"} <= branch_calls
    branch_total_cc = sum(c.cyclomatic_complexity - 1 for c in branch_parsed.chunks) + 1
    assert branch_total_cc == 3

    # Case 2: with + for + if wrapping > 150 lines
    loop_body = "\n".join(f"                z_{i} = {i}" for i in range(160))
    loop_src = (
        "def big_loop(client):\n"
        "    with open_session() as s:\n"
        "        for item in client.list_items():\n"
        f"            if validate(item):\n{loop_body}\n"
        "    return s\n"
    )
    loop_parsed = parser.parse(loop_src, file_path="src/big_loop.py")
    _assert_contiguous_chunks(loop_parsed.chunks, loop_src.count("\n"))
    loop_calls = set().union(*(c.called_symbols for c in loop_parsed.chunks))
    assert {"open_session", "list_items", "validate"} <= loop_calls
    loop_total_cc = sum(c.cyclomatic_complexity - 1 for c in loop_parsed.chunks) + 1
    assert loop_total_cc == 3

    # Case 3: giant 320-line literal with 320 ternaries -> no 1-line chunk, no CC inflation
    ternaries = "\n".join(f"        {i} if x else 0," for i in range(320))
    lit_src = f"def big_literal(x):\n    return [\n{ternaries}\n    ]\n"
    lit_parsed = parser.parse(lit_src, file_path="src/big_lit.py")
    _assert_contiguous_chunks(lit_parsed.chunks, lit_src.count("\n"))
    assert len(lit_parsed.chunks) == 3
    assert all(c.line_count >= 10 for c in lit_parsed.chunks)
    lit_total_cc = sum(c.cyclomatic_complexity - 1 for c in lit_parsed.chunks) + 1
    assert lit_total_cc == 321


def test_called_symbols_includes_decorator_and_default_parameter_calls() -> None:
    source = (
        "class Controller:\n"
        '    @app.get(build_path("/x"))\n'
        "    async def handle(\n"
        "        self,\n"
        "        dep = Depends(get_session),\n"
        "        fallback = default_factory(),\n"
        "    ) -> int:\n"
        "        return run_logic(dep, fallback)\n"
    )
    parsed = PythonASTParser().parse(source, file_path="src/api/controller.py")
    by_qname = {c.qualified_name: c for c in parsed.chunks}
    assert by_qname["Controller"].called_symbols == ()
    assert by_qname["Controller.handle"].called_symbols == (
        "get",
        "build_path",
        "Depends",
        "get_session",
        "default_factory",
        "run_logic",
    )


def test_python_ast_parser_extracts_fastapi_depends_and_security_targets() -> None:
    source = (
        "from typing import Annotated\n"
        "import fastapi\n"
        "from fastapi import Depends, Security\n\n"
        "@router.post('/orders')\n"
        "async def create_order(\n"
        "    user = Depends(verify_bearer_principal),\n"
        "    tenant = fastapi.Depends(auth_svc.resolve_tenant),\n"
        "    admin: Annotated[str, Security(require_admin_scope, scopes=['orders:write'])],\n"
        "    kw_user = Depends(dependency=load_actor_context),\n"
        "    kw_attr = Security(scopes=['read'], dependency=auth_svc.check_policy),\n"
        "    auto_dep: OrderService = Depends(),\n"
        "    anon_dep = Depends(lambda: None),\n"
        ") -> dict[str, str]:\n"
        "    return submit_order(user, tenant, admin, kw_user, kw_attr, auto_dep, anon_dep)\n"
    )
    parsed = PythonASTParser().parse(source, file_path="src/api/orders_router.py")
    assert len(parsed.chunks) == 1
    calls = parsed.chunks[0].called_symbols
    assert "Depends" in calls
    assert "Security" in calls
    assert "verify_bearer_principal" in calls
    assert "resolve_tenant" in calls
    assert "require_admin_scope" in calls
    assert "load_actor_context" in calls
    assert "check_policy" in calls
    assert "submit_order" in calls
    # Order-preserving relative to declaration position
    assert calls.index("Depends") < calls.index("verify_bearer_principal")
    assert calls.index("Security") < calls.index("require_admin_scope")
    assert calls.index("check_policy") < calls.index("submit_order")


def test_deeply_nested_definitions_do_not_raise_recursion_error() -> None:
    deep = "".join("    " * i + f"def f{i}():\n" for i in range(600)) + "    " * 600 + "pass\n"
    parsed = PythonASTParser().parse(deep, file_path="src/deep.py")
    assert isinstance(parsed, ParsedASTFile)
    assert len(parsed.chunks) > 0


def test_docstring_warnings_future_imports_crlf_and_nested_scopes() -> None:
    import warnings

    source = (
        "from __future__ import annotations\r\n"
        "\r\n"
        "def regex_doc() -> str:\r\n"
        '    """Match \\d+ digits."""\r\n'
        "    return 'ok'\r\n"
        "\r\n"
        "def concat_doc() -> str:\r\n"
        '    "part1 " "part2"\r\n'
        "    return 'ok'\r\n"
        "\r\n"
        "def bytes_not_doc() -> str:\r\n"
        '    b"""bytes"""\r\n'
        "    return 'ok'\r\n"
        "\r\n"
        "def outer_factory() -> type:\r\n"
        "    class InnerModel:\r\n"
        "        if True:\r\n"
        "            def build(self) -> int:\r\n"
        "                return 1\r\n"
        "    return InnerModel\r\n"
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parsed = PythonASTParser().parse(source, file_path="src/crlf.py")

    syntax_warnings = [w for w in caught if issubclass(w.category, SyntaxWarning)]
    assert syntax_warnings == []
    assert parsed.imports == ("__future__.annotations",)

    by_qname = {c.qualified_name: c for c in parsed.chunks}
    assert by_qname["regex_doc"].docstring == r"Match \d+ digits."
    assert by_qname["regex_doc"].start_line == 3
    assert by_qname["regex_doc"].end_line == 5
    assert by_qname["concat_doc"].docstring == "part1 part2"
    assert by_qname["bytes_not_doc"].docstring is None
    assert by_qname["outer_factory.InnerModel"].symbol_kind == "class"
    assert by_qname["outer_factory.InnerModel.build"].symbol_kind == "method"


def test_python_signature_strips_default_values_and_credentials() -> None:
    src = (
        "def connect(\n"
        "    dsn: str = 'postgresql://admin:S3cr3tP4ss@db/app',\n"
        "    api_key = 'sk-live-secret',\n"
        "    help_msg: str = typer.Option('x', help='Search: if empty, raise error'),\n"
        "    *args: int,\n"
        "    retries: int = 3,\n"
        ") -> bool:\n"
        "    return True\n"
    )
    chunks = PythonASTParser().parse(src, file_path="src/db.py").chunks
    assert len(chunks) == 1
    sig = chunks[0].signature
    assert sig == "def connect(dsn: str, api_key, help_msg: str, *args: int, retries: int) -> bool"
    assert "S3cr3tP4ss" not in sig
    assert "sk-live-secret" not in sig
    item = TransientEmbeddingItem.from_code_chunk(
        chunks[0], repo_full_name="acme/app", blob_sha="a1" * 20
    )
    assert "S3cr3tP4ss" not in str(item.payload)
