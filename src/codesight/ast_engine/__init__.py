"""Tree-sitter AST parsing, chunking, static complexity metrics, and registry."""

from codesight.ast_engine._ast_utils import DEFAULT_MAX_CHUNK_LINES
from codesight.ast_engine.complexity import (
    DEFAULT_SOURCE_GLOBS,
    DEFAULT_TEST_GLOBS,
    ComplexityAnalyzer,
    PythonComplexityAnalyzer,
    TypeScriptComplexityAnalyzer,
    compute_complexity_delta,
    detect_missing_test_companion,
    test_companion_detector,
)
from codesight.ast_engine.models import (
    MAX_SIGNATURE_CHARS,
    ASTLanguage,
    CodeSymbolChunk,
    ComplexityChangeKind,
    ComplexityDelta,
    ComplexityDiffSummary,
    FallbackReason,
    FunctionComplexity,
    HunkContextSlice,
    ParsedASTFile,
    PRFileContext,
    SymbolKind,
    TestCompanionReport,
    build_deterministic_point_id,
    sanitize_signature,
)
from codesight.ast_engine.python_parser import PythonASTParser
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.ast_engine.typescript_parser import TypeScriptASTParser

__all__ = [
    "DEFAULT_MAX_CHUNK_LINES",
    "DEFAULT_SOURCE_GLOBS",
    "DEFAULT_TEST_GLOBS",
    "MAX_SIGNATURE_CHARS",
    "ASTLanguage",
    "ASTParserRegistry",
    "CodeSymbolChunk",
    "ComplexityAnalyzer",
    "ComplexityChangeKind",
    "ComplexityDelta",
    "ComplexityDiffSummary",
    "FallbackReason",
    "FunctionComplexity",
    "HunkContextSlice",
    "PRFileContext",
    "ParsedASTFile",
    "PythonASTParser",
    "PythonComplexityAnalyzer",
    "SymbolKind",
    "TestCompanionReport",
    "TypeScriptASTParser",
    "TypeScriptComplexityAnalyzer",
    "build_deterministic_point_id",
    "compute_complexity_delta",
    "detect_missing_test_companion",
    "sanitize_signature",
    "test_companion_detector",
]
