"""Prompt templates and versioned system instructions for CodeSight-AI review agents."""

from codesight.agents.prompts.perf_arch_prompt import (
    PERF_ARCH_AUDITOR_SYSTEM_PROMPT,
    PERF_ARCH_CATEGORIES,
    PERF_ARCH_PROMPT_VERSION,
    get_perf_arch_system_prompt,
)
from codesight.agents.prompts.security_prompt import (
    OWASP_SECURITY_CATEGORIES,
    SECURITY_AUDITOR_SYSTEM_PROMPT,
    SECURITY_PROMPT_VERSION,
    get_security_system_prompt,
)

__all__ = [
    "OWASP_SECURITY_CATEGORIES",
    "PERF_ARCH_AUDITOR_SYSTEM_PROMPT",
    "PERF_ARCH_CATEGORIES",
    "PERF_ARCH_PROMPT_VERSION",
    "SECURITY_AUDITOR_SYSTEM_PROMPT",
    "SECURITY_PROMPT_VERSION",
    "get_perf_arch_system_prompt",
    "get_security_system_prompt",
]
