"""Langfuse Cloud observability, model cost estimation, and strict NFR-3 telemetry masking."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langfuse import Langfuse
from langfuse import __version__ as langfuse_version
from langfuse._client.resource_manager import LangfuseResourceManager
from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanPatch,
    TraceContext,
)
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import BaseModel

from codesight.agents.schemas import TelemetryStatus
from codesight.core.config import GEMINI_MODEL_PRICING_USD_PER_1M, Settings, get_settings

logger = structlog.get_logger("codesight.observability")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Estimated USD list pricing per 1,000,000 input/output tokens for telemetry accounting."""

    input_per_1m_usd: float
    output_per_1m_usd: float


# Estimated Google Gemini Developer API standard list rates (USD per 1M tokens).
MODEL_PRICING_PER_1M_TOKENS: Final[dict[str, ModelPricing]] = {
    model: ModelPricing(input_per_1m_usd=in_rate, output_per_1m_usd=out_rate)
    for model, (in_rate, out_rate) in GEMINI_MODEL_PRICING_USD_PER_1M.items()
}

ALLOWED_METADATA_SUBKEYS: Final[frozenset[str]] = frozenset(
    {
        "pr_id",
        "head_sha",
        "trace_id",
        "role",
        "batch_index",
        "batch_id",
        "batching_mode",
        "provider",
        "langgraph_node",
        "langgraph_step",
        "langgraph_triggers",
        "ls_model_name",
        "ls_provider",
        "ls_model_type",
    }
)

ALLOWED_TELEMETRY_KEYS: Final[frozenset[str]] = ALLOWED_METADATA_SUBKEYS | frozenset(
    {
        "total_diff_tokens",
        "batch_count",
        "finding_count",
        "verdict_count",
        "unreviewable_count",
        "truncated_candidates_count",
        "dropped_invalid_findings_count",
        "dropped_invalid_verdicts_count",
        "model_name",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "latency_ms",
        "status",
        "error_type",
        "redacted",
        "message_count",
        "file_count",
        "attempts",
        "type",
        "count",
    }
)

ALLOWED_OTEL_EXACT_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "langfuse.trace.name",
        "langfuse.trace.tags",
        "langfuse.trace.public",
        "langfuse.environment",
        "langfuse.release",
        "langfuse.version",
        "langfuse.internal.as_root",
        "langfuse.internal.is_app_root",
        "langfuse.observation.type",
        "langfuse.observation.level",
        "langfuse.observation.model.name",
        "langfuse.observation.model.parameters",
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "langfuse.observation.completion_start_time",
        "session.id",
        "user.id",
    }
)

_IO_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
    }
)

_STATUS_MESSAGE_ATTRIBUTE: Final[str] = "langfuse.observation.status_message"
_SAFE_ERROR_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_TRACE_HEX_32_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
_SECRET_LEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:AIza[0-9A-Za-z_-]{8,}|sk-lf-[^\s]+|pk-lf-[^\s]+|sk-[A-Za-z0-9_-]{12,}"
    r"|gh[psou]_[A-Za-z0-9_]{8,}|Bearer\s+[A-Za-z0-9._-]+|BEGIN\s+.*PRIVATE\s+KEY)",
    re.IGNORECASE,
)


def _normalize_model_name(model_name: str) -> str:
    cleaned = model_name.strip()
    if cleaned.startswith("models/"):
        cleaned = cleaned[len("models/") :]
    return cleaned


def estimate_model_cost_usd(
    model_name: str,
    *,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, float] | None:
    """Estimate USD input/output/total cost when unit pricing exists for `model_name`."""
    normalized = _normalize_model_name(model_name)
    pricing = MODEL_PRICING_PER_1M_TOKENS.get(normalized)
    if pricing is None:
        return None
    safe_in = max(0, input_tokens)
    safe_out = max(0, output_tokens)
    in_cost = round((safe_in / 1_000_000.0) * pricing.input_per_1m_usd, 8)
    out_cost = round((safe_out / 1_000_000.0) * pricing.output_per_1m_usd, 8)
    total_cost = round(in_cost + out_cost, 8)
    return {"input": in_cost, "output": out_cost, "total": total_cost}


