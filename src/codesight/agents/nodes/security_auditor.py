"""Security Auditor Agent node with coordinate grounding, secret masking, and indent guards."""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes._auditor_common import (
    BatchDiffIndex,
    is_evidence_grounded,
    mask_sensitive_values,
    parse_batch_diff_index,
    run_auditor_node,
    validate_auditor_findings,
)
from codesight.agents.prompts.security_prompt import SECURITY_AUDITOR_SYSTEM_PROMPT
from codesight.agents.schemas import (
    BuiltBatchContext,
    DiffBatch,
    FindingCandidate,
    LLMCallMetrics,
    LLMFindingDraft,
)

__all__ = [
    "BatchDiffIndex",
    "build_security_auditor_messages",
    "is_evidence_grounded",
    "mask_sensitive_values",
    "parse_batch_diff_index",
    "run_security_auditor",
    "validate_security_findings",
]

logger = structlog.get_logger("codesight.agents.nodes.security_auditor")


def validate_security_findings(
    drafts: Sequence[FindingCandidate | LLMFindingDraft],
    *,
    batch: DiffBatch,
    batch_context: BuiltBatchContext | None = None,
) -> tuple[list[FindingCandidate], int, int]:
    """Validate coordinate grounding, mask secrets, and preserve suggestion indentation."""
    return validate_auditor_findings(
        drafts,
        role="security_auditor",
        id_prefix="sec",
        batch=batch,
        batch_context=batch_context,
        include_deleted_in_caller_link=False,
    )


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
    return await run_auditor_node(
        role="security_auditor",
        id_prefix="sec",
        system_prompt=SECURITY_AUDITOR_SYSTEM_PROMPT,
        event_name="security_auditor_completed",
        node_logger=logger,
        batch=batch,
        batch_context=batch_context,
        llm_factory=llm_factory,
        config=config,
        include_deleted_in_caller_link=False,
    )
