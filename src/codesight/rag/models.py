"""Zero-Code RAG domain models, deterministic UUIDv5 IDs, and NFR-3 payload guards."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from codesight.ast_engine.models import (
    MAX_SIGNATURE_CHARS,
    CodeSymbolChunk,
    sanitize_signature,
)
from codesight.ast_engine.models import (
    build_deterministic_point_id as build_deterministic_point_id,
)
from codesight.diff.file_filter import normalize_diff_path

type ChunkType = Literal["code", "architecture_doc"]
type RAGLanguage = Literal["python", "typescript", "tsx", "markdown"]

EMBEDDING_MODEL_NAME: Final[str] = "gemini-embedding-001"
EMBEDDING_DIMENSIONS: Final[int] = 768
CODE_CHUNK_PLACEHOLDER_VECTOR: Final[tuple[float, ...]] = (1.0,) + (0.0,) * (
    EMBEDDING_DIMENSIONS - 1
)
DEFAULT_EMBED_BATCH_SIZE: Final[int] = 64
MAX_EMBED_BATCH_SIZE: Final[int] = 100
DEFAULT_RPM_CONCURRENCY: Final[int] = 3
MAX_TRANSIENT_SNIPPET_CHARS: Final[int] = 2000

ALLOWED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "repo_id",
        "repo_full_name",
        "file_path",
        "blob_sha",
        "chunk_type",
        "language",
        "symbol_name",
        "symbol_kind",
        "start_line",
        "end_line",
        "signature",
        "called_symbols",
        "imports",
        "cyclomatic_complexity",
        "indexed_at",
    }
)
REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "repo_full_name",
        "file_path",
        "blob_sha",
        "chunk_type",
        "language",
        "symbol_name",
        "symbol_kind",
        "start_line",
        "end_line",
        "signature",
        "called_symbols",
        "imports",
        "cyclomatic_complexity",
    }
)
FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "code",
        "raw_code",
        "source",
        "content",
        "body",
        "text",
        "document",
        "snippet",
        "docstring",
    }
)
ALLOWED_CHUNK_TYPES: Final[frozenset[str]] = frozenset({"code", "architecture_doc"})
ALLOWED_LANGUAGES: Final[frozenset[str]] = frozenset({"python", "typescript", "tsx", "markdown"})
REQUIRED_KEYWORD_INDEX_FIELDS: Final[tuple[str, ...]] = (
    "repo_id",
    "repo_full_name",
    "file_path",
    "symbol_name",
    "called_symbols",
    "blob_sha",
    "chunk_type",
)
BODY_LEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"(\n\s+(return|await|raise|if|for|while|const|let)\b|\{\s*\n)"
)
INLINE_BODY_LEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:[:;]|=>)\s*(?:return|await|yield|if|for|while)\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r"|(?:;|=>|\{(?!\.\.\.\}))\s*(?:const|let|var)\s+"
    r"(?:[A-Za-z_$][\w$]*\s*(?:[,=:;]|\}\s*;?\s*$)|[\[{])"
    r"|(?:;|=>|\{(?!\.\.\.\}))\s*throw\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r"|\{(?!\.\.\.\})\s*(?:return|await|yield|if|for|while)\b(?!\s*(?:[,:=?]|\}(?!\s*;?\s*$)))"
    r")"
)
_PY_ONLY_INLINE_BODY_LEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"[:;]\s*(?:pass|raise)\b(?!\s*[,:=?}\]])"
)


def mask_quoted_literals(text: str) -> str:
    """Replace characters inside quoted literals (`'...'`, `\"...\"`, `` `...` ``) with spaces."""
    if "'" not in text and '"' not in text and "`" not in text:
        return text
    out: list[str] = []
    active_quote: str | None = None
    escaped = False
    for ch in text:
        if active_quote is None:
            if ch in ("'", '"', "`"):
                active_quote = ch
            out.append(ch)
            continue
        if escaped:
            escaped = False
            out.append(" ")
        elif ch == "\\":
            escaped = True
            out.append(" ")
        elif ch == active_quote:
            active_quote = None
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out)


def _has_inline_body_leak(signature: str, *, language: str) -> bool:
    masked = mask_quoted_literals(signature)
    if INLINE_BODY_LEAK_RE.search(masked) is not None:
        return True
    return (
        language not in {"typescript", "tsx"}
        and _PY_ONLY_INLINE_BODY_LEAK_RE.search(masked) is not None
    )


class ZeroCodeViolationError(ValueError):
    """Raised before network I/O when a point or payload violates NFR-3 Zero-Code Persistence."""


class EmbeddingConfigError(RuntimeError):
    """Raised fail-fast (non-retryable) on missing Gemini credentials or permanent 4xx errors."""


class QdrantSchemaMismatchError(RuntimeError):
    """Raised when an existing Qdrant collection does not match the required 768-dim Cosine spec."""


def validate_positive_int(value: int, *, name: str, maximum: int | None = None) -> int:
    """Validate that `value` is a positive int (not bool), optionally bounded by `maximum`."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}.")
    return value


