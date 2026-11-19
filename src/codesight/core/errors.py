"""Domain retryable error types and HTTP Retry-After header parsing."""

from __future__ import annotations

import email.utils
import math
import time
from collections.abc import Mapping
from datetime import UTC
from typing import Final

MIN_RETRY_AFTER_SECONDS: Final[float] = 1.0
MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0


class RetryableTaskError(RuntimeError):
    """Raised on transient network, broker, or upstream 5xx errors eligible for Celery retry."""


class RateLimitTaskError(RetryableTaskError):
    """Raised on upstream HTTP 429 or secondary rate-limit responses."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _clamp_delay(seconds: float) -> float:
    return min(MAX_RETRY_AFTER_SECONDS, max(MIN_RETRY_AFTER_SECONDS, seconds))


def is_rate_limited_403(headers: Mapping[str, str]) -> bool:
    """Return True when HTTP 403 response headers signal primary/secondary rate limiting."""
    lowered = {k.lower(): v.strip() for k, v in headers.items() if isinstance(v, str)}
    return bool(lowered.get("retry-after") or lowered.get("x-ratelimit-remaining") == "0")


def parse_retry_after_header(
    headers: Mapping[str, str],
    *,
    now: float | None = None,
) -> float | None:
    """Extract and clamp retry delay [1.0, 60.0] from Retry-After or X-RateLimit-Reset headers."""
    lowered = {k.lower(): v.strip() for k, v in headers.items() if isinstance(v, str)}
    active_now = time.time() if now is None else now

    raw_retry_after = lowered.get("retry-after")
    if raw_retry_after:
        try:
            numeric_delay = float(raw_retry_after)
        except ValueError:
            numeric_delay = math.nan
        if math.isfinite(numeric_delay):
            return _clamp_delay(numeric_delay)

        try:
            parsed_dt = email.utils.parsedate_to_datetime(raw_retry_after)
        except (TypeError, ValueError):
            parsed_dt = None
        if parsed_dt is not None:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=UTC)
            return _clamp_delay(parsed_dt.timestamp() - active_now)

    raw_reset = lowered.get("x-ratelimit-reset")
    if raw_reset:
        try:
            reset_epoch = float(raw_reset)
        except ValueError:
            reset_epoch = math.nan
        if math.isfinite(reset_epoch):
            return _clamp_delay(reset_epoch - active_now)

    return None
