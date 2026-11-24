"""Model-agnostic LLMFactory with shared Semaphore(3) concurrency guard and 429 retry backoff."""

from __future__ import annotations

import asyncio
import importlib
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, Protocol, TypeVar, cast
from weakref import WeakKeyDictionary

import httpx
import structlog
from google.genai import errors as genai_errors
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import (
    GoogleAPIError,
    GoogleAuthenticationError,
    GoogleContextOverflowError,
    GoogleInvalidRequestError,
    GoogleModelNotFoundError,
    GooglePermissionDeniedError,
    GoogleRateLimitError,
)
from pydantic import BaseModel, SecretStr

from codesight.agents.schemas import LLMRole
from codesight.core.config import Settings, get_settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError, parse_retry_after_header
from codesight.core.logging import get_trace_id
from codesight.core.observability import estimate_model_cost_usd
from codesight.rag.embedder import parse_gemini_retry_delay

T = TypeVar("T", bound=BaseModel)

type SleepFn = Callable[[float], Awaitable[None]]
type ClockFn = Callable[[], float]
type ChatModelBuilder = Callable[[LLMRole, str], BaseChatModel]
type ProviderModelBuilder = Callable[[str, Settings, str | None, float, int], BaseChatModel]

DEFAULT_LLM_MAX_CONCURRENCY: Final[int] = 3
DEFAULT_LLM_MAX_RETRIES: Final[int] = 3
DEFAULT_LLM_RETRY_BASE_SECONDS: Final[float] = 1.0
DEFAULT_LLM_RETRY_CAP_SECONDS: Final[float] = 15.0
DEFAULT_LLM_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_LLM_MAX_OUTPUT_TOKENS: Final[int] = 4_096
DEFAULT_LLM_MAX_TOTAL_WAIT_SECONDS: Final[float] = 20.0
_RETRY_AFTER_MAX_JITTER_SECONDS: Final[float] = 0.25
_RETRIABLE_HTTP_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
_SYSTEM_RNG: Final[random.SystemRandom] = random.SystemRandom()
_SHARED_LOOP_SEMAPHORES: Final[
    WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]]
] = WeakKeyDictionary()

logger = structlog.get_logger("codesight.agents.llm_factory")


class LLMConfigurationError(RuntimeError):
    """Raised when LLM provider configuration, credentials, or model selection is invalid."""


class LLMStructuredOutputError(RuntimeError):
    """Raised when an LLM fails to produce a valid Pydantic structured output."""


@dataclass(frozen=True, slots=True)
class StructuredInvocationResult[TModel: BaseModel]:
    """Result envelope pairing parsed Pydantic output with token, cost, and latency telemetry."""

    parsed: TModel
    model_name: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    latency_ms: float
    attempts: int


class StructuredLLMInvoker(Protocol):
    """Pluggable async callable executing one structured-output LLM invocation."""

    async def __call__(
        self,
        role: LLMRole,
        model_name: str,
        schema: type[T],
        messages: list[BaseMessage],
        config: RunnableConfig | None,
    ) -> StructuredInvocationResult[T] | T: ...


@dataclass(frozen=True, slots=True)
class _ErrorClassification:
    retryable: bool
    is_rate_limit: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
    return chain


def _extract_headers_from_chain(chain: Sequence[BaseException]) -> dict[str, str]:
    for item in chain:
        response = getattr(item, "response", None)
        raw_headers = getattr(response, "headers", None)
        if isinstance(raw_headers, Mapping):
            return {str(k): str(v) for k, v in raw_headers.items()}
    return {}


def _extract_retry_hint(chain: Sequence[BaseException]) -> float | None:
    for item in chain:
        hint = parse_gemini_retry_delay(item, {})
        if hint is not None:
            return hint
    headers = _extract_headers_from_chain(chain)
    return parse_retry_after_header(headers) if headers else None


def _extract_status_code(chain: Sequence[BaseException]) -> int | None:
    for item in chain:
        for attr in ("code", "status_code"):
            raw = getattr(item, attr, None)
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
        response = getattr(item, "response", None)
        raw_resp_code = getattr(response, "status_code", None)
        if isinstance(raw_resp_code, int) and not isinstance(raw_resp_code, bool):
            return raw_resp_code
    return None