def validate_uuid_point_id(point_id: str) -> str:
    """Validate that point_id is a canonical UUID string accepted by Qdrant."""
    if not isinstance(point_id, str) or not point_id.strip():
        raise ZeroCodeViolationError("Qdrant point_id must be a non-empty UUID string.")
    try:
        parsed = uuid.UUID(point_id)
    except ValueError as exc:
        raise ZeroCodeViolationError("Qdrant point_id is not a valid UUID.") from exc
    return str(parsed)


def normalize_l2_vector(
    values: Sequence[float],
    *,
    expected_dim: int = EMBEDDING_DIMENSIONS,
) -> tuple[float, ...]:
    """Validate exact vector dimension, finiteness, positive norm, and apply L2 normalization."""
    if len(values) != expected_dim:
        raise ZeroCodeViolationError(
            f"Embedding vector dimension mismatch: expected {expected_dim}, got {len(values)}."
        )
    floats: list[float] = []
    sq_sum = 0.0
    for val in values:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ZeroCodeViolationError("Embedding vector elements must be finite numbers.")
        fval = float(val)
        if not math.isfinite(fval):
            raise ZeroCodeViolationError("Embedding vector contains non-finite value (NaN/Inf).")
        floats.append(fval)
        sq_sum += fval * fval

    if sq_sum <= 0.0:
        raise ZeroCodeViolationError("Embedding vector must have positive L2 norm.")
    inv_norm = 1.0 / math.sqrt(sq_sum)
    return tuple(v * inv_norm for v in floats)


def _scan_forbidden_keys(value: Any, *, path: str = "") -> list[str]:
    """Recursively locate any forbidden source-code key regardless of letter casing."""
    hits: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key_str = str(raw_key)
            key_norm = key_str.strip().lower()
            full_path = f"{path}.{key_str}" if path else key_str
            if key_norm in FORBIDDEN_PAYLOAD_KEYS:
                hits.append(full_path)
            hits.extend(_scan_forbidden_keys(child, path=full_path))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for idx, item in enumerate(value):
            hits.extend(_scan_forbidden_keys(item, path=f"{path}[{idx}]"))
    return hits


def _require_non_empty_str(payload: Mapping[str, Any], key: str, *, label: str) -> str:
    val = payload.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ZeroCodeViolationError(f"{label}: payload field '{key}' must be a non-empty string.")
    if "\n" in val or "\r" in val:
        raise ZeroCodeViolationError(
            f"{label}: payload field '{key}' must be a single-line string."
        )
    return val


def _require_str_list(payload: Mapping[str, Any], key: str, *, label: str) -> list[str]:
    val = payload.get(key)
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise ZeroCodeViolationError(f"{label}: payload field '{key}' must be a list[str].")
    if any("\n" in item or "\r" in item for item in val):
        raise ZeroCodeViolationError(
            f"{label}: payload field '{key}' elements must be single-line strings."
        )
    return list(val)


