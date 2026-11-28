"""Shared test helpers for CodeSight-AI unit and integration suites."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class RecordingSpanExporter(SpanExporter):
    """Thread-safe in-memory SpanExporter detecting premature shutdowns."""

    def __init__(self, *, fail_after_shutdown: bool = False) -> None:
        self._lock = threading.Lock()
        self.spans: list[ReadableSpan] = []
        self.shutdown_called = False
        self.fail_after_shutdown = fail_after_shutdown

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            if self.shutdown_called and self.fail_after_shutdown:
                return SpanExportResult.FAILURE
            self.spans.extend(spans)
            return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        _ = timeout_millis
        return True

    def shutdown(self) -> None:
        with self._lock:
            self.shutdown_called = True


def sha1_blob(content: bytes) -> str:
    """Compute Git blob SHA-1 digest."""
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
    ).hexdigest()


def b64_encode(content: bytes) -> str:
    """Encode bytes as ASCII base64 string."""
    return base64.b64encode(content).decode("ascii")


def make_alembic_config(dsn: str, repo_root: Path) -> Config:
    """Create isolated Alembic Config bound to `dsn` without touching global loggers."""
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def generate_ephemeral_rsa_pair() -> tuple[str, Any]:
    """Generate an ephemeral 2048-bit RSA keypair returning (base64_pkcs8_pem, public_key)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem_bytes).decode("ascii"), private_key.public_key()


def sign_github_body(raw_body: bytes, secret: str) -> str:
    """Compute GitHub X-Hub-Signature-256 header value."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class RecordingCelery:
    """In-memory Celery stand-in capturing dispatched tasks and revocations."""

    def __init__(self, *, task_prefix: str = "task") -> None:
        self._task_prefix = task_prefix
        self.dispatched: list[dict[str, Any]] = []
        self.revoked: list[tuple[str, bool]] = []
        self.control = SimpleNamespace(
            revoke=lambda tid, terminate=False: self.revoked.append((tid, terminate))
        )

    def send_task(
        self,
        name: str,
        *,
        args: tuple[Any, ...] = (),
        countdown: int = 0,
        **_: Any,
    ) -> SimpleNamespace:
        task_id = f"{self._task_prefix}-{len(self.dispatched) + 1}"
        self.dispatched.append(
            {"task_id": task_id, "name": name, "payload": dict(args[0]), "countdown": countdown}
        )
        return SimpleNamespace(id=task_id)
