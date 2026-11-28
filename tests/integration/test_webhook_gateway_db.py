"""PostgreSQL-backed transactional and latency integration tests for GitHub Webhook Gateway."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import statistics
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from codesight.api.main import create_app
from codesight.api.routes.webhooks import (
    CeleryWebhookDispatcher,
    PRWebhookDispatchPayload,
    PushWebhookDispatchPayload,
    _upsert_repository,
)
from codesight.core.config import Settings
from codesight.core.logging import bind_trace_id, clear_trace_context, configure_logging
from codesight.db.models import Base, Repository, WebhookEvent
from codesight.db.session import create_session_factory
from codesight.worker.celery_app import (
    create_celery_app,
    handle_pr_event,
    handle_push_event,
    is_pr_event_superseded,
)

TEST_WEBHOOK_SECRET = "super-secret-webhook-key-ca6"


def _sign_body(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _SpyDispatcher:
    def __init__(self, *, fail_next: bool = False) -> None:
        self.pr_calls: list[PRWebhookDispatchPayload] = []
        self.push_calls: list[PushWebhookDispatchPayload] = []
        self.fail_next = fail_next

    async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("redis broker temporarily down")
        self.pr_calls.append(payload)
        return f"task-pr-{len(self.pr_calls)}"

    async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("redis broker temporarily down")
        self.push_calls.append(payload)
        return f"task-push-{len(self.push_calls)}"


def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    head_sha: str = "a1b2c3d4e5f6",
    repo_id: int | None = 9001,
    full_name: str = "Tieens-04/CodeSight-AI",
    default_branch: str = "main",
    installation_id: int | None = 555,
) -> dict[str, Any]:
    repo_obj: dict[str, Any] = {
        "full_name": full_name,
        "default_branch": default_branch,
    }
    if repo_id is not None:
        repo_obj["id"] = repo_id
    body: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha},
            "base": {"sha": "0000base"},
        },
        "repository": repo_obj,
    }
    if installation_id is not None:
        body["installation"] = {"id": installation_id}
    return body


def _push_payload(
    *,
    ref: str = "refs/heads/main",
    after: str = "deadbeef1234",
    before: str = "111122223333",
    deleted: bool = False,
    repo_id: int = 9001,
    full_name: str = "Tieens-04/CodeSight-AI",
    default_branch: str = "main",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "before": before,
        "after": after,
        "deleted": deleted,
        "repository": {
            "id": repo_id,
            "full_name": full_name,
            "default_branch": default_branch,
        },
        "installation": {"id": 555},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_idempotency_filtering_cli_upsert_and_broker_rollback(
    isolated_postgres_dsn: str,
) -> None:
    settings = Settings(
        _env_file=None,
        github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
    )

    test_engine = create_async_engine(isolated_postgres_dsn)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(test_engine)

    # Pre-seed a CLI-registered Repository without github_repo_id
    async with session_factory() as seed_session:
        seed_session.add(
            Repository(
                full_name="Tieens-04/CodeSight-AI",
                github_repo_id=None,
                default_branch="main",
            )
        )
        await seed_session.commit()

    spy = _SpyDispatcher()
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        webhook_dispatcher=spy,
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:

            async def _send(event: str, delivery: str, body_obj: dict[str, Any]) -> httpx.Response:
                raw = json.dumps(body_obj).encode("utf-8")
                return await client.post(
                    "/api/v1/webhooks/github",
                    content=raw,
                    headers={
                        "X-GitHub-Event": event,
                        "X-GitHub-Delivery": delivery,
                        "X-Hub-Signature-256": _sign_body(raw),
                    },
                )

            # 1. ping -> 200 pong, 0 dispatches; duplicate ping -> 200 duplicate
            r_ping = await _send("ping", "deliv-ping-1", {"zen": "Keep it simple"})
            assert r_ping.status_code == 200
            assert r_ping.json()["status"] == "pong"
            r_ping_dup = await _send("ping", "deliv-ping-1", {"zen": "Keep it simple"})
            assert r_ping_dup.status_code == 200
            assert r_ping_dup.json()["status"] == "duplicate"

            # 2. pull_request.opened -> 202 queued, backfills CLI repo's github_repo_id + inst_id
            r_pr = await _send(
                "pull_request",
                "deliv-pr-1",
                _pr_payload(action="opened", repo_id=777, installation_id=99),
            )
            assert r_pr.status_code == 202
            assert r_pr.headers["x-trace-id"] == "deliv-pr-1"
            assert r_pr.json()["status"] == "queued"
            assert len(spy.pr_calls) == 1

            async with session_factory() as check_backfill:
                backfilled = (await check_backfill.execute(sa.select(Repository))).scalar_one()
                assert backfilled.github_repo_id == 777
                assert backfilled.installation_id == 99

            # 3. Duplicate X-GitHub-Delivery -> 200 duplicate, NO second dispatch
            r_dup = await _send(
                "pull_request",
                "deliv-pr-1",
                _pr_payload(action="opened", repo_id=777, installation_id=99),
            )
            assert r_dup.status_code == 200
            assert r_dup.json()["status"] == "duplicate"
            assert len(spy.pr_calls) == 1

            # 4. synchronize & reopened -> 202; closed -> 200 ignored
            r_sync = await _send(
                "pull_request",
                "deliv-pr-2",
                _pr_payload(action="synchronize", head_sha="fff000", repo_id=777),
            )
            r_reopen = await _send(
                "pull_request",
                "deliv-pr-3",
                _pr_payload(action="reopened", head_sha="eee111", repo_id=777),
            )
            r_closed = await _send(
                "pull_request",
                "deliv-pr-closed",
                _pr_payload(action="closed", repo_id=777),
            )
            assert r_sync.status_code == 202
            assert r_reopen.status_code == 202
            assert r_closed.status_code == 200
            assert r_closed.json()["status"] == "ignored"
            assert len(spy.pr_calls) == 3

            # 5. push filtering: default branch -> 202; zero before_sha normalized to None;
            #    feature branch or deleted -> 200 ignored
            r_push_main = await _send("push", "deliv-push-main", _push_payload(repo_id=777))
            r_push_init = await _send(
                "push",
                "deliv-push-init",
                _push_payload(before="0" * 40, after="abc123456789", repo_id=777),
            )
            r_push_feat = await _send(
                "push",
                "deliv-push-feat",
                _push_payload(ref="refs/heads/feat/x", repo_id=777),
            )
            r_push_del = await _send(
                "push",
                "deliv-push-del",
                _push_payload(deleted=True, after="0" * 40, repo_id=777),
            )
            assert r_push_main.status_code == 202
            assert r_push_init.status_code == 202
            assert r_push_feat.status_code == 200 and r_push_feat.json()["status"] == "ignored"
            assert r_push_del.status_code == 200 and r_push_del.json()["status"] == "ignored"
            assert len(spy.push_calls) == 2
            assert spy.push_calls[1].before_sha is None

            # 6. Broker outage -> rollback DB & 503, then retry same delivery_id -> 202!
            spy.fail_next = True
            r_broker_down = await _send(
                "pull_request",
                "deliv-retry-1",
                _pr_payload(action="synchronize", head_sha="retry999", repo_id=777),
            )
            assert r_broker_down.status_code == 503

            r_broker_retry = await _send(
                "pull_request",
                "deliv-retry-1",
                _pr_payload(action="synchronize", head_sha="retry999", repo_id=777),
            )
            assert r_broker_retry.status_code == 202
            assert r_broker_retry.json()["status"] == "queued"

        # Verify DB state: CLI repo backfilled cleanly, installation updated, and counts match
        async with session_factory() as verify_session:
            repos = (await verify_session.execute(sa.select(Repository))).scalars().all()
            assert len(repos) == 1
            assert repos[0].github_repo_id == 777
            assert repos[0].installation_id == 555

            ping_row = await verify_session.get(WebhookEvent, "deliv-ping-1")
            assert ping_row is not None and ping_row.status == "ignored"
            retry_row = await verify_session.get(WebhookEvent, "deliv-retry-1")
            assert retry_row is not None and retry_row.status == "queued"
    finally:
        await test_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_celery_dispatcher_and_stub_tasks_and_lifespan(
    isolated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_trace_context()
    bind_trace_id("webhook-trace-999")
    sent: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    revoked: list[tuple[str, bool]] = []

    class _FakeControl:
        def revoke(self, task_id: str, *, terminate: bool = False) -> None:
            revoked.append((task_id, terminate))

    class _FakeCelery:
        def __init__(self) -> None:
            self.control = _FakeControl()

        def send_task(
            self,
            name: str,
            *,
            args: tuple[Any, ...] = (),
            kwargs: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
            countdown: int = 0,
        ) -> SimpleNamespace:
            sent.append(
                (name, args, {"kwargs": kwargs, "headers": headers, "countdown": countdown})
            )
            return SimpleNamespace(id=f"celery-id-{len(sent)}")

    class _FakeRedisAsync:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            self.store[key] = value
            return True

        async def delete(self, key: str) -> int:
            return 1 if self.store.pop(key, None) is not None else 0

    fake_async_redis = _FakeRedisAsync()
    dispatcher = CeleryWebhookDispatcher(
        app=_FakeCelery(),
        redis_client=fake_async_redis,  # type: ignore[arg-type]
    )
    pr_task_id = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="webhook-trace-999",
            repo_id="repo-uuid-1",
            github_repo_id=1,
            repo_full_name="org/repo",
            default_branch="main",
            installation_id=2,
            pr_number=5,
            head_sha="sha-1",
            action="opened",
        )
    )
    pr_sync_1 = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="webhook-trace-1000",
            repo_id="repo-uuid-1",
            github_repo_id=1,
            repo_full_name="org/repo",
            default_branch="main",
            installation_id=2,
            pr_number=5,
            head_sha="sha-2",
            action="synchronize",
        )
    )
    pr_sync_2 = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="webhook-trace-1001",
            repo_id="repo-uuid-1",
            github_repo_id=1,
            repo_full_name="org/repo",
            default_branch="main",
            installation_id=2,
            pr_number=5,
            head_sha="sha-3",
            action="synchronize",
        )
    )
    push_task_id = await dispatcher.dispatch_push(
        PushWebhookDispatchPayload(
            delivery_id="webhook-trace-999",
            repo_id="repo-uuid-1",
            github_repo_id=1,
            repo_full_name="org/repo",
            default_branch="main",
            installation_id=2,
            ref="refs/heads/main",
            after_sha="def",
        )
    )
    assert pr_task_id == "celery-id-1"
    assert pr_sync_1 == "celery-id-2"
    assert pr_sync_2 == "celery-id-3"
    assert push_task_id == "celery-id-4"
    assert sent[0][0] == "codesight.webhook.handle_pr_event"
    assert sent[0][2]["countdown"] == 0
    assert sent[1][2]["countdown"] == 5
    assert sent[2][2]["countdown"] == 5
    assert sent[3][0] == "codesight.webhook.handle_push_event"
    assert fake_async_redis.store["pr_active:repo-uuid-1:5"] == "sha-3"
    assert fake_async_redis.store["pr_active_task:repo-uuid-1:5"] == "celery-id-3"
    assert revoked == [("celery-id-1", False), ("celery-id-2", False)]

    class _FakeSyncRedis:
        def __init__(self, data: dict[str, str]) -> None:
            self._data = data
            self.closed = False

        def get(self, key: str) -> str | None:
            return self._data.get(key)

        def close(self) -> None:
            self.closed = True

    sync_redis = _FakeSyncRedis(fake_async_redis.store)
    assert (
        is_pr_event_superseded(
            {"repo_id": "repo-uuid-1", "pr_number": 5, "head_sha": "sha-2"},
            redis_client=sync_redis,  # type: ignore[arg-type]
        )
        is True
    )
    assert (
        is_pr_event_superseded(
            {"repo_id": "repo-uuid-1", "pr_number": 5, "head_sha": "sha-3"},
            redis_client=sync_redis,  # type: ignore[arg-type]
        )
        is False
    )
    monkeypatch.setattr(
        "codesight.worker.celery_app.redis_sync.Redis.from_url",
        lambda *_, **__: sync_redis,
    )
    assert handle_pr_event(
        {
            "repo_id": "repo-uuid-1",
            "repo_full_name": "org/repo",
            "pr_number": 5,
            "head_sha": "sha-2",
        }
    ) == {"status": "superseded", "trace_id": "webhook-trace-999"}
    assert sync_redis.closed is True

    assert handle_pr_event({"repo_full_name": "org/repo", "pr_number": 5}) == {
        "status": "accepted",
        "trace_id": "webhook-trace-999",
    }
    assert handle_push_event({"repo_full_name": "org/repo", "ref": "refs/heads/main"}) == {
        "status": "accepted",
        "trace_id": "webhook-trace-999",
    }
    clear_trace_context()

    # Verify FastAPI lifespan initializes and disposes AsyncEngine + Redis dispatcher when omitted
    app = create_app(
        settings=Settings(_env_file=None),
        webhook_dispatcher=_SpyDispatcher(),
    )
    async with app.router.lifespan_context(app):
        assert app.state.db_engine is not None
        assert app.state.session_factory is not None

    class _FakeLifespanRedis:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    lifespan_redis = _FakeLifespanRedis()
    monkeypatch.setattr("codesight.api.main.redis_async.from_url", lambda *_, **__: lifespan_redis)
    app_default_disp = create_app(
        settings=Settings(_env_file=None),
        session_factory=MagicMock(),
    )
    async with app_default_disp.router.lifespan_context(app_default_disp):
        assert isinstance(app_default_disp.state.webhook_dispatcher, CeleryWebhookDispatcher)
    assert lifespan_redis.closed is True

    # Also verify route returns 503 when session_factory is uninitialized
    uninit_app = create_app(
        settings=Settings(
            _env_file=None,
            github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        ),
        session_factory=None,
        webhook_dispatcher=_SpyDispatcher(),
    )
    valid_raw = json.dumps(_pr_payload()).encode("utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=uninit_app),
        base_url="http://test",
    ) as client:
        r_uninit = await client.post(
            "/api/v1/webhooks/github",
            content=valid_raw,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "deliv-no-db",
                "X-Hub-Signature-256": _sign_body(valid_raw),
            },
        )
    assert r_uninit.status_code == 503


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_rejects_bool_ints_invalid_slugs_oversize_strings_and_handles_edge_events(
    isolated_postgres_dsn: str,
) -> None:
    engine = create_async_engine(isolated_postgres_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    spy = _SpyDispatcher()
    app = create_app(
        settings=Settings(
            _env_file=None,
            github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        ),
        session_factory=session_factory,
        webhook_dispatcher=spy,
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:

            async def _post(
                event: str,
                delivery: str,
                raw_bytes: bytes,
                extra_headers: dict[str, str] | None = None,
            ) -> httpx.Response:
                hdrs = {
                    "X-GitHub-Event": event,
                    "X-GitHub-Delivery": delivery,
                    "X-Hub-Signature-256": _sign_body(raw_bytes),
                }
                if extra_headers:
                    hdrs.update(extra_headers)
                return await client.post("/api/v1/webhooks/github", content=raw_bytes, headers=hdrs)

            # Invalid Content-Length and invalid JSON -> 400
            r_bad_cl = await client.post(
                "/api/v1/webhooks/github",
                content=b"{}",
                headers={"Content-Length": "not-an-int"},
            )
            r_neg_cl = await client.post(
                "/api/v1/webhooks/github",
                content=b"{}",
                headers={"Content-Length": "-1"},
            )
            r_bad_json = await _post("ping", "d-bad-json", b"{not json")
            assert r_bad_cl.status_code == 400
            assert r_neg_cl.status_code == 400
            assert r_bad_json.status_code == 400

            # bool-as-int and out-of-range integer attacks -> 400
            bad_payloads: list[dict[str, Any]] = [
                _pr_payload(repo_id=True),
                _pr_payload(installation_id=True),
                _pr_payload(pr_number=True),
                _pr_payload(repo_id=-5),
                _pr_payload(repo_id=2**63),
                _pr_payload(pr_number=2**31),
                # String length / slug / whitespace SHA attacks -> 400
                _pr_payload(full_name="../evil"),
                _pr_payload(full_name="org/" + ("a" * 260)),
                _pr_payload(default_branch="b" * 129),
                _pr_payload(action="c" * 65),
                _pr_payload(head_sha="   "),
                # Missing repository or pull_request objects -> 400
                {"action": "opened"},
                {"action": "opened", "repository": {"id": 1, "full_name": "org/repo"}},
            ]
            for idx, bad_obj in enumerate(bad_payloads):
                resp = await _post(
                    "pull_request",
                    f"d-bad-payload-{idx}",
                    json.dumps(bad_obj).encode("utf-8"),
                )
                assert resp.status_code == 400, (
                    f"Expected 400 for case {idx}, got {resp.status_code}"
                )

            # Independent push deletion cases & ignored events -> 200 ignored, 0 dispatches
            r_zero_sha = await _post(
                "push",
                "d-push-zero-sha",
                json.dumps(_push_payload(deleted=False, after="0" * 40)).encode("utf-8"),
            )
            r_deleted_only = await _post(
                "push",
                "d-push-deleted-only",
                json.dumps(_push_payload(deleted=True, after="deadbeef")).encode("utf-8"),
            )
            r_push_no_repo = await _post(
                "push",
                "d-push-no-repo",
                json.dumps({"ref": "refs/heads/main", "after": "abc"}).encode("utf-8"),
            )
            r_unknown_evt = await _post(
                "issues",
                "d-unknown-evt",
                json.dumps({"action": "opened"}).encode("utf-8"),
            )
            assert r_zero_sha.status_code == 200 and r_zero_sha.json()["status"] == "ignored"
            assert (
                r_deleted_only.status_code == 200 and r_deleted_only.json()["status"] == "ignored"
            )
            assert (
                r_push_no_repo.status_code == 200 and r_push_no_repo.json()["status"] == "ignored"
            )
            assert r_unknown_evt.status_code == 200 and r_unknown_evt.json()["status"] == "ignored"
            assert spy.pr_calls == []
            assert spy.push_calls == []
    finally:
        await engine.dispose()


@contextlib.contextmanager
def _pause_coverage_tracer() -> Iterator[None]:
    """Pause coverage.py tracing strictly during the timed SLA loop after warmups."""
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_gateway_warm_mean_latency_under_100ms(
    isolated_postgres_dsn: str,
) -> None:
    engine = create_async_engine(
        isolated_postgres_dsn,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    base_settings = Settings()
    settings = base_settings.model_copy(
        update={"github_webhook_secret": SecretStr(TEST_WEBHOOK_SECRET)}
    )
    bench_queue = f"codesight_bench_{uuid.uuid4().hex[:8]}"
    bench_celery = create_celery_app(base_settings)
    bench_celery.conf.task_default_queue = bench_queue
    redis_client = redis_async.Redis.from_url(
        base_settings.redis_url,
        decode_responses=True,
        socket_timeout=base_settings.health_timeout_seconds,
    )

    app = create_app(
        settings=settings,
        session_factory=session_factory,
        webhook_dispatcher=CeleryWebhookDispatcher(
            app=bench_celery,
            redis_client=redis_client,
        ),
    )

    configure_logging(level="INFO", service_name="codesight-bench", stream=io.StringIO())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Warmup 0 + 1: populate asyncpg, Redis, Celery broker & Kombu pidbox revoke sockets
            for idx, (action, sha) in enumerate(
                (("opened", "benchwarm0000"), ("synchronize", "benchwarm0001"))
            ):
                warm_raw = json.dumps(_pr_payload(action=action, pr_number=1, head_sha=sha)).encode(
                    "utf-8"
                )
                warm_resp = await client.post(
                    "/api/v1/webhooks/github",
                    content=warm_raw,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": f"bench-warmup-{idx}",
                        "X-Hub-Signature-256": _sign_body(warm_raw),
                    },
                )
                assert warm_resp.status_code == 202

            latencies_ms: list[float] = []
            with _pause_coverage_tracer():
                for idx in range(50):
                    # Every 5th request updates an existing PR to exercise real supersede + revoke
                    pr_num = 1 if idx % 5 == 0 else idx + 2
                    raw = json.dumps(
                        _pr_payload(
                            action="synchronize",
                            pr_number=pr_num,
                            head_sha=f"benchsha{idx:04d}",
                        )
                    ).encode("utf-8")
                    t0 = time.perf_counter()
                    resp = await client.post(
                        "/api/v1/webhooks/github",
                        content=raw,
                        headers={
                            "X-GitHub-Event": "pull_request",
                            "X-GitHub-Delivery": f"bench-deliv-{idx}",
                            "X-Hub-Signature-256": _sign_body(raw),
                        },
                    )
                    latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                    assert resp.status_code == 202

        mean_ms = statistics.fmean(latencies_ms)
        median_ms = statistics.median(latencies_ms)
        assert median_ms < 100.0, f"Median webhook latency {median_ms:.2f}ms exceeded 100ms SLA"
        assert mean_ms < 100.0, f"Mean webhook latency {mean_ms:.2f}ms exceeded 100ms SLA"
    finally:
        configure_logging(level=base_settings.log_level)
        try:
            async with session_factory() as cleanup_session:
                repo_id = (
                    await cleanup_session.execute(sa.select(Repository.id))
                ).scalar_one_or_none()
            if repo_id is not None:
                keys = [
                    str(k) async for k in redis_client.scan_iter(match=f"pr_active*:{repo_id}:*")
                ]
                if keys:
                    await redis_client.delete(*keys)
            await redis_client.delete(bench_queue)
        finally:
            await redis_client.aclose()
            bench_celery.close()
            await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_commits_before_dispatch_and_handles_repo_rename_collisions(
    isolated_postgres_dsn: str,
) -> None:
    engine = create_async_engine(isolated_postgres_dsn, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    # Seed two repos: Repo A (id=100, org/alpha) and Repo B (id=200, org/beta)
    async with session_factory() as seed:
        seed.add_all(
            [
                Repository(github_repo_id=100, full_name="org/alpha", default_branch="main"),
                Repository(github_repo_id=200, full_name="org/beta", default_branch="main"),
            ]
        )
        await seed.commit()

    observed_during_dispatch: list[tuple[str | None, str | None]] = []

    class _VisibilityCheckingDispatcher:
        async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str | None:
            # Independent connection MUST already see committed Repo + WebhookEvent ('received')
            async with session_factory() as worker_session:
                repo_row = (
                    await worker_session.execute(
                        sa.select(Repository).where(Repository.full_name == payload.repo_full_name)
                    )
                ).scalar_one_or_none()
                evt_row = await worker_session.get(WebhookEvent, payload.delivery_id)
                observed_during_dispatch.append(
                    (
                        str(repo_row.id) if repo_row else None,
                        evt_row.status if evt_row else None,
                    )
                )
            if payload.delivery_id == "deliv-superseded-stale":
                return None
            return "task-vis-1"

        async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
            return "task-push-vis-1"

    app = create_app(
        settings=Settings(
            _env_file=None,
            github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        ),
        session_factory=session_factory,
        webhook_dispatcher=_VisibilityCheckingDispatcher(),
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:

            async def _send(delivery: str, body_obj: dict[str, Any]) -> httpx.Response:
                raw = json.dumps(body_obj).encode("utf-8")
                return await client.post(
                    "/api/v1/webhooks/github",
                    content=raw,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": delivery,
                        "X-Hub-Signature-256": _sign_body(raw),
                    },
                )

            # 1. Repo A (id=100) renamed to "org/beta" colliding with Repo B (id=200) -> 202
            r_rename = await _send(
                "deliv-rename-collision",
                _pr_payload(action="opened", repo_id=100, full_name="org/beta", pr_number=10),
            )
            assert r_rename.status_code == 202

            # 2. Brand-new Repo C (id=300) reuses "org/beta" -> 202, Repo A archived safely
            r_reuse = await _send(
                "deliv-slug-reuse",
                _pr_payload(action="opened", repo_id=300, full_name="org/beta", pr_number=11),
            )
            assert r_reuse.status_code == 202

            # 3. Stale out-of-order PR delivery (dispatcher returns None) -> 200 status='superseded'
            r_sup = await _send(
                "deliv-superseded-stale",
                _pr_payload(action="synchronize", repo_id=300, full_name="org/beta", pr_number=11),
            )
            assert r_sup.status_code == 200
            assert r_sup.json() == {
                "status": "superseded",
                "delivery_id": "deliv-superseded-stale",
                "event": "pull_request",
                "task_id": None,
            }

        assert len(observed_during_dispatch) == 3
        assert all(
            repo_id is not None and evt_status == "received"
            for repo_id, evt_status in observed_during_dispatch
        )

        async with session_factory() as check:
            repos = {
                r.github_repo_id: r.full_name
                for r in (await check.execute(sa.select(Repository))).scalars().all()
            }
            assert repos[200] == "org/beta__stale_200"
            assert repos[100] == "org/beta__stale_100"
            assert repos[300] == "org/beta"
            evt1 = await check.get(WebhookEvent, "deliv-rename-collision")
            evt2 = await check.get(WebhookEvent, "deliv-slug-reuse")
            evt3 = await check.get(WebhookEvent, "deliv-superseded-stale")
            assert evt1 is not None and evt1.status == "queued"
            assert evt2 is not None and evt2.status == "queued"
            assert evt3 is not None and evt3.status == "ignored"
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_webhook_stale_received_reclaim_null_repo_id_and_concurrent_upsert(
    isolated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(isolated_postgres_dsn, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    spy = _SpyDispatcher()
    app = create_app(
        settings=Settings(
            _env_file=None,
            github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        ),
        session_factory=session_factory,
        webhook_dispatcher=spy,
    )

    try:
        async with session_factory() as seed:
            seed.add_all(
                [
                    WebhookEvent(
                        delivery_id="deliv-stale-rcv",
                        event_type="pull_request",
                        action="opened",
                        status="received",
                        received_at=datetime.now(UTC) - timedelta(seconds=120),
                    ),
                    WebhookEvent(
                        delivery_id="deliv-fresh-rcv",
                        event_type="pull_request",
                        action="opened",
                        status="received",
                        received_at=datetime.now(UTC),
                    ),
                ]
            )
            await seed.commit()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:

            async def _send(delivery: str, body_obj: dict[str, Any]) -> httpx.Response:
                raw = json.dumps(body_obj).encode("utf-8")
                return await client.post(
                    "/api/v1/webhooks/github",
                    content=raw,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": delivery,
                        "X-Hub-Signature-256": _sign_body(raw),
                    },
                )

            # 1. Stale 'received' is reclaimed and dispatched; fresh 'received' returns duplicate
            r_stale = await _send("deliv-stale-rcv", _pr_payload(pr_number=101))
            r_fresh = await _send("deliv-fresh-rcv", _pr_payload(pr_number=102))
            assert r_stale.status_code == 202 and r_stale.json()["status"] == "queued"
            assert r_fresh.status_code == 200 and r_fresh.json()["status"] == "duplicate"
            assert len(spy.pr_calls) == 1

            # 2. repo_id=None inserts then updates by full_name on subsequent delivery
            r_null_insert = await _send(
                "deliv-null-repo-1",
                _pr_payload(
                    repo_id=None,
                    full_name="org/null-id-repo",
                    default_branch="main",
                    installation_id=100,
                ),
            )
            r_null_update = await _send(
                "deliv-null-repo-2",
                _pr_payload(
                    repo_id=None,
                    full_name="org/null-id-repo",
                    default_branch="develop",
                    installation_id=4321,
                ),
            )
            assert r_null_insert.status_code == 202
            assert r_null_update.status_code == 202

            # 3. Non-string repository.full_name and non-string action return 400
            r_bad_name_type = await _send(
                "deliv-bad-name-type",
                {
                    "action": "opened",
                    "repository": {"full_name": 123},
                    "pull_request": {"number": 1, "head": {"sha": "abc"}},
                },
            )
            r_bad_action_type = await _send(
                "deliv-bad-action-type",
                {
                    "action": 123,
                    "repository": {"full_name": "org/valid"},
                },
            )
            assert r_bad_name_type.status_code == 400
            assert r_bad_action_type.status_code == 400

        async with session_factory() as check:
            null_repo = (
                await check.execute(
                    sa.select(Repository).where(Repository.full_name == "org/null-id-repo")
                )
            ).scalar_one()
            assert null_repo.github_repo_id is None
            assert null_repo.default_branch == "develop"
            assert null_repo.installation_id == 4321

        # 4. Concurrent IntegrityError inside _upsert_repository resolves cleanly on retry
        async with session_factory() as race_session:
            orig_begin_nested = race_session.begin_nested
            triggered = False

            def _raced_begin_nested() -> Any:
                nonlocal triggered
                ctx = orig_begin_nested()
                if not triggered:
                    triggered = True

                    class _RacingSavepoint:
                        async def __aenter__(self) -> Any:
                            async with session_factory() as other_session:
                                other_session.add(
                                    Repository(
                                        github_repo_id=8888,
                                        full_name="org/race-upsert",
                                        default_branch="main",
                                        installation_id=1,
                                    )
                                )
                                await other_session.commit()
                            return await ctx.__aenter__()

                        async def __aexit__(self, *args: object) -> Any:
                            return await ctx.__aexit__(*args)

                    return _RacingSavepoint()
                return ctx

            monkeypatch.setattr(race_session, "begin_nested", _raced_begin_nested)
            resolved = await _upsert_repository(
                race_session,
                github_repo_id=8888,
                full_name="org/race-upsert",
                default_branch="trunk",
                installation_id=99,
            )
            await race_session.commit()
            assert resolved.github_repo_id == 8888
            assert resolved.default_branch == "trunk"
            assert resolved.installation_id == 99

        # 5. Phase-3 status update never clobbers worker 'processed'/'failed' back to 'queued'
        class _FastWorkerDispatcher:
            async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
                async with session_factory() as worker_session:
                    await worker_session.execute(
                        sa.update(WebhookEvent)
                        .where(WebhookEvent.delivery_id == payload.delivery_id)
                        .values(status="processed")
                    )
                    await worker_session.commit()
                return "fast-worker-task-1"

            async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
                return "fast-push-1"

        race_app = create_app(
            settings=Settings(
                _env_file=None,
                github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
            ),
            session_factory=session_factory,
            webhook_dispatcher=_FastWorkerDispatcher(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=race_app),
            base_url="http://test",
        ) as race_client:
            raw_fast = json.dumps(_pr_payload(pr_number=202, head_sha="fastsha202")).encode("utf-8")
            r_fast = await race_client.post(
                "/api/v1/webhooks/github",
                content=raw_fast,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "deliv-fast-worker-202",
                    "X-Hub-Signature-256": _sign_body(raw_fast),
                },
            )
            assert r_fast.status_code == 202

        async with session_factory() as verify_fast:
            fast_evt = await verify_fast.get(WebhookEvent, "deliv-fast-worker-202")
            assert fast_evt is not None
            assert fast_evt.status == "processed"
    finally:
        await engine.dispose()
