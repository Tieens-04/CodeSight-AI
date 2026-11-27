# CodeSight-AI — Persistent Team Memory (`pstack` + Grok Bot Edition)

> Shared, rot-resistant engineering memory consulted at **Step 1 (Plan)**, **Step 2 (Implement)**, and **Step 6 (Deliver)**.
>
> **Write Policy (Lauren Tan's 4-Step Hierarchy)**:
>
> 1. Fix setup/tooling first.
> 2. Encode deterministic checks in `.ai-workflow/scripts/verify.py`, `preclose_audit.py`, `audit_qdrant_zerocode.py`, `validate_diff_and_payload.py`, `check_kpi_gates.py`, Ruff, or `mypy --strict`.
> 3. Encode domain review checklists in `.ai-workflow/rules/domain-review.md`.
> 4. Add to `MEMORY.md` **last** — max 2 lines per item, written as a **general principle + canonical source**, never an incident story.
>
> **Fetch, Never Remember**: Live Jira AC/comments (`atlassian-mcp-server` / `jira-cwb`), `AGENTS.md`, `src/codesight/db/models.py`, and `python .ai-workflow/scripts/verify.py diff` are always fetched fresh; never rely on memory for mutable schema or ticket state.

---

## 1. Decisions (Settled Architecture & Conventions)

- **Zero-Code Persistence (`NFR-3`)**: Keep customer blobs/diffs strictly in RAM (`src/codesight/vcs/blob_fetcher.py`), enforce Qdrant payload allowlists (`src/codesight/rag/models.py`, single-line `signature <= 300` chars), clean temp workspaces via `src/codesight/core/ephemeral.py` in `finally`, and apply 3-layer OTel/Langfuse span masking (`src/codesight/core/observability.py`).
- **Webhook `< 100ms` ACK & Cancel-on-Supersede**: Verify `HMAC-SHA256` with `hmac.compare_digest` over raw bytes, claim `X-GitHub-Delivery` idempotency atomically, `commit()` DB state before Celery dispatch (`src/codesight/api/routes/webhooks.py`), and enforce `AsyncSupersedeLock.ensure_active(stage="pre_publish")` (`src/codesight/worker/supersede_lock.py`).
- **Shared per-loop RPM & `< 25s` SLA guard**: Share `asyncio.Semaphore(3)` per event loop via `WeakKeyDictionary` (`src/codesight/agents/llm_factory.py`), batch `gemini-embedding-001` at `64` chunks/call with `output_dimensionality=768` (`src/codesight/rag/embedder.py`), disable overlapping SDK retries (`max_retries=1`), and fail-fast when `Retry-After` exceeds the remaining SLA deadline.
- **Tolerant per-item LLM wire parsing**: Parse LLM outputs into tolerant draft schemas (`extra="ignore"`) and validate/normalize each finding or verdict individually (`src/codesight/agents/schemas.py`, `src/codesight/agents/graph.py`) so one malformed candidate never crashes the whole PR review.
- **Zero `HTTP 422` & full-buffer AST suggestion splice**: Track `hunk_id` (`\ No newline...` never advances line counters), omit `start_line`/`start_side` for single-line comments, downgrade out-of-hunk findings to PR Summary, and splice every ```` ```suggestion ```` into the full in-memory file buffer verified with `not tree.root_node.has_error` (`src/codesight/vcs/github_publisher.py`).
- **Non-blocking async telemetry & task cancellation**: Never invoke synchronous network/flush calls (e.g., `get_trace_url()`, sync `flush()`) on the `asyncio` event loop; cancel sibling tasks cleanly on failure and isolate bounded telemetry flushes via `asyncio.to_thread` (`src/codesight/core/observability.py`, `src/codesight/agents/graph.py`).
- **Trust-on-Default-Branch `.codesight.yaml`**: Read `.codesight.yaml` (`<= 64 KiB`, `yaml.safe_load`) exclusively from the repository default branch with field-level fallback and deterministic 16-hex SHA-256 `config_hash` (`src/codesight/config_reader/repo_config.py`).
- **Batch-scoped context & evidence grounding**: Scope batch auxiliary context by both `right_line_hunks_by_file` and `left_deleted_hunks_by_file` (`src/codesight/agents/nodes/context_builder.py`), drop auditor drafts lacking anchored or file-linked `<reference_context>` code identifiers, mask credential literals (including multi-word passphrases while sparing error-message keys), and re-align suggestion indentation (`src/codesight/agents/nodes/security_auditor.py`).
- **Shared Auditor Guardrails & Indentation Scaling**: Reuse `run_auditor_node` and `validate_auditor_findings` (enclosing-symbol + optional `LEFT` deleted-symbol caller linking, same-hunk suggestion enforcement, batch-wide `RIGHT`+`LEFT`+TS-backtick secret masking, and delta-based 2/4-space scaling) (`src/codesight/agents/nodes/_auditor_common.py`, `src/codesight/agents/nodes/perf_arch_auditor.py`).
- **Single-Source Gemini Model Catalog & Pricing**: Define supported Gemini models and USD/1M token rates once in `GEMINI_MODEL_PRICING_USD_PER_1M` (`src/codesight/core/config.py`) and derive `ALLOWED_GEMINI_CRITIC_MODELS` and `MODEL_PRICING_PER_1M_TOKENS` (`src/codesight/core/observability.py`) from it.

---

## 2. Sources (Canonical Repo Examples for `plan.md` Startup Checklist)

- **Webhook Gateway, HMAC, Idempotency & Supersede Lock**: `src/codesight/api/routes/webhooks.py`, `src/codesight/worker/supersede_lock.py`, `src/codesight/worker/lifecycle.py`
- **Ephemeral `NFR-3` Guard, Settings & Masked Langfuse Tracing**: `src/codesight/core/ephemeral.py`, `src/codesight/core/config.py`, `src/codesight/core/observability.py`
- **Tree-sitter AST Engine (`py` / `ts` / `tsx`) & Removed Symbols**: `src/codesight/ast_engine/python_parser.py`, `src/codesight/ast_engine/typescript_parser.py`, `src/codesight/ast_engine/registry.py`
- **Batched Embeddings, Zero-Code Qdrant & Hybrid RAG Packing**: `src/codesight/rag/embedder.py`, `src/codesight/rag/qdrant_store.py`, `src/codesight/rag/retriever.py`, `src/codesight/rag/token_packer.py`
- **Smart Diff Mapping, `Zero HTTP 422` & Suggestion Guard**: `src/codesight/diff/hunk_mapper.py`, `src/codesight/vcs/github_publisher.py`, `tests/unit/test_github_publisher_422_guard.py`
- **LangGraph Batch-Grouped Parallel / `Send()` & `LLMFactory`**: `src/codesight/agents/llm_factory.py`, `src/codesight/agents/schemas.py`, `src/codesight/agents/graph.py`, `tests/unit/test_langgraph_batching_and_rate_limit.py`
- **Zero-LLM Context Builder & Grounded Security Auditor**: `src/codesight/agents/nodes/context_builder.py`, `src/codesight/agents/nodes/security_auditor.py`, `src/codesight/agents/prompts/security_prompt.py`, `tests/unit/test_context_builder_formatting.py`, `tests/integration/test_security_auditor.py`
- **Parallel Auditors, Shared Guardrails & Live Timing Gate**: `src/codesight/agents/nodes/_auditor_common.py`, `src/codesight/agents/nodes/perf_arch_auditor.py`, `src/codesight/agents/prompts/perf_arch_prompt.py`, `tests/integration/test_parallel_auditors.py`
- **Hermetic Live Docker Integration Suites (`5433` / `6381` / `6333-6334`)**: `tests/integration/test_week1_webhook_to_worker_e2e.py`, `tests/integration/test_week2_dual_lang_rag_e2e.py`

---

## 3. Prompts & Role Delegation Patterns (4-Role Specialist Team)

- **`Researcher` (`/how` + `/why` via `research` subagent in Step 1)**: Delegate multi-file exploration to a read-only `research` subagent that maps live execution (`/how`), checks `git log -n 5` / `git log -S` on concurrency/security guards (`/why`), and returns `file:line` citations + 1–2 canonical repo examples in ≤ 30 lines.
- **`Engineer` Specialist Split (Large Cross-Subsystem Tickets in Step 2)**: When `plan.md` spans complex backend async/LangGraph/RAG pipelines AND `/radar` Jinja2/Chart.js UI or CLI/evals, isolate `Pipeline & Infra Specialist` (`src/codesight/{agents,rag,worker,db}/**`) from `Surface, UI & Eval Specialist` (`src/codesight/{api,cli,scoring}/**`, `evals/**`).
- **`Checker` (`FAILED / SAW / FIX` + `Comment-Sicko` in Step 3)**: Dispatch 3 independent `research` subagents (`Standards & Domain`, `Spec & Test`, `Ponytail & Comment-Sicko`) over `python .ai-workflow/scripts/verify.py diff` output; strip AI-generated comment narration, dead code, and speculative abstractions.

---

## 4. Lessons (Generalizable Guardrails)

- **Uncommitted working-tree diff**: Steps 2 and 4 do not commit; always collect review scope via `python .ai-workflow/scripts/verify.py diff` + `git diff HEAD` + direct reads of `??` untracked files (three-dot `main...HEAD` returns empty).
- **Branch coverage (`branch = true`)**: `pyproject.toml` enforces `>= 80%` **branch** coverage across `src/codesight`; unit tests must exercise both truthy and fallback/error branches rather than happy paths alone.
- **Credential-gated live tests & env isolation**: Gate external API smoke tests (`GEMINI_API_KEY`, `LANGFUSE_*`, GitHub PAT) with `@pytest.mark.skipif(...)` at decorator level so disabled runs skip zero-cost without provisioning DBs, and clear ambient `.env` keys via `monkeypatch.delenv` in unit tests.
- **Docker port coexistence & hermetic teardown**: Scope Compose commands strictly with `docker compose -p codesight ...` (`5433`, `6381`, `6333/6334`, `8082`), never touch `flintflow-*` or `documind-*`, and isolate integration DB/Redis/Qdrant teardown in independent `try ... finally` blocks.
- **Headed Playwright execution**: Always run Playwright browser checks for `/radar` UI in headed mode (`--headed` / `headless: false`) so browser execution stays visible on screen.
- **Independent ground-truth evaluation**: Keep answer hints out of fixture snippets and assert strict 1-to-1 `(file_path, start_line, end_line)` spans (rejecting over-wide spans, duplicates, and safe-control false positives) (`tests/integration/test_security_auditor.py`).
