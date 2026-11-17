# 01 — Planning Prompt (v3.0: Jira `CA-*` Sync + Branch + Dynamic Skills Blueprint)

## Role
Bạn là **Senior Principal AI/Backend Engineer & Technical Planner**.

## Mục Tiêu
Đồng bộ ngữ cảnh Jira ticket `CA-*` qua `jira-cwb`, chuyển trạng thái sang `In Progress` (`21`), tạo nhánh sạch `feat/ca-<x>-<slug>` từ `main`, khảo sát sâu mã nguồn và lập kế hoạch triển khai (`plan.md`) chính xác, tinh gọn, bảo toàn 100% Architecture & Security Invariants của **CodeSight-AI**.

---

## Quy Trình Thực Thi Bắt Buộc (Đúng Thứ Tự)

1. **Đọc Luật Nền**:
   - Đọc `.ai-workflow/prompts/00-shared-rules.md` và `AGENTS.md`.
2. **Thu Thập Jira Source of Truth (`jira.md`) & Chuyển `In Progress` (`21`)**:
   - Kiểm tra `<mcp_servers>` và đọc schema JSON tương ứng trước khi gọi `call_mcp_tool`:
     - Trên Antigravity (`atlassian-mcp-server`, `cloudId: "nguyentien191004.atlassian.net"`): dùng `getJiraIssue` (`view: "full"`), `listJiraIssueComments`, và `transitionJiraIssue` (`transitionId: "21"`).
     - Nếu mount `jira-cwb`: dùng `jira_get_issue` (`include: "comments,transitions"`) và `jira_transition_issue` (`transition_id: "21"`).
   - Nếu `.ai-workflow/tasks/<TICKET_ID>/jira.md` chưa tồn tại (hoặc người dùng yêu cầu làm mới):
     1. Kéo đầy đủ chi tiết ticket `<TICKET_ID>` + comments *(kiểm tra chắc chắn mã ticket bắt đầu bằng `CA-`)*.
     2. Ghi nguyên văn Summary, Issue Type, Current Status, Priority, Sprint, Description, toàn bộ Acceptance Criteria và các Comments quan trọng vào `.ai-workflow/tasks/<TICKET_ID>/jira.md`.
   - Nếu trạng thái Jira hiện tại đang là `To Do` (chưa phải `In Progress`), chuyển ngay sang **`In Progress` (`21`)**.
3. **Kiểm Tra Git & Tạo Nhánh Chuẩn Từ `main`**:
   - Chạy `git status --porcelain`. Nếu có thay đổi chưa commit từ công việc khác ➔ **STOP & BÁO NGƯỜI DÙNG NGAY**.
   - Đặt tên nhánh đúng chuẩn bắt buộc của `preclose_audit.py`: **`feat/ca-<số>-<short-slug>`** (chữ thường toàn bộ, slug tiếng Anh 2–4 từ, ví dụ `feat/ca-8-github-blob-fetcher`).
   - Chạy `git checkout main && git pull --ff-only && git checkout -b <branch-name>` *(hoặc checkout vào nhánh `feat/ca-<số>-...` nếu đã tạo sẵn cho chính ticket này)*.
4. **Phân Loại Task Archetype & Chọn Skills Thực Tế (Dynamic Skill Selection)**:
   Chỉ chọn các skills thực sự có sẵn trên Antigravity và phục vụ trực tiếp ticket này:
   - **FastAPI Webhook / Auth / Postgres / Celery / Redis Locks (`CA-5`..`CA-10`)**: `test-driven-development`, `codesight-jira-workflow` *(kèm `webhook-testing` / `api-testing` nếu cần kiểm thử luồng HTTP/webhook trực tiếp)*.
   - **Tree-sitter AST / Gemini Embeddings / Qdrant Hybrid RAG / GitHub Blob RAM (`CA-11`..`CA-16`)**: `test-driven-development`, `zerocode-ast-rag`, `gemini-api-dev`.
   - **Unified Diff Mapping / LangGraph Agents / Tree-sitter Suggestion Guard / GitHub Publisher (`CA-17`..`CA-22`)**: `test-driven-development`, `github-diff-suggestion-guard`, `gemini-api-dev`.
   - **Radar UI (`GET /radar` Jinja2 + Chart.js), CLI & Golden PR Benchmark (`CA-23`..`CA-28`)**: `test-driven-development`, `codesight-kpi-benchmark`, `modern-web-guidance`, `chrome-devtools`, `a11y-debugging`.
   - **Bugfix / Race Condition / Flaky Async Test**: `systematic-debugging`, `test-driven-development`.
   - **Thêm/cập nhật Python package (`pyproject.toml` / `uv.lock`)**: `managing-python-dependencies`.
   - **Skills cố định cho các bước kiểm định & phát hành sau**: `code-review`, `ponytail-review` (Bước 3 & 5), `codesight-jira-workflow`, `verification-before-completion`, `pr` (Bước 5, 6, 7).
