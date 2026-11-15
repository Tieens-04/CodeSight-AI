# CodeSight-AI — Project Rules, Architecture Invariants & Jira Workflow

## 1. Jira Space & Task Management Configuration
- **Atlassian Instance:** `https://nguyentien191004.atlassian.net` (`nguyentien191004@gmail.com`)
- **MCP Server:** `jira-cwb` (and `atlassian-mcp-server`)
- **Jira Space / Project Key:** `CA` (**CodeSight-AI** — `https://nguyentien191004.atlassian.net/jira/software/projects/CA/summary`)
- **Agile Board ID:** `74` (`CA board`)
  - `Sprint 1` (ID `110`): `CA-5` .. `CA-10` (`CA-1` Epic 1)
  - `Sprint 2` (ID `111`): `CA-11` .. `CA-16` (`CA-2` Epic 2)
  - `Sprint 3` (ID `112`): `CA-17` .. `CA-22` (`CA-3` Epic 3)
  - `Sprint 4` (ID `113`): `CA-23` .. `CA-28` (`CA-4` Epic 4)
- **Mandatory Jira Workflow Transitions (`CA-*`):**
  1. Before starting implementation on a ticket `CA-X`, transition status to **`In Progress`** (`transition: "21"`).
  2. Work on a dedicated Git branch named `feat/ca-X-<short-slug>` (e.g., `feat/ca-5-day1-foundation`).
  3. During automated testing / verification, transition status to **`Verify / Test`** (`transition: "31"`).
  4. Only after 100% of Acceptance Criteria, `ruff`, `mypy`, and `pytest` pass with real verification output, post a structured **Verification Evidence** comment via `jira_add_comment` and transition status to **`Done`** (`transition: "41"`).

## 2. Architecture & Locked Tech Stack Baseline
- **Runtime & Package Manager:** Python `3.12+` managed strictly via `uv` (`pyproject.toml` + `uv.lock`). Never run global `pip install`.
- **Web Gateway & Radar UI:** FastAPI, Pydantic v2, Structlog (JSON logging with `trace_id`), Jinja2 + Chart.js (`GET /radar`), Typer + Rich (`codesight` CLI).
- **Async Queue & State:** Celery + Redis 7 (Broker, SHA-level cache, Cancel-on-Supersede lock), PostgreSQL 16 (`SQLAlchemy 2.0 Async` + `Alembic`).
- **AST & Hybrid Code RAG:** `tree-sitter` (`tree-sitter-python`, `tree-sitter-typescript` for `.ts`/`.tsx`), Qdrant (`gRPC` + Payload Keyword Indexes on `symbol_name`, `called_symbols`, `blob_sha`).
- **AI & LLMOps:** LangGraph (`Batch-Grouped Parallel` ~3 LLM calls/PR), Google Gemini API (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-embedding-001`) behind `LLMFactory`, Langfuse Cloud (Free Tier), DeepEval.

## 3. Local Host Port Allocation (FlintFlow & DocuMind Coexistence)
- **NEVER** stop, modify, or collide with `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`) containers.
- Always parameterize via `.env` using these conflict-free canonical host defaults:
  - **PostgreSQL 16 (`codesight-postgres`):** `5433` -> container `5432` (`POSTGRES_PORT=5433`)
  - **Redis 7 (`codesight-redis`):** `6381` -> container `6379` (`REDIS_PORT=6381`)
  - **Qdrant HTTP / gRPC (`codesight-qdrant`):** `6333` / `6334` (`QDRANT_HTTP_PORT=6333`, `QDRANT_GRPC_PORT=6334`)
  - **FastAPI Gateway & `/radar` (`codesight-api`):** `8082` -> container `8000` (`API_PORT=8082`)
- **Playwright / Browser Checks:** Always run Playwright in headed mode (`--headed` / `headless: false`) per user rule.

## 4. Non-Negotiable Engineering & Security Invariants
1. **Zero-Code Persistence (`NFR-3`):**
   - Never persist customer raw source code on disk or inside Qdrant payloads. Qdrant stores vectors + AST metadata (`file_path`, `blob_sha`, `start_line`, `end_line`, `signature`, `called_symbols`, `imports`) only. Fetch required code ranges into RAM via GitHub Blob API at runtime and clean ephemeral workspaces in `finally`.
2. **Latency & Quota Discipline (`< 25s` E2E, `< 300ms` Webhook ACK):**
   - `/api/v1/webhooks/github` must verify `HMAC-SHA256` (`hmac.compare_digest`), enforce `X-GitHub-Delivery` idempotency, and return `202 Accepted` in `< 100ms`.
   - Run `pull_request.opened` with `0s` delay; apply **Cancel-on-Supersede** + `5s` debounce on `pull_request.synchronize`.
   - Protect Google AI Studio Free Tier RPM limits with batched embeddings (`64` chunks/call), `asyncio.Semaphore(3)`, and Exponential Backoff.
3. **Zero `HTTP 422` & Syntax-Safe Suggestions:**
   - Map every diff hunk to `valid_commentable_lines` on `RIGHT` side; automatically downgrade out-of-hunk findings to the PR Summary.
   - Validate every ```` ```suggestion ```` block in-memory with `tree-sitter` (zero syntax `ERROR` nodes) before publishing to GitHub.
4. **Quality & KPI Gates:**
   - Maintain `>= 80%` test coverage across core modules (`diff`, `ast_engine`, `rag`, `scoring`).
   - Benchmark targets (`evals/run_benchmark.py`): `Precision >= 85%`, `Suggestion Syntax Pass Rate = 100%`, `Token Reduction >= 40%`, `Average E2E Latency < 25s`.
