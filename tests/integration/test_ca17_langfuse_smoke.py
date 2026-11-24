"""Credential-gated live Gemini + Langfuse Cloud smoke test for CA-17."""

from __future__ import annotations

import pytest

from codesight.agents.graph import run_pr_review_graph
from codesight.agents.llm_factory import LLMFactory
from codesight.core.config import Settings
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser


def _has_live_ca17_credentials() -> bool:
    try:
        cfg = Settings()
    except Exception:
        return False
    return cfg.has_gemini_api_key and cfg.has_langfuse_credentials and cfg.langfuse_tracing_enabled


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_live_ca17_credentials(),
    reason=("Requires GEMINI_API_KEY, LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY in env/.env."),
)
@pytest.mark.asyncio
async def test_ca17_live_gemini_and_langfuse_cloud_smoke() -> None:
    """Verify real 3-call LangGraph execution and NFR-3 masked hierarchical Langfuse export."""
    patch_text = "\n".join(
        [
            "@@ -1,2 +1,6 @@",
            " import sqlite3",
            " ",
            "+def fetch_user(conn: sqlite3.Connection, user_id: str) -> object:",
            "+    query = f\"SELECT * FROM users WHERE id = '{user_id}'\"",
            "+    return conn.execute(query).fetchone()",
            "+",
        ]
    )
    parsed_diff = SmartDiffParser().parse_patch_entries(
        [PatchFileEntry(filename="src/users_repo.py", patch=patch_text, status="modified")]
    )
    cfg = Settings()
    factory = LLMFactory(settings=cfg)

    final_state = await run_pr_review_graph(
        parsed_diff=parsed_diff,
        llm_factory=factory,
        pr_id="Tieens-04/CodeSight-AI#smoke-ca17",
        head_sha="smoke001",
    )

    assert final_state.status == "completed"
    assert final_state.batching_mode == "single_batch"
    assert len(final_state.llm_metrics) == 3
    assert final_state.telemetry.status == "active"
    assert final_state.telemetry.flushed is True
    assert final_state.telemetry.trace_id is not None