def _is_safe_short_string(value: str) -> bool:
    if len(value) > 128 or "\n" in value or "\r" in value:
        return False
    return _SECRET_LEAK_RE.search(value) is None


def _sanitize_primitive(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if _is_safe_short_string(value) else "[REDACTED]"
    return "[REDACTED]"


def _sanitize_error_identifier(raw: Any) -> str:
    if isinstance(raw, BaseException):
        token = type(raw).__name__
    elif isinstance(raw, str):
        first = raw.strip().split(":", 1)[0].split("\n", 1)[0].strip()
        token = first if _SAFE_ERROR_TOKEN_RE.fullmatch(first) else "Error"
    else:
        token = "Error"
    if not _SAFE_ERROR_TOKEN_RE.fullmatch(token) or _SECRET_LEAK_RE.search(token):
        return "Error"
    return token


def sanitize_telemetry_value(data: Any, **_: Any) -> Any:
    """Enforce strict allowlist redaction for NFR-3: keep IDs/metrics only, strip code/secrets."""
    if data is None or isinstance(data, (bool, int, float)):
        return data
    if isinstance(data, BaseModel):
        return _summarize_model_safely(data)
    if isinstance(data, Mapping):
        sanitized: dict[str, Any] = {}
        dropped = False
        for key, value in data.items():
            key_str = str(key)
            if key_str not in ALLOWED_TELEMETRY_KEYS:
                dropped = True
                continue
            if isinstance(value, (Mapping, list, tuple, set, BaseModel)):
                sanitized[key_str] = sanitize_telemetry_value(value)
            else:
                sanitized[key_str] = _sanitize_primitive(value)
        if dropped:
            sanitized["redacted"] = True
        return sanitized
    if isinstance(data, (list, tuple, set)):
        return {"redacted": True, "count": len(data)}
    return {"redacted": True, "type": type(data).__name__}


def _summarize_model_safely(model: BaseModel) -> dict[str, Any]:
    """Extract only safe scalar IDs and counts from Pydantic models before telemetry export."""
    summary: dict[str, Any] = {"type": type(model).__name__, "redacted": True}
    for key in (
        "pr_id",
        "head_sha",
        "trace_id",
        "role",
        "batch_index",
        "batch_id",
        "batching_mode",
        "total_diff_tokens",
        "status",
    ):
        if hasattr(model, key):
            summary[key] = _sanitize_primitive(getattr(model, key))
    for attr_name, count_key in (
        ("batches", "batch_count"),
        ("findings", "finding_count"),
        ("merged_findings", "finding_count"),
        ("verdicts", "verdict_count"),
        ("merged_verdicts", "verdict_count"),
        ("unreviewable_segments", "unreviewable_count"),
    ):
        val = getattr(model, attr_name, None)
        if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
            summary[count_key] = len(val)
    return summary


def _extract_usage_tokens(raw_usage: Any) -> tuple[int, int]:
    usage_obj: Any = raw_usage
    if isinstance(raw_usage, str):
        try:
            usage_obj = json.loads(raw_usage)
        except json.JSONDecodeError:
            return 0, 0
    if not isinstance(usage_obj, Mapping):
        return 0, 0
    raw_in = usage_obj.get("input", usage_obj.get("input_tokens", 0))
    raw_out = usage_obj.get("output", usage_obj.get("output_tokens", 0))
    in_tok = raw_in if isinstance(raw_in, int) and not isinstance(raw_in, bool) else 0
    out_tok = raw_out if isinstance(raw_out, int) and not isinstance(raw_out, bool) else 0
    return max(0, in_tok), max(0, out_tok)


def _needs_cost_enrichment(raw_cost: Any) -> bool:
    if raw_cost is None:
        return True
    cost_obj: Any = raw_cost
    if isinstance(raw_cost, str):
        try:
            cost_obj = json.loads(raw_cost)
        except json.JSONDecodeError:
            return True
    if not isinstance(cost_obj, Mapping):
        return True
    total = cost_obj.get("total", 0)
    return not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0


def mask_otel_spans_allowlist(params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    """Sanitize every outgoing OpenTelemetry span right before export and attach model costs."""
    patches: dict[Any, OtelSpanPatch] = {}

    for span_id, span_data in params.spans.items():
        attrs = span_data.attributes
        set_attrs: dict[str, Any] = {}
        del_attrs: list[str] = []

        for key, value in attrs.items():
            if key in ALLOWED_OTEL_EXACT_ATTRIBUTES:
                if isinstance(value, str) and _SECRET_LEAK_RE.search(value):
                    del_attrs.append(key)
                continue

            if key in _IO_ATTRIBUTES:
                parsed: Any = value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        parsed = value
                set_attrs[key] = json.dumps(sanitize_telemetry_value(parsed), sort_keys=True)
                continue

            if key == _STATUS_MESSAGE_ATTRIBUTE:
                set_attrs[key] = _sanitize_error_identifier(value)
                continue

            if key.startswith(("langfuse.observation.metadata.", "langfuse.trace.metadata.")):
                subkey = key.rsplit(".", 1)[-1]
                if subkey in ALLOWED_METADATA_SUBKEYS:
                    if isinstance(value, str) and not _is_safe_short_string(value):
                        del_attrs.append(key)
                else:
                    del_attrs.append(key)
                continue

            del_attrs.append(key)

        # Attach estimated model cost when usage tokens & model pricing are available.
        model_name = attrs.get("langfuse.observation.model.name")
        usage_attr = attrs.get("langfuse.observation.usage_details")
        if (
            isinstance(model_name, str)
            and usage_attr is not None
            and _needs_cost_enrichment(attrs.get("langfuse.observation.cost_details"))
        ):
            in_tok, out_tok = _extract_usage_tokens(usage_attr)
            if in_tok > 0 or out_tok > 0:
                cost = estimate_model_cost_usd(
                    model_name,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )
                if cost is not None:
                    set_attrs["langfuse.observation.cost_details"] = json.dumps(
                        cost,
                        sort_keys=True,
                    )

        if set_attrs or del_attrs:
            patches[span_id] = OtelSpanPatch(
                set_attributes=set_attrs,
                delete_attributes=del_attrs,
            )

    return MaskOtelSpansResult(span_patches=patches)


class _ResilientSpanExporter(SpanExporter):
    """Wraps an OTel SpanExporter so export/flush failures degrade telemetry without crashing."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._error_seq = 0
        self.last_error_type: str | None = None

    def snapshot_error_seq(self) -> int:
        """Return monotonic export/flush error counter for session-scoped health checks."""
        with self._lock:
            return self._error_seq

    def _record_error(self, error_type: str) -> None:
        with self._lock:
            self._error_seq += 1
            self.last_error_type = error_type

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
            if result != SpanExportResult.SUCCESS:
                self._record_error("SpanExportFailure")
                logger.warning(
                    "langfuse_span_export_degraded",
                    error_type="SpanExportFailure",
                )
            return result
        except Exception as exc:
            err_type = type(exc).__name__
            self._record_error(err_type)
            logger.warning(
                "langfuse_span_export_degraded",
                error_type=err_type,
            )
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            flushed = self._inner.force_flush(timeout_millis=timeout_millis)
            if not flushed:
                with self._lock:
                    self._error_seq += 1
                    if self.last_error_type is None:
                        self.last_error_type = "SpanExportFailure"
                logger.warning(
                    "langfuse_span_flush_degraded",
                    error_type=self.last_error_type,
                )
            return flushed
        except Exception as exc:
            err_type = type(exc).__name__
            self._record_error(err_type)
            logger.warning(
                "langfuse_span_flush_degraded",
                error_type=err_type,
            )
            return False

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as exc:
            with self._lock:
                if self.last_error_type is None:
                    self.last_error_type = type(exc).__name__


class CodeSightLangfuseCallbackHandler(LangfuseCallbackHandler):
    """Zero-code, failure-isolated Langfuse LangChain callback handler (NFR-3)."""

    def __init__(
        self,
        *,
        public_key: str | None = None,
        trace_context: TraceContext | None = None,
        langfuse_client: Langfuse | None = None,
    ) -> None:
        super().__init__(public_key=public_key, trace_context=trace_context)
        if langfuse_client is not None:
            self._langfuse_client = langfuse_client
        self.degraded: bool = False
        self.last_error_type: str | None = None

    def _record_degraded(self, exc: BaseException) -> None:
        self.degraded = True
        self.last_error_type = type(exc).__name__
        logger.warning(
            "langfuse_callback_degraded",
            error_type=self.last_error_type,
        )

    def _get_error_level_and_status_message(
        self,
        error: BaseException,
    ) -> tuple[Literal["DEFAULT", "ERROR"], str]:
        level, _ = super()._get_error_level_and_status_message(error)
        return level, _sanitize_error_identifier(error)

    @staticmethod
    def _scrub_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if not metadata:
            return None
        cleaned: dict[str, Any] = {}
        for key, value in metadata.items():
            if key in ALLOWED_METADATA_SUBKEYS:
                cleaned[key] = _sanitize_primitive(value)
        return cleaned

    @staticmethod
    def _scrub_llm_result(response: LLMResult) -> LLMResult:
        scrubbed_gens: list[list[Any]] = []
        for gen_list in response.generations:
            row: list[Any] = []
            for gen in gen_list:
                if isinstance(gen, ChatGeneration):
                    msg = gen.message
                    safe_resp_meta: dict[str, Any] = {}
                    raw_resp_meta = getattr(msg, "response_metadata", None)
                    if isinstance(raw_resp_meta, Mapping):
                        model_name = raw_resp_meta.get("model_name") or raw_resp_meta.get("model")
                        if isinstance(model_name, str) and _is_safe_short_string(model_name):
                            safe_resp_meta["model_name"] = model_name
                    scrubbed_msg = msg.model_copy(
                        update={
                            "content": '{"redacted": true}',
                            "additional_kwargs": {},
                            "response_metadata": safe_resp_meta,
                        }
                    )
                    if hasattr(scrubbed_msg, "tool_calls"):
                        try:
                            scrubbed_msg = scrubbed_msg.model_copy(update={"tool_calls": []})
                        except Exception:
                            pass
                    if hasattr(scrubbed_msg, "invalid_tool_calls"):
                        try:
                            scrubbed_msg = scrubbed_msg.model_copy(
                                update={"invalid_tool_calls": []}
                            )
                        except Exception:
                            pass
                    safe_gen_info: dict[str, Any] = {}
                    if isinstance(gen.generation_info, Mapping):
                        if "usage_metadata" in gen.generation_info:
                            safe_gen_info["usage_metadata"] = gen.generation_info["usage_metadata"]
                        if "model_name" in gen.generation_info:
                            safe_gen_info["model_name"] = _sanitize_primitive(
                                gen.generation_info["model_name"]
                            )
                    row.append(
                        ChatGeneration(
                            message=scrubbed_msg,
                            generation_info=safe_gen_info or None,
                        )
                    )
                else:
                    copied = copy.copy(gen)
                    copied.text = '{"redacted": true}'
                    copied.generation_info = None
                    row.append(copied)
            scrubbed_gens.append(row)

        safe_llm_output: dict[str, Any] | None = None
        if isinstance(response.llm_output, Mapping):
            safe_llm_output = {}
            for key in ("model_name", "model", "token_usage", "usage"):
                if key in response.llm_output:
                    safe_llm_output[key] = response.llm_output[key]
        return LLMResult(generations=scrubbed_gens, llm_output=safe_llm_output)

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            scrubbed_inputs = cast(dict[str, Any], sanitize_telemetry_value(inputs))
            return super().on_chain_start(
                serialized,
                scrubbed_inputs,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=self._scrub_metadata(metadata),
                **kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            scrubbed_outputs = sanitize_telemetry_value(outputs)
            safe_kwargs = {k: v for k, v in kwargs.items() if k != "inputs"}
            return super().on_chain_end(
                scrubbed_outputs,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **safe_kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            safe_kwargs = {k: v for k, v in kwargs.items() if k != "inputs"}
            super().on_chain_error(
                error,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                **safe_kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            redacted_messages: list[list[BaseMessage]] = [
                [HumanMessage(content=json.dumps({"redacted": True, "message_count": len(batch)}))]
                for batch in messages
            ]
            return super().on_chat_model_start(
                serialized,
                redacted_messages,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=self._scrub_metadata(metadata),
                **kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            redacted_prompts = [
                json.dumps({"redacted": True, "prompt_count": len(prompts)}) for _ in prompts
            ]
            return super().on_llm_start(
                serialized,
                redacted_prompts,
                run_id=run_id,
                parent_run_id=parent_run_id,
                tags=tags,
                metadata=self._scrub_metadata(metadata),
                **kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            scrubbed = self._scrub_llm_result(response)
            safe_kwargs = {k: v for k, v in kwargs.items() if k != "inputs"}
            return super().on_llm_end(
                scrubbed,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **safe_kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            safe_kwargs = {k: v for k, v in kwargs.items() if k != "inputs"}
            return super().on_llm_error(
                error,
                run_id=run_id,
                parent_run_id=parent_run_id,
                **safe_kwargs,
            )
        except Exception as exc:
            self._record_degraded(exc)
            return None


_CACHED_LANGFUSE_CLIENTS: dict[
    tuple[str, str, str, str], tuple[Langfuse, _ResilientSpanExporter]
] = {}
_CLIENT_CACHE_LOCK = threading.Lock()


def reset_cached_langfuse_clients() -> None:
    """Shutdown and clear cached process-level Langfuse clients (primarily for test isolation)."""
    with _CLIENT_CACHE_LOCK:
        cached = list(_CACHED_LANGFUSE_CLIENTS.values())
        _CACHED_LANGFUSE_CLIENTS.clear()
    for client, _ in cached:
        try:
            client.shutdown()
        except Exception:
            pass


def _build_default_otlp_exporter(
    *,
    public_key: str,
    secret_key: str,
    base_url: str,
    timeout_seconds: int = 3,
) -> SpanExporter:
    basic_auth_header = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode(
        "ascii"
    )
    headers = {
        "Authorization": basic_auth_header,
        "x-langfuse-sdk-name": "python",
        "x-langfuse-sdk-version": langfuse_version,
        "x-langfuse-public-key": public_key,
        "x-langfuse-ingestion-version": "4",
    }
    endpoint = f"{base_url.rstrip('/')}/api/public/otel/v1/traces"
    return OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers,
        timeout=timeout_seconds,
        compression=Compression.Gzip,
    )


def _normalize_langfuse_trace_id(raw_trace_id: str | None) -> str:
    if raw_trace_id is not None:
        cleaned = raw_trace_id.strip().lower()
        if _TRACE_HEX_32_RE.fullmatch(cleaned):
            return cleaned
        if cleaned and cleaned != "-":
            return Langfuse.create_trace_id(seed=cleaned)
    return Langfuse.create_trace_id()


class LangfuseTraceSession:
    """Manages an isolated or cached Langfuse trace session with non-blocking flush."""

    def __init__(
        self,
        *,
        client: Langfuse | None,
        handler: CodeSightLangfuseCallbackHandler | None,
        telemetry_status: TelemetryStatus,
        resilient_exporter: _ResilientSpanExporter | None = None,
        isolated: bool = False,
        public_key: str | None = None,
    ) -> None:
        self._client = client
        self._handler = handler
        self._telemetry_status = telemetry_status
        self._resilient_exporter = resilient_exporter
        self._start_error_seq = (
            resilient_exporter.snapshot_error_seq() if resilient_exporter is not None else 0
        )
        self._isolated = isolated
        self._public_key = public_key
        self._closed = False

    @property
    def telemetry_status(self) -> TelemetryStatus:
        """Return the latest immutable telemetry status snapshot."""
        return self._telemetry_status

    @property
    def callbacks(self) -> tuple[CodeSightLangfuseCallbackHandler, ...]:
        """Return active LangChain callbacks (empty when telemetry is disabled)."""
        return (self._handler,) if self._handler is not None else ()

    async def afetch_trace_url(self, *, timeout_seconds: float = 3.0) -> str | None:
        """Fetch Langfuse trace UI URL off the event loop (for diagnostics/smoke runs)."""
        if self._client is None or self._telemetry_status.trace_id is None:
            return None
        trace_id = self._telemetry_status.trace_id
        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(self._client.get_trace_url, trace_id=trace_id),
                timeout=timeout_seconds,
            )
            if url:
                self._telemetry_status = self._telemetry_status.model_copy(
                    update={"trace_url": url}
                )
            return url
        except Exception:
            return None

    def flush_and_close(self) -> TelemetryStatus:
        """Flush pending Langfuse spans synchronously, recording degraded status on failure."""
        if self._closed:
            return self._telemetry_status
        self._closed = True

        if self._client is None:
            return self._telemetry_status

        error_type: str | None = None
        flushed = False
        try:
            self._client.flush()
            flushed = True
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning("langfuse_flush_failed", error_type=error_type)
        finally:
            if self._isolated:
                try:
                    self._client.shutdown()
                except Exception as exc:
                    if error_type is None:
                        error_type = type(exc).__name__
                finally:
                    if self._public_key is not None:
                        LangfuseResourceManager._instances.pop(self._public_key, None)

        if self._handler is not None and self._handler.degraded and error_type is None:
            error_type = self._handler.last_error_type or "CallbackError"
        if (
            self._resilient_exporter is not None
            and self._resilient_exporter.snapshot_error_seq() > self._start_error_seq
            and error_type is None
        ):
            error_type = self._resilient_exporter.last_error_type or "SpanExportFailure"

        if error_type is not None:
            self._telemetry_status = self._telemetry_status.model_copy(
                update={
                    "active": False,
                    "status": "degraded",
                    "error_type": error_type,
                    "flushed": False,
                }
            )
        else:
            self._telemetry_status = self._telemetry_status.model_copy(
                update={
                    "active": True,
                    "status": "active",
                    "error_type": None,
                    "flushed": flushed,
                }
            )
        return self._telemetry_status

    async def aflush_and_close(self, *, timeout_seconds: float = 2.0) -> TelemetryStatus:
        """Flush pending Langfuse spans off the event loop with a hard timeout."""
        if self._closed or self._client is None:
            return self._telemetry_status
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.flush_and_close),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            self._closed = True
            logger.warning("langfuse_flush_timeout", timeout_seconds=timeout_seconds)
            self._telemetry_status = self._telemetry_status.model_copy(
                update={
                    "active": False,
                    "status": "degraded",
                    "error_type": "TimeoutError",
                    "flushed": False,
                }
            )
            return self._telemetry_status


def _verify_masked_langfuse_client(client: Langfuse) -> None:
    resources = getattr(client, "_resources", None)
    if resources is not None and (
        getattr(resources, "mask_otel_spans", None) is not mask_otel_spans_allowlist
        or getattr(resources, "mask", None) is not sanitize_telemetry_value
    ):
        raise RuntimeError("UnmaskedLangfuseSingleton")


def _get_or_create_cached_langfuse_client(
    *,
    public_key: str,
    secret_key: str,
    base_url: str,
    environment: str,
) -> tuple[Langfuse, _ResilientSpanExporter]:
    secret_digest = hashlib.sha256(secret_key.encode("utf-8")).hexdigest()
    cache_key = (public_key, secret_digest, base_url, environment)
    with _CLIENT_CACHE_LOCK:
        cached = _CACHED_LANGFUSE_CLIENTS.get(cache_key)
        if cached is not None:
            _verify_masked_langfuse_client(cached[0])
            return cached

        raw_otlp = _build_default_otlp_exporter(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            timeout_seconds=3,
        )
        resilient_exporter = _ResilientSpanExporter(raw_otlp)
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            environment=environment,
            timeout=3,
            tracing_enabled=True,
            mask=sanitize_telemetry_value,
            mask_otel_spans=mask_otel_spans_allowlist,
            tracer_provider=TracerProvider(),
            span_exporter=resilient_exporter,
        )
        _verify_masked_langfuse_client(client)
        _CACHED_LANGFUSE_CLIENTS[cache_key] = (client, resilient_exporter)
        return client, resilient_exporter


def create_langfuse_trace_session(
    *,
    settings: Settings | None = None,
    trace_id: str | None = None,
    span_exporter: SpanExporter | None = None,
) -> LangfuseTraceSession:
    """Create a NFR-3 masked Langfuse trace session, degrading cleanly if disabled or failing."""
    cfg = settings if settings is not None else get_settings()
    if not cfg.langfuse_tracing_enabled:
        return LangfuseTraceSession(
            client=None,
            handler=None,
            telemetry_status=TelemetryStatus(
                enabled=False,
                active=False,
                trace_id=trace_id,
                status="disabled",
            ),
        )

    if not cfg.has_langfuse_credentials:
        return LangfuseTraceSession(
            client=None,
            handler=None,
            telemetry_status=TelemetryStatus(
                enabled=False,
                active=False,
                trace_id=trace_id,
                status="disabled",
            ),
        )

    assert cfg.langfuse_public_key is not None
    assert cfg.langfuse_secret_key is not None
    public_key = cfg.langfuse_public_key.strip()
    secret_key = cfg.langfuse_secret_key.get_secret_value().strip()
    lf_trace_id = _normalize_langfuse_trace_id(trace_id)

    try:
        isolated = span_exporter is not None
        if span_exporter is not None:
            resilient_exporter = _ResilientSpanExporter(span_exporter)
            # Clear any stale cached test client for the same public_key and isolate TracerProvider.
            LangfuseResourceManager._instances.pop(public_key, None)
            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=cfg.langfuse_host,
                environment=cfg.app_env,
                tracing_enabled=True,
                mask=sanitize_telemetry_value,
                mask_otel_spans=mask_otel_spans_allowlist,
                tracer_provider=TracerProvider(),
                span_exporter=resilient_exporter,
            )
        else:
            client, resilient_exporter = _get_or_create_cached_langfuse_client(
                public_key=public_key,
                secret_key=secret_key,
                base_url=cfg.langfuse_host,
                environment=cfg.app_env,
            )

        handler = CodeSightLangfuseCallbackHandler(
            public_key=public_key,
            trace_context=TraceContext(trace_id=lf_trace_id),
            langfuse_client=client,
        )

        return LangfuseTraceSession(
            client=client,
            handler=handler,
            telemetry_status=TelemetryStatus(
                enabled=True,
                active=True,
                trace_id=lf_trace_id,
                trace_url=None,
                status="active",
            ),
            resilient_exporter=resilient_exporter,
            isolated=isolated,
            public_key=public_key,
        )
    except Exception as exc:
        err_type = type(exc).__name__
        logger.warning("langfuse_session_init_degraded", error_type=err_type)
        return LangfuseTraceSession(
            client=None,
            handler=None,
            telemetry_status=TelemetryStatus(
                enabled=True,
                active=False,
                trace_id=lf_trace_id,
                status="degraded",
                error_type=err_type,
            ),
        )