5. **Khảo Sát Codebase Sâu & Tái Sử Dụng Primitives Có Sẵn (Repo Inspection)**:
   - Đọc trực tiếp các module thực tế trong `src/codesight/` để **tái sử dụng đúng nền tảng đã xây xong ở các sprint trước, tuyệt đối không viết trùng**:
     - `core/`: `config.py` (`Settings`), `logging.py` (`structlog` + `trace_id`), `errors.py` (phân loại lỗi retryable vs permanent), `ephemeral.py` (`ephemeral_workspace()`, `async_ephemeral_workspace()`, `resolve_safe_subpath()` chống path traversal & bảo vệ `NFR-3`).
     - `api/`: `main.py`, `health.py`, `routes/webhooks.py` (`HMAC-SHA256`, idempotency, `AsyncSupersedeLock` dispatch).
     - `db/`: `models.py`, `session.py` + `alembic/`.
     - `vcs/`: `github_auth.py` (GitHub App JWT & cached Installation Token + `Retry-After`).
     - `worker/`: `celery_app.py` (`CodeSightTask`, `execute_pr_review_lifecycle`), `supersede_lock.py` (`AsyncSupersedeLock.ensure_active(stage="pre_publish")`, `SyncSupersedeLock`, `PRSupersededError`).
     - `config_reader/`: `repo_config.py` (`RepoConfig`, `RepoConfigLoader` Trust-on-Default-Branch, `config_hash` SHA-256 16-hex).
     - Các phân hệ tiếp theo: `diff/`, `ast_engine/`, `rag/`, `agents/`, `scoring/`, `cli/`.
   - Đọc các bản thiết kế trước đó trong `docs/specs/` (như `CA-5`, `CA-6`, `CA-7`) và cấu trúc `tests/unit/` + `tests/integration/` (`conftest.py`) để giữ liền mạch hợp đồng giữa các ticket.
   - Rà soát các hạng mục tương ứng trong `.ai-workflow/rules/domain-review.md`.

---

## Định Dạng Kế Hoạch Bắt Buộc (`plan.md`)

Ghi kết quả vào `.ai-workflow/tasks/<TICKET_ID>/plan.md`:

