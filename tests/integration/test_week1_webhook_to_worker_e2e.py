"""Live PostgreSQL (5433) & Redis (6381) E2E integration tests for Week 1 Milestone 1."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from codesight.api.main import create_app
from codesight.api.routes.webhooks import CeleryWebhookDispatcher
from codesight.core.config import Settings
from codesight.core.logging import configure_logging
from codesight.db.models import PRReview, Repository, WebhookEvent
from codesight.db.session import create_session_factory
from codesight.worker.celery_app import run_pr_webhook_task

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_WEBHOOK_SECRET = "e2e-webhook-secret-ca10"


def _make_alembic_config(dsn: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _generate_ephemeral_rsa_pair() -> tuple[str, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b64_pem = base64.b64encode(pem_bytes).decode("ascii")
    return b64_pem, private_key.public_key()


def _sign_body(raw_body: bytes, secret: str = E2E_WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_pr_webhook_body(
    *,
    action: str,
    pr_number: int,
    head_sha: str,
    before_sha: str | None = None,
    installation_id: int = 555,
    repo_id: int = 42010,
    repo_full_name: str = "Tieens-04/CodeSight-AI",
    updated_at: str = "2026-11-21T10:00:00Z",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action": action,
        "repository": {
            "id": repo_id,
            "full_name": repo_full_name,
            "default_branch": "main",
        },
        "installation": {"id": installation_id},
        "pull_request": {
            "number": pr_number,
            "updated_at": updated_at,
            "head": {"sha": head_sha},
            "base": {"sha": "0000base0000"},
        },
    }
    if before_sha is not None:
        body["before"] = before_sha
    return body


class _RecordingControl:
    def __init__(self, revoked: list[tuple[str, bool]]) -> None:
        self._revoked = revoked

    def revoke(self, task_id: str, *, terminate: bool = False) -> None:
        self._revoked.append((task_id, terminate))


class _RecordingCelery:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.revoked: list[tuple[str, bool]] = []
        self.control = _RecordingControl(self.revoked)

    def send_task(
        self,
        name: str,
        *,
        args: tuple[Any, ...] = (),
        countdown: int = 0,
        **_: Any,
    ) -> SimpleNamespace:
        task_id = f"e2e-task-{len(self.dispatched) + 1}"
        self.dispatched.append(
            {
                "task_id": task_id,
                "name": name,
                "payload": dict(args[0]),
                "countdown": countdown,
            }
        )
        return SimpleNamespace(id=task_id)


@contextlib.contextmanager
def _pause_coverage_tracer() -> Iterator[None]:
    try:
        import coverage
    except ImportError:
        yield
        return

    active_cov = coverage.Coverage.current()
    if active_cov is None:
        yield
        return

    active_cov.stop()
    try:
        yield
    finally:
        active_cov.start()


async def _cleanup_redis_keys(
    redis_client: redis_async.Redis,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    installation_id: int | None,
) -> None:
    if installation_id is not None:
        await redis_client.delete(f"gh_inst_token:{installation_id}")
    async with session_factory() as session:
        repo_ids = (await session.execute(sa.select(Repository.id))).scalars().all()
    for repo_id in repo_ids:
        keys = [str(k) async for k in redis_client.scan_iter(match=f"pr_active*:{repo_id}:*")]
        if keys:
            await redis_client.delete(*keys)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week1_e2e_webhook_ack_latency_under_100ms_and_worker_publishes_zero_422_review(
    isolated_postgres_dsn: str,
    guard_validator: Any,
) -> None:
    command.upgrade(_make_alembic_config(isolated_postgres_dsn), "head")

    b64_pem, pub_key = _generate_ephemeral_rsa_pair()
    inst_id = 91000 + int(uuid.uuid4().hex[:4], 16) % 9000
    base_settings = Settings()
    settings = base_settings.model_copy(
        update={
            "github_app_id": "10010",
            "github_private_key": SecretStr(b64_pem),
            "github_webhook_secret": SecretStr(E2E_WEBHOOK_SECRET),
            "github_token": None,
            "week1_smoke_inline_comment": True,
        }
    )

    engine = create_async_engine(isolated_postgres_dsn, pool_size=5, pool_pre_ping=True)
    session_factory = create_session_factory(engine)
    redis_client = redis_async.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=1.5,
    )
    recorder = _RecordingCelery()
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        webhook_dispatcher=CeleryWebhookDispatcher(app=recorder, redis_client=redis_client),
    )

    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1 +1,3 @@",
            ' "name": "demo",',
            '+ "depA": "1.0.0",',
            '+ "depB": "2.0.0"',
            "diff --git a/generated/out.py b/generated/out.py",
            "--- a/generated/out.py",
            "+++ b/generated/out.py",
            "@@ -1 +1 @@",
            "-GENERATED = 1",
            "+GENERATED = 2",
            "diff --git a/src/payment/service.py b/src/payment/service.py",
            "--- a/src/payment/service.py",
            "+++ b/src/payment/service.py",
            "@@ -10 +10 @@",
            "-flag = False",
            "\\ No newline at end of file",
            "+flag = True",
            "@@ -20,2 +20,3 @@ def process_payment(user):",
            "     audit(user)",
            "+    token = 'RAW_CUSTOMER_SECRET_CODE_E2E'",
            "     return True",
        ]
    )
    yaml_b64 = base64.b64encode(b"min_debt_score: 75\nignore_paths:\n  - generated/**\n").decode(
        "ascii"
    )

    token_calls = 0
    check_runs_created: list[dict[str, Any]] = []
    check_runs_completed: list[dict[str, Any]] = []
    reviews_posted: list[dict[str, Any]] = []

    def gh_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.url.path == f"/app/installations/{inst_id}/access_tokens":
            token_calls += 1
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            claims = jwt.decode(bearer, pub_key, algorithms=["RS256"])
            assert claims["iss"] == "10010"
            return httpx.Response(201, json={"token": "ghs_e2e_inst_token_xyz"})

        assert request.headers.get("authorization") == "Bearer ghs_e2e_inst_token_xyz"
        if request.url.path == "/repos/Tieens-04/CodeSight-AI/contents/.codesight.yaml":
            return httpx.Response(
                200,
                json={"type": "file", "size": 64, "encoding": "base64", "content": yaml_b64},
            )
        if (
            request.url.path == "/repos/Tieens-04/CodeSight-AI/check-runs"
            and request.method == "POST"
        ):
            check_runs_created.append(body)
            return httpx.Response(
                201, json={"id": 9101, "name": body["name"], "status": "in_progress"}
            )
        if request.url.path == "/repos/Tieens-04/CodeSight-AI/pulls/12" and request.method == "GET":
            return httpx.Response(200, text=raw_diff)
        if (
            request.url.path == "/repos/Tieens-04/CodeSight-AI/pulls/12/reviews"
            and request.method == "POST"
        ):
            reviews_posted.append(body)
            return httpx.Response(
                200,
                json={
                    "id": 7101,
                    "state": "COMMENTED",
                    "html_url": "https://github.com/Tieens-04/CodeSight-AI/pull/12#r",
                },
            )
        if (
            request.url.path == "/repos/Tieens-04/CodeSight-AI/check-runs/9101"
            and request.method == "PATCH"
        ):
            check_runs_completed.append(body)
            return httpx.Response(
                200,
                json={"id": 9101, "status": "completed", "conclusion": body["conclusion"]},
            )
        return httpx.Response(404)

    log_stream = io.StringIO()
    configure_logging(level="INFO", service_name="e2e-test", stream=log_stream)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api_client:
            warm_raw = json.dumps(
                _build_pr_webhook_body(
                    action="opened",
                    pr_number=1,
                    head_sha="warm0000",
                    installation_id=inst_id,
                )
            ).encode("utf-8")
            warm_resp = await api_client.post(
                "/api/v1/webhooks/github",
                content=warm_raw,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-e2e-warm",
                    "X-Hub-Signature-256": _sign_body(warm_raw),
                },
            )
            assert warm_resp.status_code == 202

            delivery_id = f"deliv-e2e-{uuid.uuid4().hex[:8]}"
            pr_raw = json.dumps(
                _build_pr_webhook_body(
                    action="opened",
                    pr_number=12,
                    head_sha="sha12opened0001",
                    installation_id=inst_id,
                )
            ).encode("utf-8")
            with _pause_coverage_tracer():
                t0 = time.perf_counter()
                resp = await api_client.post(
                    "/api/v1/webhooks/github",
                    content=pr_raw,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": delivery_id,
                        "X-Hub-Signature-256": _sign_body(pr_raw),
                    },
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0

            assert resp.status_code == 202
            assert resp.headers["x-trace-id"] == delivery_id
            assert resp.json()["status"] == "queued"
            assert elapsed_ms < 100.0, f"Warm webhook ACK took {elapsed_ms:.2f}ms (>= 100ms)"

        dispatched_entry = recorder.dispatched[-1]
        assert dispatched_entry["name"] == "codesight.webhook.handle_pr_event"
        assert dispatched_entry["countdown"] == 0

        async with httpx.AsyncClient(transport=httpx.MockTransport(gh_handler)) as gh_client:
            worker_res = await run_pr_webhook_task(
                dispatched_entry["payload"],
                settings=settings,
                http_client=gh_client,
                session_factory=session_factory,
                redis_client=redis_client,
            )

        assert worker_res == {"status": "completed", "trace_id": delivery_id}
        assert token_calls == 1
        assert await redis_client.get(f"gh_inst_token:{inst_id}") == "ghs_e2e_inst_token_xyz"
        assert len(check_runs_created) == 1
        assert check_runs_created[0]["external_id"] == delivery_id
        assert len(check_runs_completed) == 1
        assert check_runs_completed[0]["conclusion"] == "success"

        assert len(reviews_posted) == 1
        review_payload = reviews_posted[0]
        assert review_payload["event"] == "COMMENT"
        assert len(review_payload["comments"]) == 1
        inline_comment = review_payload["comments"][0]
        assert inline_comment["path"] == "src/payment/service.py"
        assert inline_comment["side"] == "RIGHT"
        assert "start_line" not in inline_comment
        assert "package-lock.json" in review_payload["body"]
        assert "generated/out.py" in review_payload["body"]

        oracle_map = guard_validator.map_right_hunk_lines(raw_diff)
        assert (
            guard_validator.validate_review_comments(oracle_map, review_payload["comments"]) == []
        )

        async with session_factory() as verify_session:
            evt_row = await verify_session.get(WebhookEvent, delivery_id)
            assert evt_row is not None and evt_row.status == "processed"
            pr_review_row = (
                await verify_session.execute(
                    sa.select(PRReview).where(PRReview.trace_id == delivery_id)
                )
            ).scalar_one()
            assert pr_review_row.status == "completed"
            assert pr_review_row.info_count == 1
            assert pr_review_row.tokens_saved_pct is not None and pr_review_row.tokens_saved_pct > 0
            assert pr_review_row.latency_ms is not None and pr_review_row.latency_ms >= 0

        logs_text = log_stream.getvalue()
        assert "RAW_CUSTOMER_SECRET_CODE_E2E" not in logs_text
        assert "ghs_e2e_inst_token_xyz" not in logs_text
        assert "RAW_CUSTOMER_SECRET_CODE_E2E" not in review_payload["body"]
        assert "RAW_CUSTOMER_SECRET_CODE_E2E" not in inline_comment["body"]
    finally:
        configure_logging(level=base_settings.log_level)
        await _cleanup_redis_keys(
            redis_client, session_factory=session_factory, installation_id=inst_id
        )
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week1_e2e_rapid_two_commits_cancel_on_supersede_queued_and_midflight(
    isolated_postgres_dsn: str,
) -> None:
    command.upgrade(_make_alembic_config(isolated_postgres_dsn), "head")

    b64_pem, _ = _generate_ephemeral_rsa_pair()
    inst_id = 92000 + int(uuid.uuid4().hex[:4], 16) % 8000
    settings = Settings().model_copy(
        update={
            "github_app_id": "10020",
            "github_private_key": SecretStr(b64_pem),
            "github_webhook_secret": SecretStr(E2E_WEBHOOK_SECRET),
            "github_token": None,
            "week1_smoke_inline_comment": True,
        }
    )

    engine = create_async_engine(isolated_postgres_dsn, pool_pre_ping=True)
    session_factory = create_session_factory(engine)
    redis_client = redis_async.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=1.5,
    )
    recorder = _RecordingCelery()
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        webhook_dispatcher=CeleryWebhookDispatcher(app=recorder, redis_client=redis_client),
    )

    sample_diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-v = 1\n"
        "+v = 2\n"
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api_client:

            async def _send_pr(
                action: str, pr_number: int, head_sha: str, before_sha: str | None = None
            ) -> str:
                deliv = f"deliv-{pr_number}-{head_sha}"
                raw = json.dumps(
                    _build_pr_webhook_body(
                        action=action,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        before_sha=before_sha,
                        installation_id=inst_id,
                    )
                ).encode("utf-8")
                resp = await api_client.post(
                    "/api/v1/webhooks/github",
                    content=raw,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": deliv,
                        "X-Hub-Signature-256": _sign_body(raw),
                    },
                )
                assert resp.status_code == 202
                return deliv

            # Sub-case A: Queued supersede before SHA_1 starts
            await _send_pr("opened", 20, "sha20_commit1")
            await _send_pr("synchronize", 20, "sha20_commit2", before_sha="sha20_commit1")
            assert recorder.dispatched[0]["countdown"] == 0
            assert recorder.dispatched[1]["countdown"] == 5
            assert recorder.revoked == [(recorder.dispatched[0]["task_id"], False)]

            gh_calls_a: list[tuple[str, str, dict[str, Any]]] = []

            async def gh_handler_a(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                gh_calls_a.append((request.method, request.url.path, body))
                if request.url.path == f"/app/installations/{inst_id}/access_tokens":
                    return httpx.Response(201, json={"token": "ghs_supersede_token"})
                if request.url.path.endswith(
                    "/contents/.codesight.yaml"
                ) or request.url.path.endswith("/contents/.codesight.yml"):
                    return httpx.Response(404)
                if request.url.path.endswith("/check-runs") and request.method == "POST":
                    return httpx.Response(201, json={"id": 6101, "status": "in_progress"})
                if request.url.path.endswith("/pulls/20") and request.method == "GET":
                    return httpx.Response(200, text=sample_diff)
                if request.url.path.endswith("/pulls/20/reviews") and request.method == "POST":
                    return httpx.Response(200, json={"id": 7201, "state": "COMMENTED"})
                if "/check-runs/" in request.url.path and request.method == "PATCH":
                    return httpx.Response(200, json={"id": 6101, "status": "completed"})
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(gh_handler_a)) as gh_client:
                res_a1 = await run_pr_webhook_task(
                    recorder.dispatched[0]["payload"],
                    settings=settings,
                    http_client=gh_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                )
                assert res_a1["status"] == "superseded"
                assert gh_calls_a == []

                res_a2 = await run_pr_webhook_task(
                    recorder.dispatched[1]["payload"],
                    settings=settings,
                    http_client=gh_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                )
                assert res_a2["status"] == "completed"
                assert any(c[1].endswith("/pulls/20/reviews") for c in gh_calls_a)

            # Sub-case B: Mid-flight supersede while SHA_A is fetching diff
            await _send_pr("opened", 21, "sha21_commitA")
            task_a_payload = recorder.dispatched[-1]["payload"]
            midflight_triggered = False
            check_run_counter = 6200
            gh_calls_b: list[tuple[str, str, dict[str, Any]]] = []

            async def gh_handler_b(request: httpx.Request) -> httpx.Response:
                nonlocal midflight_triggered, check_run_counter
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                gh_calls_b.append((request.method, request.url.path, body))
                if request.url.path.endswith(
                    "/contents/.codesight.yaml"
                ) or request.url.path.endswith("/contents/.codesight.yml"):
                    return httpx.Response(404)
                if request.url.path.endswith("/check-runs") and request.method == "POST":
                    check_run_counter += 1
                    return httpx.Response(
                        201, json={"id": check_run_counter, "status": "in_progress"}
                    )
                if request.url.path.endswith("/pulls/21") and request.method == "GET":
                    if not midflight_triggered:
                        midflight_triggered = True
                        await _send_pr(
                            "synchronize",
                            21,
                            "sha21_commitB",
                            before_sha="sha21_commitA",
                        )
                    return httpx.Response(200, text=sample_diff)
                if request.url.path.endswith("/pulls/21/reviews") and request.method == "POST":
                    return httpx.Response(
                        200, json={"id": 7301, "state": "COMMENTED", "commit_id": body["commit_id"]}
                    )
                if "/check-runs/" in request.url.path and request.method == "PATCH":
                    return httpx.Response(
                        200, json={"id": check_run_counter, "status": "completed"}
                    )
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(gh_handler_b)) as gh_client:
                res_b1 = await run_pr_webhook_task(
                    task_a_payload,
                    settings=settings,
                    http_client=gh_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                )
                assert res_b1["status"] == "superseded"
                assert not any(c[1].endswith("/pulls/21/reviews") for c in gh_calls_b)
                patches_after_a = [c for c in gh_calls_b if c[0] == "PATCH"]
                assert len(patches_after_a) == 1
                assert patches_after_a[0][2]["conclusion"] == "neutral"

                task_b_payload = recorder.dispatched[-1]["payload"]
                assert task_b_payload["head_sha"] == "sha21_commitB"
                res_b2 = await run_pr_webhook_task(
                    task_b_payload,
                    settings=settings,
                    http_client=gh_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                )
                assert res_b2["status"] == "completed"
                reviews_b = [c for c in gh_calls_b if c[1].endswith("/pulls/21/reviews")]
                assert len(reviews_b) == 1
                assert reviews_b[0][2]["commit_id"] == "sha21_commitB"
                patches_after_b = [c for c in gh_calls_b if c[0] == "PATCH"]
                assert len(patches_after_b) == 2
                assert patches_after_b[1][2]["conclusion"] == "success"

        async with session_factory() as verify_session:
            b_rows = (
                (
                    await verify_session.execute(
                        sa.select(PRReview)
                        .where(PRReview.pr_number == 21)
                        .order_by(PRReview.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [(r.head_sha, r.status) for r in b_rows] == [
                ("sha21_commitA", "superseded"),
                ("sha21_commitB", "completed"),
            ]
    finally:
        await _cleanup_redis_keys(
            redis_client, session_factory=session_factory, installation_id=inst_id
        )
        await redis_client.aclose()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week1_e2e_duplicate_delivery_files_api_fallback_and_noise_only_pr(
    isolated_postgres_dsn: str,
) -> None:
    command.upgrade(_make_alembic_config(isolated_postgres_dsn), "head")

    b64_pem, _ = _generate_ephemeral_rsa_pair()
    inst_id = 93000 + int(uuid.uuid4().hex[:4], 16) % 7000
    settings = Settings().model_copy(
        update={
            "github_app_id": "10030",
            "github_private_key": SecretStr(b64_pem),
            "github_webhook_secret": SecretStr(E2E_WEBHOOK_SECRET),
            "github_token": None,
            "week1_smoke_inline_comment": True,
        }
    )

    engine = create_async_engine(isolated_postgres_dsn, pool_pre_ping=True)
    session_factory = create_session_factory(engine)
    redis_client = redis_async.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=1.5,
    )
    recorder = _RecordingCelery()
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        webhook_dispatcher=CeleryWebhookDispatcher(app=recorder, redis_client=redis_client),
    )

    reviews_posted: list[tuple[str, dict[str, Any]]] = []
    check_conclusions: list[str] = []

    def gh_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.url.path == f"/app/installations/{inst_id}/access_tokens":
            return httpx.Response(201, json={"token": "ghs_fallback_token"})
        if request.url.path.endswith("/contents/.codesight.yaml") or request.url.path.endswith(
            "/contents/.codesight.yml"
        ):
            return httpx.Response(404)
        if request.url.path.endswith("/check-runs") and request.method == "POST":
            return httpx.Response(201, json={"id": 8101, "status": "in_progress"})
        if request.url.path == "/repos/Tieens-04/CodeSight-AI/pulls/31" and request.method == "GET":
            return httpx.Response(406, json={"message": "Diff exceeded 300 files"})
        if (
            request.url.path == "/repos/Tieens-04/CodeSight-AI/pulls/31/files"
            and request.method == "GET"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/handler.py",
                        "status": "modified",
                        "patch": "@@ -1 +1,2 @@\n def ok():\n+    return True\n",
                    }
                ],
            )
        if request.url.path == "/repos/Tieens-04/CodeSight-AI/pulls/32" and request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/package-lock.json b/package-lock.json\n"
                    "--- a/package-lock.json\n"
                    "+++ b/package-lock.json\n"
                    "@@ -1 +1 @@\n"
                    '-"v": 1\n'
                    '+"v": 2\n'
                ),
            )
        if request.url.path.endswith("/reviews") and request.method == "POST":
            reviews_posted.append((request.url.path, body))
            return httpx.Response(200, json={"id": 8201, "state": "COMMENTED"})
        if "/check-runs/" in request.url.path and request.method == "PATCH":
            check_conclusions.append(str(body["conclusion"]))
            return httpx.Response(200, json={"id": 8101, "status": "completed"})
        return httpx.Response(404)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api_client:
            raw_31 = json.dumps(
                _build_pr_webhook_body(
                    action="opened",
                    pr_number=31,
                    head_sha="sha31_files_fallback",
                    installation_id=inst_id,
                )
            ).encode("utf-8")
            hdrs_31 = {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-e2e-31",
                "X-Hub-Signature-256": _sign_body(raw_31),
            }
            r1 = await api_client.post("/api/v1/webhooks/github", content=raw_31, headers=hdrs_31)
            r_dup = await api_client.post(
                "/api/v1/webhooks/github", content=raw_31, headers=hdrs_31
            )
            assert r1.status_code == 202
            assert r_dup.status_code == 200 and r_dup.json()["status"] == "duplicate"
            assert len(recorder.dispatched) == 1

            raw_32 = json.dumps(
                _build_pr_webhook_body(
                    action="opened",
                    pr_number=32,
                    head_sha="sha32_noise_only",
                    installation_id=inst_id,
                )
            ).encode("utf-8")
            r32 = await api_client.post(
                "/api/v1/webhooks/github",
                content=raw_32,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-e2e-32",
                    "X-Hub-Signature-256": _sign_body(raw_32),
                },
            )
            assert r32.status_code == 202
            assert len(recorder.dispatched) == 2

        async with httpx.AsyncClient(transport=httpx.MockTransport(gh_handler)) as gh_client:
            res_31 = await run_pr_webhook_task(
                recorder.dispatched[0]["payload"],
                settings=settings,
                http_client=gh_client,
                session_factory=session_factory,
                redis_client=redis_client,
            )
            res_32 = await run_pr_webhook_task(
                recorder.dispatched[1]["payload"],
                settings=settings,
                http_client=gh_client,
                session_factory=session_factory,
                redis_client=redis_client,
            )

        assert res_31["status"] == "completed"
        assert res_32["status"] == "completed"
        assert check_conclusions == ["success", "success"]
        assert len(reviews_posted) == 2
        assert len(reviews_posted[0][1]["comments"]) == 1
        assert "Diff Source:** `files_api`" in reviews_posted[0][1]["body"]
        assert reviews_posted[1][1]["comments"] == []
        assert "package-lock.json" in reviews_posted[1][1]["body"]

        async with session_factory() as verify_session:
            row_32 = (
                await verify_session.execute(sa.select(PRReview).where(PRReview.pr_number == 32))
            ).scalar_one()
            assert row_32.status == "completed"
            assert row_32.info_count == 0
    finally:
        await _cleanup_redis_keys(
            redis_client, session_factory=session_factory, installation_id=inst_id
        )
        await redis_client.aclose()
        await engine.dispose()