def validate_zero_code_payload(
    payload: Mapping[str, Any],
    *,
    point_id: str | None = None,
) -> dict[str, Any]:
    """Validate and copy a Qdrant payload against NFR-3 allowlist and body-leak invariants."""
    label = f"Point {point_id}" if point_id is not None else "Payload"
    if not isinstance(payload, Mapping):
        raise ZeroCodeViolationError(f"{label}: payload must be a mapping.")

    forbidden_hits = _scan_forbidden_keys(payload)
    if forbidden_hits:
        raise ZeroCodeViolationError(
            f"{label}: NFR-3 breach — forbidden raw-code keys detected: {sorted(forbidden_hits)}."
        )

    keys = {str(k) for k in payload}
    unknown_keys = keys - ALLOWED_PAYLOAD_KEYS
    if unknown_keys:
        raise ZeroCodeViolationError(
            f"{label}: unapproved payload keys outside NFR-3 allowlist: {sorted(unknown_keys)}."
        )

    missing_keys = REQUIRED_PAYLOAD_KEYS - keys
    if missing_keys:
        raise ZeroCodeViolationError(
            f"{label}: missing required payload keys: {sorted(missing_keys)}."
        )

    for str_key in ("repo_full_name", "file_path", "blob_sha", "symbol_name", "symbol_kind"):
        _require_non_empty_str(payload, str_key, label=label)

    norm_file_path = normalize_diff_path(payload["file_path"]).rstrip("/")
    if not norm_file_path:
        raise ZeroCodeViolationError(
            f"{label}: payload field 'file_path' must normalize to a non-empty POSIX path."
        )

    for opt_str_key in ("repo_id", "indexed_at"):
        if opt_str_key in payload and payload[opt_str_key] is not None:
            _require_non_empty_str(payload, opt_str_key, label=label)

    chunk_type = payload["chunk_type"]
    if not isinstance(chunk_type, str) or chunk_type not in ALLOWED_CHUNK_TYPES:
        raise ZeroCodeViolationError(
            f"{label}: chunk_type must be one of {sorted(ALLOWED_CHUNK_TYPES)}."
        )

    language = payload["language"]
    if not isinstance(language, str) or language not in ALLOWED_LANGUAGES:
        raise ZeroCodeViolationError(
            f"{label}: language must be one of {sorted(ALLOWED_LANGUAGES)}."
        )

    start_line = payload["start_line"]
    end_line = payload["end_line"]
    complexity = payload["cyclomatic_complexity"]
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or start_line < 1
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
        or end_line < start_line
    ):
        raise ZeroCodeViolationError(
            f"{label}: start_line and end_line must be integers satisfying 1 <= start <= end."
        )
    if isinstance(complexity, bool) or not isinstance(complexity, int) or complexity < 0:
        raise ZeroCodeViolationError(
            f"{label}: cyclomatic_complexity must be a non-negative integer."
        )

    sig = payload["signature"]
    if not isinstance(sig, str) or not sig.strip():
        raise ZeroCodeViolationError(f"{label}: signature must be a non-empty string.")
    if len(sig) > MAX_SIGNATURE_CHARS:
        raise ZeroCodeViolationError(
            f"{label}: signature length {len(sig)} > {MAX_SIGNATURE_CHARS} chars "
            "(possible body leak)."
        )
    if (
        "\n" in sig
        or "\r" in sig
        or (chunk_type == "code" and _has_inline_body_leak(sig, language=language))
    ):
        raise ZeroCodeViolationError(
            f"{label}: signature must be a single-line header without code body statements."
        )

    called_symbols = _require_str_list(payload, "called_symbols", label=label)
    imports = _require_str_list(payload, "imports", label=label)

    clean: dict[str, Any] = {
        "repo_full_name": payload["repo_full_name"],
        "file_path": norm_file_path,
        "blob_sha": payload["blob_sha"],
        "chunk_type": chunk_type,
        "language": language,
        "symbol_name": payload["symbol_name"],
        "symbol_kind": payload["symbol_kind"],
        "start_line": start_line,
        "end_line": end_line,
        "signature": sig,
        "called_symbols": called_symbols,
        "imports": imports,
        "cyclomatic_complexity": complexity,
    }
    if payload.get("repo_id") is not None:
        clean["repo_id"] = payload["repo_id"]
    if payload.get("indexed_at") is not None:
        clean["indexed_at"] = payload["indexed_at"]
    return clean


@dataclass(frozen=True, slots=True)
class QdrantZeroCodePoint:
    """NFR-3 compliant point ready for Qdrant gRPC upsert (contains zero raw source code)."""

    point_id: str
    vector: tuple[float, ...]
    payload: Mapping[str, Any]


def validate_zero_code_point(point: QdrantZeroCodePoint) -> tuple[str, list[float], dict[str, Any]]:
    """Validate UUID point ID, 768-dim finite non-zero vector, and strict NFR-3 payload."""
    canonical_id = validate_uuid_point_id(point.point_id)
    normalized_vec = normalize_l2_vector(point.vector, expected_dim=EMBEDDING_DIMENSIONS)
    clean_payload = validate_zero_code_payload(point.payload, point_id=canonical_id)
    return canonical_id, list(normalized_vec), clean_payload


@dataclass(frozen=True, slots=True)
class QdrantStoredPoint:
    """Zero-code metadata point retrieved from Qdrant (without raw source code)."""

    point_id: str
    payload: dict[str, Any]
    score: float | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingRunResult:
    """Structured output and quota-saving telemetry from GeminiEmbedder.embed_items."""

    points: tuple[QdrantZeroCodePoint, ...]
    total_items: int
    deduped_in_batch: int
    skipped_existing: int
    embedded_count: int
    batch_calls: int


def _truncate_transient_snippet(
    snippet: str | None,
    *,
    max_chars: int = MAX_TRANSIENT_SNIPPET_CHARS,
) -> str | None:
    if snippet is None:
        return None
    stripped = snippet.strip()
    if not stripped:
        return None
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars]