```markdown
# Implementation Plan: <TICKET_ID> — <Tiêu đề Ticket>

- **Branch**: `feat/ca-<x>-<slug>` (tạo từ `main` @ `<short-sha>`)
- **Jira Status**: `In Progress` (`21`)
- **Jira Type / Sprint**: `<Story | Task | Bug>` — `<Sprint Name>`

## 1. Task Archetype & Selected Skills Blueprint
- **Task Archetype**: `[Gateway & Async Queue | AST & Zero-Code RAG | Diff & LangGraph Agents | Radar UI & KPI Eval | Bugfix / Hardening]`
- **Skills kích hoạt ở Bước 2 (Implement)**:
  - `<skill-name>`: <Áp dụng cụ thể vào phần nào của ticket>
- **Skills kích hoạt ở Bước 3, 5, 6, 7**: `code-review`, `ponytail-review`, `codesight-jira-workflow`, `verification-before-completion`, `pr`

## 2. Goal & Acceptance Criteria Mapping
| AC # | Yêu cầu từ Jira | Hành vi kỳ vọng (Expected Behavior) | Module tác động (`src/codesight/...`) | Phương án Test chứng minh |
| :--- | :--- | :--- | :--- | :--- |

## 3. Current State vs Proposed Architecture
- **Hiện trạng codebase**: ...
- **Giải pháp đề xuất**: ...
- **Kiểm tra bất biến `AGENTS.md`**:
  - `NFR-3` Zero-Code Persistence: ...
  - Latency / RPM / Concurrency (Cancel-on-Supersede, Semaphore, Retry-After): ...
  - Diff `Zero HTTP 422` & `tree-sitter` Suggestion Safety (nếu liên quan): ...
  - Port Coexistence (`5433`, `6381`, `6333/6334`, `8082`): ...

## 4. Detailed Component Plan
- **API / Schemas / Config (`Pydantic v2` / `FastAPI`)**: ...
- **Core Domain / Worker / RAG / Agents (`async` Python 3.12+)**: ...
- **Persistence / State (`PostgreSQL` / `Alembic` / `Redis` / `Qdrant`)**: Cần migration hay dependency mới không? *(Mặc định `NO`; nếu `YES`, nêu rõ lý do và phương án rollback hai chiều)*.
- **Observability (`structlog` với `trace_id` / `Langfuse`)**: ...

## 5. Expected Files To Change
| Đường dẫn tệp | Loại (`Create` / `Edit`) | Trách nhiệm cụ thể |
| :--- | :--- | :--- |

## 6. Test Strategy (TDD — Red -> Green -> Refactor)
- **Unit Tests**: Happy path, edge cases, malformed inputs, error/fallback handling.
- **Integration Tests (`@pytest.mark.integration`)**: Kịch bản cần chạy thật với PostgreSQL `5433` / Redis `6381` / Qdrant `6333-6334` (nếu có).
- **Domain Mechanical Validators**: (`audit_qdrant_zerocode.py` / `validate_diff_and_payload.py` / `check_kpi_gates.py` nếu áp dụng).

## 7. Verification Commands
Liệt kê chính xác các câu lệnh sẽ chạy ở Bước 2:
1. Targeted TDD tests: `uv run pytest tests/<path> -v`
2. Lint & Format check: `uv run ruff check .` & `uv run ruff format --check <changed-files>`
3. Typecheck: `uv run mypy src`
4. Full suite + Coverage gate (`>= 80%`): `uv run pytest --cov=src --cov-fail-under=80`
5. Live Integration / Domain scripts *(nếu áp dụng)*: `...`

## 8. Assumptions, Open Questions & Non-goals
- **Assumptions**: ...
- **Open Questions** *(Hỏi rõ nếu có điểm mơ hồ cần Human chốt tại Gate 1)*: ...
- **Non-goals (`YAGNI`)**: Những thứ tuyệt đối không làm trong ticket này.
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Sau khi tạo xong `jira.md` và `plan.md`, tóm tắt ngắn gọn kế hoạch (kèm Open Questions nếu có), in hộp dưới đây rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

```markdown
---
### ⏩ Bước tiếp theo: Triển Khai Mã Nguồn (Bước 2 — Implement)
- **🤖 Model khuyến nghị trên Antigravity**: **Claude Sonnet 5.5 (High / Medium)** hoặc **Orchestrator (Carbon)** / **Gemini 4 Argon (High)** *(nếu xử lý Lua CAS concurrency, AST splice hoặc LangGraph phức tạp dùng **Gemini 4 Carbon (Medium/High)** hoặc **Claude Opus 5.5 (Medium/High)**)* — *Tốc độ sửa nhiều file + chạy vòng lặp TDD `uv run` nhanh, chuẩn xác và tiết kiệm quota.*
- **💡 Mẹo ngữ cảnh**: Giữ nguyên cửa sổ chat hiện tại (hoặc đổi Model trên thanh chọn rồi gửi câu lệnh dưới).

Nếu bạn đồng ý với kế hoạch trên, hãy copy câu lệnh dưới đây để bắt đầu implement:
> `Duyệt plan. Thực hiện Bước 2 (Implement) cho task <TICKET_ID> theo .ai-workflow/prompts/02-implement.md và các skills trong blueprint của plan.md.`
```

---

## Hard Rules
- Chỉ tạo `jira.md`, chuyển Jira sang `21` (`In Progress`), tạo nhánh `feat/ca-...` và ghi `plan.md`.
- Không tạo hay sửa bất kỳ file nào trong `src/`, `tests/`, `alembic/`, `evals/`, `pyproject.toml`.
- Sau khi in hộp chuyển tiếp: **DỪNG LẠI NGAY LẬP TỨC (`END TURN`)**.