def _is_transient_transport_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
            TimeoutError,
            ConnectionError,
        ),
    )


def classify_llm_exception(exc: BaseException) -> _ErrorClassification:
    """Classify an LLM exception into permanent vs retryable (429 rate-limit / 5xx / network)."""
    chain = _iter_exception_chain(exc)

    # Permanent configuration / auth / validation / context-overflow errors always fail fast.
    for item in chain:
        if isinstance(
            item,
            (
                LLMConfigurationError,
                LLMStructuredOutputError,
                GoogleAuthenticationError,
                GooglePermissionDeniedError,
                GoogleInvalidRequestError,
                GoogleModelNotFoundError,
                GoogleContextOverflowError,
                ValueError,
                TypeError,
            ),
        ):
            return _ErrorClassification(
                retryable=False,
                is_rate_limit=False,
                status_code=_extract_status_code(chain),
            )

    status_code = _extract_status_code(chain)
    retry_hint = _extract_retry_hint(chain)

    # Explicit 429 / RESOURCE_EXHAUSTED detection.
    is_429 = (
        status_code == 429
        or any(isinstance(item, GoogleRateLimitError) for item in chain)
        or any("RESOURCE_EXHAUSTED" in str(item).upper() for item in chain)
    )
    if is_429:
        return _ErrorClassification(
            retryable=True,
            is_rate_limit=True,
            status_code=429,
            retry_after_seconds=retry_hint,
        )

    # Transient server / timeout / transport errors.
    if (
        (status_code is not None and status_code in _RETRIABLE_HTTP_STATUS_CODES)
        or any(isinstance(item, (genai_errors.ServerError, GoogleAPIError)) for item in chain)
        or any(_is_transient_transport_error(item) for item in chain)
    ):
        return _ErrorClassification(
            retryable=True,
            is_rate_limit=False,
            status_code=status_code,
            retry_after_seconds=retry_hint,
        )

    return _ErrorClassification(retryable=False, is_rate_limit=False, status_code=status_code)


def _resolve_gemini_api_key(settings: Settings, override_key: str | None) -> str:
    if override_key is not None and override_key.strip():
        return override_key.strip()
    if settings.gemini_api_key is not None:
        cleaned = settings.gemini_api_key.get_secret_value().strip()
        if cleaned:
            return cleaned
    raise LLMConfigurationError("GEMINI_API_KEY is required to initialize Gemini chat models.")


def _build_gemini_chat_model(
    model_name: str,
    settings: Settings,
    override_api_key: str | None,
    timeout_seconds: float,
    max_output_tokens: int,
) -> BaseChatModel:
    api_key = _resolve_gemini_api_key(settings, override_api_key)
    # Pass max_retries=1 so langchain-google-genai configures HttpRetryOptions(attempts=1)
    # (1 total attempt, 0 SDK retries) and never double-stacks retries with LLMFactory.
    return ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=SecretStr(api_key),
        temperature=0.0,
        max_retries=1,
        timeout=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


def _build_optional_provider_model(
    *,
    provider: str,
    module_name: str,
    class_name: str,
    model_name: str,
    override_api_key: str | None,
    timeout_seconds: float,
    max_output_tokens: int,
) -> BaseChatModel:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LLMConfigurationError(
            f"Provider '{provider}' requires optional package '{module_name.replace('_', '-')}' "
            "to be installed via `uv add`."
        ) from exc
    model_cls = getattr(module, class_name)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "temperature": 0.0,
        "max_retries": 0,
        "timeout": timeout_seconds,
        "max_tokens": max_output_tokens,
    }
    if override_api_key is not None and override_api_key.strip():
        kwargs["api_key"] = SecretStr(override_api_key.strip())
    return cast(BaseChatModel, model_cls(**kwargs))


