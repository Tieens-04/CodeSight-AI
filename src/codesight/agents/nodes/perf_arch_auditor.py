"""Performance & Architecture Auditor Agent node with grounding, redaction, and indent guards."""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes._auditor_common import run_auditor_node, validate_auditor_findings
from codesight.agents.prompts.perf_arch_prompt import PERF_ARCH_AUDITOR_SYSTEM_PROMPT
from codesight.agents.schemas import (
    BuiltBatchContext,
    DiffBatch,
    FindingCandidate,
    LLMCallMetrics,
    LLMFindingDraft,
)

logger = structlog.get_logger("codesight.agents.nodes.perf_arch_auditor")


def validate_perf_arch_findings(
    drafts: Sequence[FindingCandidate | LLMFindingDraft],
    *,
    batch: DiffBatch,
    batch_context: BuiltBatchContext | None = None,
) -> tuple[list[FindingCandidate], int, int]:
    """Validate RIGHT-hunk grounding, caller/ADR evidence, redaction, and 2/4-space indents."""
    return validate_auditor_findings(
        drafts,
        role="logic_performance_auditor",
        id_prefix="log",
        batch=batch,
        batch_context=batch_context,
        include_deleted_in_caller_link=True,
    )


def build_perf_arch_auditor_messages(batch_context: BuiltBatchContext) -> list[BaseMessage]:
    """Construct the System + Human message pair for Performance & Architecture Auditor."""
    return [
        SystemMessage(content=PERF_ARCH_AUDITOR_SYSTEM_PROMPT),
        HumanMessage(content=batch_context.rendered_prompt),
    ]


async def run_perf_arch_auditor(
    *,
    batch: DiffBatch,
    batch_context: BuiltBatchContext,
    llm_factory: LLMFactory,
    config: RunnableConfig | None = None,
) -> tuple[list[FindingCandidate], LLMCallMetrics, int]:
    """Run Performance & Architecture Auditor LLM call and validate batch RIGHT-line findings."""
    return await run_auditor_node(
        role="logic_performance_auditor",
        id_prefix="log",
        system_prompt=PERF_ARCH_AUDITOR_SYSTEM_PROMPT,
        event_name="perf_arch_auditor_completed",
        node_logger=logger,
        batch=batch,
        batch_context=batch_context,
        llm_factory=llm_factory,
        config=config,
        include_deleted_in_caller_link=True,
    )
