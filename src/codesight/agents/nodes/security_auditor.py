"""Security Auditor Agent node with coordinate grounding, secret masking, and indent guards."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes.context_builder import (
    ANNOTATED_FILE_HEADER_RE,
    normalize_agent_path,
)
from codesight.agents.prompts.security_prompt import SECURITY_AUDITOR_SYSTEM_PROMPT
from codesight.agents.schemas import (
    BuiltBatchContext,
    CodeSuggestion,
    DiffBatch,
    FindingCandidate,
    LLMAuditorDraftOutput,
    LLMCallMetrics,
    LLMCodeSuggestionDraft,
    LLMFindingDraft,
)
from codesight.diff.hunk_mapper import parse_hunk_header, split_diff_lines

_REDACTED: Final[str] = "***REDACTED***"

_RIGHT_LINE_CAPTURE_RE: Final[re.Pattern[str]] = re.compile(r"^L(\d+) \| (?:\+ |  )(.*)$")
_FENCE_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$")
_FENCE_CLOSE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_REF_CONTEXT_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<reference_context\b([^>]*)>\n(.*?)\n</reference_context>",
    re.DOTALL,
)
_REF_TYPE_ATTR_RE: Final[re.Pattern[str]] = re.compile(r'\btype="([^"]*)"')
_REF_SOURCE_ATTR_RE: Final[re.Pattern[str]] = re.compile(r'\bsource="([^"]*)"')
_REF_SOURCE_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"^(.*):L(\d+)-L(\d+)$")
_REF_SYMBOL_NAME_RE: Final[re.Pattern[str]] = re.compile(r'\bsymbol="([^"]*)"')
_REF_CALLS_ATTR_RE: Final[re.Pattern[str]] = re.compile(r'\bcalls="([^"]*)"')
_REF_LINE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^L\d+:\s*")
_NON_SECRET_KEY_PREFIX_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "err",
        "error",
        "invalid",
        "missing",
        "empty",
        "msg",
        "message",
        "label",
        "hint",
        "prompt",
        "placeholder",
    }
)

_SECRET_ASSIGN_CAPTURE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![a-z0-9_-])((['\"]?)([a-z0-9_-]*?"
    r"(?:api[-_]?key|api[-_]?token|auth[-_]?token|access[-_]?token|refresh[-_]?token|"
    r"secret[-_]?key|client[-_]?secret|private[-_]?key|bearer[-_]?token|signing[-_]?secret|"
    r"passphrase|password|passwd|secret|token)(?:[-_]?val(?:ue)?|\d*)?)\2\s*[:=]\s*)"
    r"(['\"])([^'\"\n]{8,})\4"
)
_SECRET_VALUE_REGEXES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*KEY-----|$)"),
        _REDACTED,
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), _REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), _REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b"), _REDACTED),
    (re.compile(r"\b(?:sk|rk)-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _REDACTED),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), _REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), _REDACTED),
    (re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), rf"\1{_REDACTED}"),
)
_NON_GROUNDING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "def",
        "return",
        "const",
        "let",
        "var",
        "function",
        "import",
        "from",
        "class",
        "async",
        "await",
        "self",
        "none",
        "true",
        "false",
        "null",
        "undefined",
        "string",
        "str",
        "int",
        "bool",
        "float",
        "export",
        "default",
        "pass",
        "else",
        "elif",
        "try",
        "except",
        "finally",
        "raise",
        "assert",
        "yield",
        "object",
        "dict",
        "list",
        "tuple",
        "set",
        "any",
        "void",
        "number",
        "boolean",
        "type",
        "interface",
        "public",
        "private",
        "protected",
        "static",
        "new",
        "this",
        "only",
        "not",
        "the",
        "and",
        "for",
        "with",
    }
)

logger = structlog.get_logger("codesight.agents.nodes.security_auditor")


@dataclass(frozen=True, slots=True)
class BatchDiffIndex:
    """In-memory RIGHT-side hunk coordinate and line content index for one DiffBatch."""

    line_hunks_by_file: dict[str, dict[int, int]]
    line_contents_by_file: dict[str, dict[int, str]]


def _is_strict_pos_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def parse_batch_diff_index(batch: DiffBatch) -> BatchDiffIndex:
    """Build RIGHT-side `(file_path -> {line: hunk_id})` and line content lookup for `batch`."""
    parsed_hunks: dict[str, dict[int, int]] = {}
    line_contents: dict[str, dict[int, str]] = {}

    current_file: str | None = None
    current_hunk_id = 0
    for raw_line in split_diff_lines(batch.annotated_diff):
        file_match = ANNOTATED_FILE_HEADER_RE.match(raw_line)
        if file_match is not None:
            norm = normalize_agent_path(file_match.group(1))
            current_file = norm or None
            current_hunk_id = 0
            if current_file is not None:
                parsed_hunks.setdefault(current_file, {})
                line_contents.setdefault(current_file, {})
            continue
        if current_file is None:
            continue
        if parse_hunk_header(raw_line) is not None:
            current_hunk_id += 1
            continue
        line_match = _RIGHT_LINE_CAPTURE_RE.match(raw_line)
        if line_match is not None:
            line_no = int(line_match.group(1))
            content = line_match.group(2)
            parsed_hunks[current_file][line_no] = max(1, current_hunk_id)
            line_contents[current_file][line_no] = content

    if batch.right_line_hunks_by_file:
        line_hunks: dict[str, dict[int, int]] = {}
        for raw_fp, hmap in batch.right_line_hunks_by_file.items():
            norm = normalize_agent_path(raw_fp)
            if norm:
                line_hunks[norm] = {int(ln): int(hid) for ln, hid in hmap.items()}
    else:
        line_hunks = parsed_hunks

    return BatchDiffIndex(line_hunks_by_file=line_hunks, line_contents_by_file=line_contents)


def _is_credential_key(raw_key: str) -> bool:
    parts = [part for part in re.split(r"[-_]+", raw_key.lower()) if part]
    if len(parts) >= 2 and parts[0] in _NON_SECRET_KEY_PREFIX_TOKENS:
        return False
    return True


def _looks_like_secret_literal(value: str) -> bool:
    if len(value) < 8 or _REDACTED in value or "\n" in value or "\r" in value:
        return False
    stripped = value.strip()
    if not stripped or stripped.startswith(("<", "${", "{{")):
        return False
    return True


def _redact_secret_assignment(match: re.Match[str]) -> str:
    if not (_is_credential_key(match.group(3)) and _looks_like_secret_literal(match.group(5))):
        return match.group(0)
    return f"{match.group(1)}{match.group(4)}{_REDACTED}{match.group(4)}"


def _extract_diff_secret_literals(lines: Sequence[str]) -> tuple[str, ...]:
    literals: list[str] = []
    for line in lines:
        for match in _SECRET_ASSIGN_CAPTURE_RE.finditer(line):
            secret_val = match.group(5)
            if _is_credential_key(match.group(3)) and _looks_like_secret_literal(secret_val):
                literals.append(secret_val)
    return tuple(dict.fromkeys(literals))


def mask_sensitive_values(text: str, *, extra_literals: Sequence[str] = ()) -> str:
    """Mask secret literals, keys, and tokens deterministically with `***REDACTED***`."""
    masked = text
    for literal in sorted(
        {lit for lit in extra_literals if len(lit) >= 8 and lit != _REDACTED},
        key=len,
        reverse=True,
    ):
        masked = masked.replace(literal, _REDACTED)
    for pattern, replacement in _SECRET_VALUE_REGEXES:
        masked = pattern.sub(replacement, masked)
    return _SECRET_ASSIGN_CAPTURE_RE.sub(_redact_secret_assignment, masked)


@dataclass(frozen=True, slots=True)
class _ReferenceCodeBlock:
    tier: str
    source_file: str
    start_line: int | None
    end_line: int | None
    symbol_name: str
    calls: tuple[str, ...]
    code_text: str


def _parse_reference_code_blocks(rendered_prompt: str) -> tuple[_ReferenceCodeBlock, ...]:
    """Parse `<reference_context>` blocks (excluding architecture_doc prose and headings)."""
    blocks: list[_ReferenceCodeBlock] = []
    for match in _REF_CONTEXT_BLOCK_RE.finditer(rendered_prompt):
        attrs = match.group(1)
        type_match = _REF_TYPE_ATTR_RE.search(attrs)
        tier = html.unescape(type_match.group(1)) if type_match is not None else ""
        if tier == "architecture_doc":
            continue

        source_file = ""
        start_line: int | None = None
        end_line: int | None = None
        source_match = _REF_SOURCE_ATTR_RE.search(attrs)
        if source_match is not None:
            raw_source = html.unescape(source_match.group(1))
            span_match = _REF_SOURCE_SPAN_RE.match(raw_source)
            if span_match is not None:
                source_file = normalize_agent_path(span_match.group(1))
                start_line = int(span_match.group(2))
                end_line = int(span_match.group(3))
            else:
                source_file = normalize_agent_path(raw_source)

        sym_match = _REF_SYMBOL_NAME_RE.search(attrs)
        symbol_name = html.unescape(sym_match.group(1)).strip() if sym_match is not None else ""
        calls_match = _REF_CALLS_ATTR_RE.search(attrs)
        calls = (
            tuple(c.strip() for c in html.unescape(calls_match.group(1)).split(",") if c.strip())
            if calls_match is not None
            else ()
        )

        parts: list[str] = []
        if symbol_name:
            parts.append(symbol_name)
        parts.extend(calls)
        for raw_line in split_diff_lines(match.group(2)):
            if raw_line.startswith("### "):
                continue
            parts.append(_REF_LINE_PREFIX_RE.sub("", raw_line))

        blocks.append(
            _ReferenceCodeBlock(
                tier=tier,
                source_file=source_file,
                start_line=start_line,
                end_line=end_line,
                symbol_name=symbol_name,
                calls=calls,
                code_text="\n".join(parts),
            )
        )
    return tuple(blocks)


def _extract_grounding_tokens(text: str) -> set[str]:
    return {
        tok.lower() for tok in _IDENT_RE.findall(text) if tok.lower() not in _NON_GROUNDING_TOKENS
    }


def _normalize_code_ws(text: str) -> str:
    return " ".join(text.split())


def _select_reference_code_for_finding(
    blocks: Sequence[_ReferenceCodeBlock],
    *,
    file_path: str,
    start_line: int,
    end_line: int,
    file_contents: dict[int, str],
) -> str:
    """Return reference code text belonging strictly to `file_path` or its linked symbols."""
    if not blocks:
        return ""
    file_idents = _extract_grounding_tokens("\n".join(file_contents.values()))
    selected: list[str] = []
    for block in blocks:
        if block.tier == "enclosing_symbol":
            if block.source_file != file_path:
                continue
            if (
                block.start_line is not None
                and block.end_line is not None
                and (block.end_line < start_line or block.start_line > end_line)
            ):
                continue
            selected.append(block.code_text)
        elif block.tier == "caller":
            if block.source_file == file_path:
                selected.append(block.code_text)
            elif any(c.lower() in file_idents for c in block.calls):
                selected.append(block.code_text)
            elif not block.calls and (_extract_grounding_tokens(block.code_text) & file_idents):
                selected.append(block.code_text)
        elif block.tier == "callee":
            if block.source_file == file_path or (
                block.symbol_name and block.symbol_name.lower() in file_idents
            ):
                selected.append(block.code_text)
    return "\n".join(selected)


def is_evidence_grounded(
    evidence: str,
    *,
    anchored_lines: Sequence[str],
    start_line: int = 1,
    end_line: int = 1,
    reference_context_text: str = "",
) -> bool:
    """Return True only when evidence cites code/identifiers present in the anchored source."""
    del start_line, end_line
    cleaned = evidence.strip()
    if not cleaned:
        return False

    source_parts = [ln for ln in anchored_lines if ln.strip()]
    if reference_context_text.strip():
        source_parts.append(reference_context_text)
    if not source_parts:
        return False

    source_blob = "\n".join(source_parts)
    source_tokens = _extract_grounding_tokens(source_blob)
    if source_tokens:
        evidence_tokens = {tok.lower() for tok in _IDENT_RE.findall(cleaned)}
        if source_tokens & evidence_tokens:
            return True

    norm_evidence = _normalize_code_ws(cleaned)
    for raw_line in anchored_lines:
        norm_line = _normalize_code_ws(raw_line)
        if len(norm_line) >= 3 and norm_line in norm_evidence:
            return True
    return False


def _strip_outer_code_fence(code: str) -> str:
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    non_empty_indices = [idx for idx, ln in enumerate(lines) if ln.strip()]
    if len(non_empty_indices) >= 2:
        first_idx = non_empty_indices[0]
        last_idx = non_empty_indices[-1]
        open_match = _FENCE_OPEN_RE.match(lines[first_idx])
        close_match = _FENCE_CLOSE_RE.match(lines[last_idx])
        if (
            open_match is not None
            and close_match is not None
            and open_match.group(1)[0] == close_match.group(1)[0]
        ):
            lines = lines[first_idx + 1 : last_idx]
    return "\n".join(lines).rstrip("\n")


def _align_leading_indentation(code: str, orig_first_line: str | None) -> str | None:
    if orig_first_line is None:
        return code
    orig_indent = orig_first_line[: len(orig_first_line) - len(orig_first_line.lstrip(" \t"))]
    lines = code.split("\n")
    first_non_empty = next((ln for ln in lines if ln.strip()), None)
    if first_non_empty is None:
        return None
    sugg_first_indent = first_non_empty[: len(first_non_empty) - len(first_non_empty.lstrip(" \t"))]
    if sugg_first_indent == orig_indent:
        return code

    orig_spaces_only = bool(orig_indent) and "\t" not in orig_indent
    orig_tabs_only = bool(orig_indent) and " " not in orig_indent

    realigned: list[str] = []
    for ln in lines:
        if not ln.strip():
            realigned.append("")
            continue
        if sugg_first_indent and not ln.startswith(sugg_first_indent):
            return None
        rel_body = ln[len(sugg_first_indent) :]
        rel_indent = rel_body[: len(rel_body) - len(rel_body.lstrip(" \t"))]
        if (orig_spaces_only and "\t" in rel_indent) or (orig_tabs_only and " " in rel_indent):
            return None
        realigned.append(f"{orig_indent}{rel_body}")
    return "\n".join(realigned)


def _normalize_security_suggestion(
    raw_suggestion: CodeSuggestion | LLMCodeSuggestionDraft | None,
    *,
    file_line_hunks: dict[int, int],
    file_line_contents: dict[int, str],
    extra_secret_literals: Sequence[str],
) -> CodeSuggestion | None:
    if raw_suggestion is None:
        return None
    s_start = raw_suggestion.start_line
    s_end = raw_suggestion.end_line
    if not (_is_strict_pos_int(s_start) and _is_strict_pos_int(s_end)):
        return None
    if s_start > s_end:
        return None

    start_hunk = file_line_hunks.get(s_start)
    if start_hunk is None or any(
        file_line_hunks.get(ln) != start_hunk for ln in range(s_start, s_end + 1)
    ):
        return None

    cleaned_code = _strip_outer_code_fence(raw_suggestion.replacement_code)
    if not cleaned_code.strip():
        return None

    aligned_code = _align_leading_indentation(cleaned_code, file_line_contents.get(s_start))
    if aligned_code is None:
        return None
    masked_code = mask_sensitive_values(aligned_code, extra_literals=extra_secret_literals)
    masked_expl = (
        mask_sensitive_values(raw_suggestion.explanation, extra_literals=extra_secret_literals)
        if raw_suggestion.explanation is not None
        else None
    )
    try:
        return CodeSuggestion.model_validate(
            {
                "start_line": s_start,
                "end_line": s_end,
                "replacement_code": masked_code,
                "explanation": masked_expl,
            }
        )
    except ValidationError:
        return None


def validate_security_findings(
    drafts: Sequence[FindingCandidate | LLMFindingDraft],
    *,
    batch: DiffBatch,
    batch_context: BuiltBatchContext | None = None,
) -> tuple[list[FindingCandidate], int, int]:
    """Validate coordinate grounding, mask secrets, and preserve suggestion indentation."""
    index = parse_batch_diff_index(batch)
    ref_blocks = (
        _parse_reference_code_blocks(batch_context.rendered_prompt)
        if batch_context is not None
        else ()
    )
    file_secret_literals_cache: dict[str, tuple[str, ...]] = {}

    normalized: list[FindingCandidate] = []
    dropped_count = 0
    ungrounded_count = 0

    for item in drafts:
        norm_path = normalize_agent_path(item.file_path)
        file_hunks = index.line_hunks_by_file.get(norm_path)
        if not norm_path or not file_hunks:
            dropped_count += 1
            continue

        start_line = item.start_line
        end_line = item.end_line
        if not (_is_strict_pos_int(start_line) and _is_strict_pos_int(end_line)):
            dropped_count += 1
            continue
        if start_line > end_line:
            dropped_count += 1
            continue

        start_hunk = file_hunks.get(start_line)
        if start_hunk is None or any(
            file_hunks.get(ln) != start_hunk for ln in range(start_line, end_line + 1)
        ):
            dropped_count += 1
            continue

        file_contents = index.line_contents_by_file.get(norm_path, {})
        anchored_lines = [
            file_contents[ln] for ln in range(start_line, end_line + 1) if ln in file_contents
        ]
        if norm_path not in file_secret_literals_cache:
            file_secret_literals_cache[norm_path] = _extract_diff_secret_literals(
                list(file_contents.values())
            )
        secret_literals = file_secret_literals_cache[norm_path]

        file_ref_context = _select_reference_code_for_finding(
            ref_blocks,
            file_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            file_contents=file_contents,
        )
        if not is_evidence_grounded(
            item.evidence,
            anchored_lines=anchored_lines,
            start_line=start_line,
            end_line=end_line,
            reference_context_text=file_ref_context,
        ):
            ungrounded_count += 1
            dropped_count += 1
            continue

        masked_title = mask_sensitive_values(item.title, extra_literals=secret_literals)
        masked_evidence = mask_sensitive_values(item.evidence, extra_literals=secret_literals)
        confidence = float(item.confidence)

        valid_suggestion = _normalize_security_suggestion(
            item.suggestion,
            file_line_hunks=file_hunks,
            file_line_contents=file_contents,
            extra_secret_literals=secret_literals,
        )
        stable_id = f"b{batch.batch_index:03d}-sec-{len(normalized):03d}"

        try:
            candidate = FindingCandidate.model_validate(
                {
                    "candidate_id": stable_id,
                    "role": "security_auditor",
                    "batch_index": batch.batch_index,
                    "file_path": norm_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "severity": item.severity.strip().upper(),
                    "category": item.category,
                    "title": masked_title,
                    "evidence": masked_evidence,
                    "confidence": confidence,
                    "suggestion": valid_suggestion,
                }
            )
        except ValidationError:
            dropped_count += 1
            continue

        normalized.append(candidate)

    return normalized, dropped_count, ungrounded_count


def build_security_auditor_messages(batch_context: BuiltBatchContext) -> list[BaseMessage]:
    """Construct the System + Human message pair for Security Auditor invocation."""
    return [
        SystemMessage(content=SECURITY_AUDITOR_SYSTEM_PROMPT),
        HumanMessage(content=batch_context.rendered_prompt),
    ]


async def run_security_auditor(
    *,
    batch: DiffBatch,
    batch_context: BuiltBatchContext,
    llm_factory: LLMFactory,
    config: RunnableConfig | None = None,
) -> tuple[list[FindingCandidate], LLMCallMetrics, int]:
    """Run Security Auditor structured LLM call and validate findings against batch RIGHT lines."""
    messages = build_security_auditor_messages(batch_context)
    inv = await llm_factory.invoke_structured(
        role="security_auditor",
        schema=LLMAuditorDraftOutput,
        messages=messages,
        config=config,
    )
    findings, dropped_count, ungrounded_count = validate_security_findings(
        inv.parsed.findings,
        batch=batch,
        batch_context=batch_context,
    )
    metrics = LLMCallMetrics(
        role="security_auditor",
        batch_index=batch.batch_index,
        model_name=inv.model_name,
        input_tokens=inv.input_tokens,
        output_tokens=inv.output_tokens,
        total_tokens=inv.total_tokens,
        estimated_cost_usd=inv.estimated_cost_usd,
        latency_ms=inv.latency_ms,
        attempts=inv.attempts,
    )
    logger.info(
        "security_auditor_completed",
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        finding_count=len(findings),
        dropped_count=dropped_count,
        ungrounded_evidence_count=ungrounded_count,
        latency_ms=inv.latency_ms,
    )
    return findings, metrics, dropped_count