def _build_openai_chat_model(
    model_name: str,
    _settings: Settings,
    override_api_key: str | None,
    timeout_seconds: float,
    max_output_tokens: int,
) -> BaseChatModel:
    return _build_optional_provider_model(
        provider="openai",
        module_name="langchain_openai",
        class_name="ChatOpenAI",
        model_name=model_name,
        override_api_key=override_api_key,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


def _build_anthropic_chat_model(
    model_name: str,
    _settings: Settings,
    override_api_key: str | None,
    timeout_seconds: float,
    max_output_tokens: int,
) -> BaseChatModel:
    return _build_optional_provider_model(
        provider="anthropic",
        module_name="langchain_anthropic",
        class_name="ChatAnthropic",
        model_name=model_name,
        override_api_key=override_api_key,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


_DEFAULT_PROVIDER_BUILDERS: Final[dict[str, ProviderModelBuilder]] = {
    "gemini": _build_gemini_chat_model,
    "openai": _build_openai_chat_model,
    "anthropic": _build_anthropic_chat_model,
}


def _extract_usage_from_message(raw_message: Any) -> tuple[int, int, int]:
    usage = getattr(raw_message, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        return 0, 0, 0
    raw_in = usage.get("input_tokens", 0)
    raw_out = usage.get("output_tokens", 0)
    raw_total = usage.get("total_tokens", 0)
    in_tok = raw_in if isinstance(raw_in, int) and not isinstance(raw_in, bool) else 0
    out_tok = raw_out if isinstance(raw_out, int) and not isinstance(raw_out, bool) else 0
    total_tok = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else 0
    safe_in = max(0, in_tok)
    safe_out = max(0, out_tok)
    safe_total = max(safe_in + safe_out, total_tok)
    return safe_in, safe_out, safe_total


class LLMFactory:
    """Model-agnostic chat factory with shared per-loop Semaphore(3) and 429 exponential backoff."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        provider: str | None = None,
        auditor_model: str | None = None,
        critic_model: str | None = None,
        api_key: str | SecretStr | None = None,
        max_concurrency: int | None = None,
        max_retries: int = DEFAULT_LLM_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_LLM_RETRY_BASE_SECONDS,
        retry_cap_seconds: float = DEFAULT_LLM_RETRY_CAP_SECONDS,
        request_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        max_output_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        max_total_wait_seconds: float | None = DEFAULT_LLM_MAX_TOTAL_WAIT_SECONDS,
        semaphore: asyncio.Semaphore | None = None,
        chat_model_builder: ChatModelBuilder | None = None,
        structured_invoker: StructuredLLMInvoker | None = None,
        sleep_fn: SleepFn = asyncio.sleep,
        clock_fn: ClockFn = time.monotonic,
        rng: random.Random = _SYSTEM_RNG,
    ) -> None:
        cfg = settings if settings is not None else get_settings()
        resolved_concurrency = (
            max_concurrency if max_concurrency is not None else cfg.llm_max_concurrency
        )
        if (
            isinstance(resolved_concurrency, bool)
            or not isinstance(resolved_concurrency, int)
            or resolved_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer.")
        if retry_base_seconds <= 0.0 or retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_base_seconds must be > 0 and <= retry_cap_seconds.")
        if request_timeout_seconds <= 0.0:
            raise ValueError("request_timeout_seconds must be > 0.")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer.")
        if max_total_wait_seconds is not None and (
            isinstance(max_total_wait_seconds, bool) or max_total_wait_seconds <= 0.0
        ):
            raise ValueError("max_total_wait_seconds must be > 0 when provided.")

        self._settings = cfg
        self._provider = (provider if provider is not None else cfg.llm_provider).strip().lower()
        self._auditor_model = (
            auditor_model if auditor_model is not None else cfg.auditor_model
        ).strip()
        self._critic_model = (
            critic_model if critic_model is not None else cfg.critic_model
        ).strip()
        if isinstance(api_key, SecretStr):
            self._api_key_override: str | None = api_key.get_secret_value()
        else:
            self._api_key_override = api_key

        self._max_concurrency = resolved_concurrency
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._max_total_wait_seconds = max_total_wait_seconds
        self._explicit_semaphore = semaphore
        self._chat_model_builder = chat_model_builder
        self._structured_invoker = structured_invoker
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._rng = rng
        self._providers: dict[str, ProviderModelBuilder] = dict(_DEFAULT_PROVIDER_BUILDERS)
        self._chat_model_cache: dict[tuple[LLMRole, str], BaseChatModel] = {}

    @property
    def settings(self) -> Settings:
        """Return Settings bound to this factory."""
        return self._settings

    @property
    def provider(self) -> str:
        """Return active provider identifier."""
        return self._provider

    @property
    def max_concurrency(self) -> int:
        """Return configured concurrent provider-call limit (default 3)."""
        return self._max_concurrency

    def register_provider(self, name: str, builder: ProviderModelBuilder) -> None:
        """Register an additional model provider builder dynamically."""
        cleaned = name.strip().lower()
        if not cleaned:
            raise ValueError("Provider name must not be empty.")
        self._providers[cleaned] = builder
        self._chat_model_cache.clear()

    def resolve_model_name(self, role: LLMRole) -> str:
        """Return the configured model name for `role`."""
        if role == "critic":
            return self._critic_model
        return self._auditor_model

    def build_chat_model(self, role: LLMRole) -> BaseChatModel:
        """Instantiate the LangChain BaseChatModel configured for `role`."""
        model_name = self.resolve_model_name(role)
        if self._chat_model_builder is not None:
            return self._chat_model_builder(role, model_name)
        builder = self._providers.get(self._provider)
        if builder is None:
            raise LLMConfigurationError(
                f"Unsupported LLM provider '{self._provider}'. "
                f"Registered providers: {sorted(self._providers)}."
            )
        return builder(
            model_name,
            self._settings,
            self._api_key_override,
            self._request_timeout_seconds,
            self._max_output_tokens,
        )

    def _get_or_build_chat_model(self, role: LLMRole) -> BaseChatModel:
        model_name = self.resolve_model_name(role)
        cache_key = (role, model_name)
        cached = self._chat_model_cache.get(cache_key)
        if cached is None:
            cached = self.build_chat_model(role)
            self._chat_model_cache[cache_key] = cached
        return cached

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._explicit_semaphore is not None:
            return self._explicit_semaphore
        loop = asyncio.get_running_loop()
        per_limit = _SHARED_LOOP_SEMAPHORES.get(loop)
        if per_limit is None:
            per_limit = {}
            _SHARED_LOOP_SEMAPHORES[loop] = per_limit
        sem = per_limit.get(self._max_concurrency)
        if sem is None:
            sem = asyncio.Semaphore(self._max_concurrency)
            per_limit[self._max_concurrency] = sem
        return sem

    def _compute_backoff_delay(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
    ) -> float:
        if retry_after is not None:
            jittered = max(0.5, retry_after) + self._rng.uniform(
                0.0, _RETRY_AFTER_MAX_JITTER_SECONDS
            )
            return min(self._retry_cap_seconds, jittered)
        ceiling = min(self._retry_cap_seconds, self._retry_base_seconds * float(2**attempt))
        return self._rng.uniform(0.0, ceiling)

    @staticmethod
    def _raise_retry_exhausted(
        exc: BaseException,
        classification: _ErrorClassification,
        attempts: int,
    ) -> NoReturn:
        if classification.is_rate_limit:
            raise RateLimitTaskError(
                f"LLM rate limit exhausted after {attempts} attempts.",
                retry_after_seconds=classification.retry_after_seconds,
            ) from exc
        raise RetryableTaskError(
            f"LLM transient failure ({type(exc).__name__}) exhausted after {attempts} attempts.",
            retry_after_seconds=classification.retry_after_seconds,
        ) from exc

    async def _invoke_once(
        self,
        *,
        role: LLMRole,
        model_name: str,
        schema: type[T],
        messages: list[BaseMessage],
        config: RunnableConfig | None,
        attempt_number: int,
    ) -> StructuredInvocationResult[T]:
        started = time.perf_counter()

        if self._structured_invoker is not None:
            raw_result = await self._structured_invoker(
                role,
                model_name,
                schema,
                messages,
                config,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            if isinstance(raw_result, StructuredInvocationResult):
                return StructuredInvocationResult(
                    parsed=raw_result.parsed,
                    model_name=raw_result.model_name,
                    input_tokens=raw_result.input_tokens,
                    output_tokens=raw_result.output_tokens,
                    total_tokens=raw_result.total_tokens,
                    estimated_cost_usd=raw_result.estimated_cost_usd,
                    latency_ms=raw_result.latency_ms,
                    attempts=attempt_number,
                )
            if not isinstance(raw_result, schema):
                raise LLMStructuredOutputError(
                    f"Structured invoker returned {type(raw_result).__name__}, "
                    f"expected {schema.__name__}."
                )
            return StructuredInvocationResult(
                parsed=raw_result,
                model_name=model_name,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                estimated_cost_usd=None,
                latency_ms=elapsed_ms,
                attempts=attempt_number,
            )

        chat_model = self._get_or_build_chat_model(role)
        structured_runnable = chat_model.with_structured_output(schema, include_raw=True)
        response = await structured_runnable.ainvoke(messages, config=config)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

        if not isinstance(response, Mapping):
            raise LLMStructuredOutputError(
                f"Structured output runnable returned unexpected {type(response).__name__}."
            )

        parsing_error = response.get("parsing_error")
        parsed = response.get("parsed")
        raw_message = response.get("raw")
        if parsing_error is not None or not isinstance(parsed, schema):
            err_kind = type(parsing_error).__name__ if parsing_error is not None else "MissingValue"
            raise LLMStructuredOutputError(
                f"Structured output validation failed for {schema.__name__} ({err_kind})."
            )

        in_tok, out_tok, total_tok = _extract_usage_from_message(raw_message)
        cost_breakdown = estimate_model_cost_usd(
            model_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        return StructuredInvocationResult(
            parsed=parsed,
            model_name=model_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=total_tok,
            estimated_cost_usd=cost_breakdown["total"] if cost_breakdown is not None else None,
            latency_ms=elapsed_ms,
            attempts=attempt_number,
        )

    async def invoke_structured(
        self,
        *,
        role: LLMRole,
        schema: type[T],
        messages: Sequence[BaseMessage],
        config: RunnableConfig | None = None,
    ) -> StructuredInvocationResult[T]:
        """Invoke an LLM structured output call bounded by Semaphore(3) and 429 retry backoff."""
        model_name = self.resolve_model_name(role)
        msg_list = list(messages)
        semaphore = self._get_semaphore()
        deadline = (
            self._clock_fn() + self._max_total_wait_seconds
            if self._max_total_wait_seconds is not None
            else None
        )

        for attempt in range(self._max_retries + 1):
            try:
                async with semaphore:
                    return await self._invoke_once(
                        role=role,
                        model_name=model_name,
                        schema=schema,
                        messages=msg_list,
                        config=config,
                        attempt_number=attempt + 1,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                classification = classify_llm_exception(exc)
                if not classification.retryable:
                    raise
                if attempt >= self._max_retries:
                    self._raise_retry_exhausted(exc, classification, attempt + 1)

                remaining = (deadline - self._clock_fn()) if deadline is not None else None
                hint = classification.retry_after_seconds
                if hint is not None:
                    if hint > self._retry_cap_seconds or (
                        remaining is not None and hint > remaining
                    ):
                        self._raise_retry_exhausted(exc, classification, attempt + 1)
                    delay = self._compute_backoff_delay(attempt, retry_after=hint)
                    if remaining is not None:
                        delay = min(delay, remaining)
                else:
                    if remaining is not None and remaining <= 0.0:
                        self._raise_retry_exhausted(exc, classification, attempt + 1)
                    delay = self._compute_backoff_delay(attempt)
                    if remaining is not None:
                        delay = min(delay, remaining)

                logger.warning(
                    "llm_factory_retry",
                    trace_id=get_trace_id() or "-",
                    role=role,
                    model=model_name,
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    status_code=classification.status_code,
                    is_rate_limit=classification.is_rate_limit,
                    delay_seconds=round(delay, 3),
                    error_type=type(exc).__name__,
                )
                # Semaphore permit is ALREADY released before sleeping in backoff.
                await self._sleep_fn(delay)

        raise RetryableTaskError("LLM invocation exhausted retries unexpectedly.")