@dataclass(slots=True)
class TransientEmbeddingItem:
    """Ephemeral RAM envelope pairing transient embedding text with an NFR-3 safe Qdrant payload.

    `clear_transient_text()` drops `_embedding_text` immediately after vector computation so raw
    code/doc snippets are never retained in memory or forwarded to `QdrantZeroCodeStore`.
    """

    point_id: str
    chunk_type: ChunkType
    payload: dict[str, Any]
    _embedding_text: str | None = field(repr=False)

    def peek_embedding_text(self) -> str | None:
        """Inspect transient RAM text (returns None once released)."""
        return self._embedding_text

    def consume_embedding_text(self) -> str:
        """Return and immediately clear the transient RAM embedding text."""
        text = self._embedding_text
        self._embedding_text = None
        if text is None or not text.strip():
            raise ValueError(
                f"Transient embedding text for point '{self.point_id}' "
                "is missing or already cleared."
            )
        return text

    def clear_transient_text(self) -> None:
        """Release transient RAM text without leaving source snippets referenced."""
        self._embedding_text = None

    @classmethod
    def from_code_chunk(
        cls,
        chunk: CodeSymbolChunk,
        *,
        repo_full_name: str,
        blob_sha: str,
        repo_id: str | None = None,
        transient_snippet: str | None = None,
        indexed_at: str | None = None,
    ) -> TransientEmbeddingItem:
        """Build a transient RAM embedding item from a Python/TypeScript AST CodeSymbolChunk."""
        if (
            "\n" in chunk.signature
            or "\r" in chunk.signature
            or _has_inline_body_leak(chunk.signature, language=chunk.language)
        ):
            raise ZeroCodeViolationError(
                "CodeSymbolChunk signature must be a single-line header without body statements."
            )
        point_id = chunk.build_point_id(repo_full_name, blob_sha)
        raw_payload = chunk.to_qdrant_payload(
            repo_full_name=repo_full_name,
            blob_sha=blob_sha,
            repo_id=repo_id,
            indexed_at=indexed_at,
        )
        clean_payload = validate_zero_code_payload(raw_payload, point_id=point_id)

        parts = [chunk.build_embedding_text()]
        bounded_snippet = _truncate_transient_snippet(transient_snippet)
        if bounded_snippet is not None:
            parts.append(f"context:\n{bounded_snippet}")

        return cls(
            point_id=point_id,
            chunk_type="code",
            payload=clean_payload,
            _embedding_text="\n".join(parts),
        )

    @classmethod
    def from_architecture_doc(
        cls,
        *,
        repo_full_name: str,
        blob_sha: str,
        file_path: str,
        section_title: str,
        start_line: int,
        end_line: int,
        transient_snippet: str,
        repo_id: str | None = None,
        indexed_at: str | None = None,
    ) -> TransientEmbeddingItem:
        """Build a transient RAM embedding item for a Markdown architecture document section."""
        if "\n" in section_title or "\r" in section_title:
            raise ZeroCodeViolationError(
                "section_title must be a single-line heading without multi-line body statements."
            )
        norm_path = normalize_diff_path(file_path).rstrip("/")
        if not norm_path:
            raise ZeroCodeViolationError("file_path must normalize to a non-empty POSIX path.")
        clean_title = sanitize_signature(section_title)
        if not clean_title:
            raise ValueError("section_title must be a non-empty string.")
        point_id = build_deterministic_point_id(
            repo_full_name,
            norm_path,
            blob_sha,
            clean_title,
            start_line,
        )
        raw_payload: dict[str, Any] = {
            "repo_full_name": repo_full_name,
            "file_path": norm_path,
            "blob_sha": blob_sha,
            "chunk_type": "architecture_doc",
            "language": "markdown",
            "symbol_name": clean_title,
            "symbol_kind": "section",
            "start_line": start_line,
            "end_line": end_line,
            "signature": clean_title,
            "called_symbols": [],
            "imports": [],
            "cyclomatic_complexity": 0,
        }
        if repo_id is not None:
            raw_payload["repo_id"] = repo_id
        if indexed_at is not None:
            raw_payload["indexed_at"] = indexed_at
        clean_payload = validate_zero_code_payload(raw_payload, point_id=point_id)

        lines = [
            f"file: {norm_path}",
            f"section: {clean_title} (architecture_doc)",
        ]
        bounded_snippet = _truncate_transient_snippet(transient_snippet)
        if bounded_snippet is not None:
            lines.append(f"excerpt:\n{bounded_snippet}")

        return cls(
            point_id=point_id,
            chunk_type="architecture_doc",
            payload=clean_payload,
            _embedding_text="\n".join(lines),
        )
