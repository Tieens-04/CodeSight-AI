# Shared Rules — CodeSight-AI Workflow (v3.0: Antigravity & Cross-Model Edition)

Quy tắc kỷ luật bắt buộc áp dụng cho toàn bộ quy trình `.ai-workflow` của **CodeSight-AI**. Thiết kế chuyên biệt cho **Google Antigravity** (phối hợp đa Model **Gemini 4 / Orchestrator** & **Claude 5.5**, Subagents độc lập, MCP `jira-cwb`, Python `3.12+` / `uv` và các cổng kiểm tra cơ học).

---

## 1. Thứ Tự Ưu Tiên Source of Truth

Khi có xung đột thông tin, luôn ưu tiên từ trên xuống dưới:
1. **Jira Issue Description + Acceptance Criteria + Product Comments** (lấy từ Project **`CA`** qua `jira-cwb`, lưu tại `.ai-workflow/tasks/<TICKET_ID>/jira.md`).
2. **Architecture & Security Invariants trong `AGENTS.md`**:
   - **Zero-Code Persistence (`NFR-3`)**: Không bao giờ lưu mã nguồn thô xuống ổ cứng hoặc payload Qdrant; mọi workspace tạm bắt buộc dọn sạch trong `finally`.
   - **Latency & Quota Discipline**: Webhook `/api/v1/webhooks/github` xác thực `HMAC-SHA256` (`hmac.compare_digest`), chống trùng `X-GitHub-Delivery`, trả `202 Accepted` trong `< 100ms`; tổng E2E `< 25s`; `pull_request.opened` (`0s`) vs `pull_request.synchronize` (Cancel-on-Supersede + `5s` debounce); batch embedding `64` chunks + `asyncio.Semaphore(3)`.
   - **Zero `HTTP 422` & Syntax-Safe Suggestions**: Chỉ gắn comment inline vào `valid_commentable_lines` phía `RIGHT` trong cùng 1 hunk (ngoài hunk đẩy xuống PR Summary); kiểm tra mọi khối ```` ```suggestion ```` trong RAM bằng `tree-sitter` (`not tree.root_node.has_error`).
   - **Host Port Coexistence**: Postgres `5433`, Redis `6381`, Qdrant `6333`/`6334`, FastAPI/Radar `8082`. **Tuyệt đối không** dừng hay đụng tới bất kỳ container `flintflow-*` hoặc `documind-*` nào (luôn dùng `-p codesight` khi chạy Docker Compose).
3. **Approved Implementation Plan** (`.ai-workflow/tasks/<TICKET_ID>/plan.md` hoặc spec liên quan trong `docs/specs/`).
4. **Agent Assumptions**: Mức ưu tiên thấp nhất; bắt buộc ghi rõ trong báo cáo.

---

## 2. Kỷ Luật `1 Lượt = 1 Bước` (Single-Step Execution Gate)

Mỗi lượt trả lời của Agent **chỉ được thực thi đúng 1 bước** mà người dùng vừa gọi, ghi đúng artifact vào `.ai-workflow/tasks/<TICKET_ID>/`, in hộp câu lệnh chuyển tiếp rồi **kết thúc lượt ngay lập tức (`END TURN`)**:

| Bước | Hành động được phép | Artifact bắt buộc trong `.ai-workflow/tasks/<TICKET_ID>/` | Ranh giới dừng (Hard Stop) |
| :--- | :--- | :--- | :--- |
| **Bước 1: Plan** | Kéo Jira `CA-*`, chuyển Jira sang `21` (`In Progress`), tạo nhánh `feat/ca-...` từ `main`, khảo sát code, lập kế hoạch | `jira.md`, `plan.md` | Không sửa `src/`, `tests/`, `alembic/`; dừng chờ Gate 1 |
| **Bước 2: Implement** | Viết tests (`TDD`) + code theo `plan.md`, chạy bộ kiểm tra `uv run` | `implementation-report.md` | Không `git commit` / `git push`, không tự tạo `review-01.md`; dừng chờ Gate 2 |
| **Bước 3: Review** | Khởi chạy 3 Subagents độc lập đọc `git diff HEAD` + file untracked `??` | `review-01.md` | Tuyệt đối không sửa bất kỳ file code/test/docs nào; dừng chờ Gate 3 |
| **Bước 4: Fix** | Vá tận gốc các findings đã xác nhận + bổ sung regression tests | `fix-report.md` | Không mở rộng scope, không `git commit`, không tự tạo `final-review.md` |
| **Bước 5: Final Review** | Chuyển Jira sang `31` (`Verify / Test`), đối chiếu findings, chạy thật `uv run` & `preclose_audit.py` | `final-review.md` | Không sửa code, không `git commit`; dừng chờ Gate 4 |
| **Bước 6: Pre-Commit** | Quét working tree, secret & port coexistence, dùng skill `pr` soạn PR + khung Verification Evidence | `precommit.md` | Không tự `git commit` / `git push`; giữ Jira ở `31` và in lệnh Git cho Human |
| **Bước 7: Close Jira** *(Tùy chọn sau commit)* | Lấy `<short-sha>` thật từ `HEAD`, chạy `preclose_audit.py`, đăng comment 5 mục & chuyển Jira sang `41` (`Done`) | Cập nhật mục chốt đóng trong `precommit.md` | Chỉ chạy khi Human gõ `/ai-workflow close CA-XX` sau khi đã `git commit` |

---

## 3. Hộp Gợi Ý Chuyển Tiếp Kèm Khuyến Nghị Model (Mandatory Hand-off Box)

Ở cuối **mọi** câu trả lời khi kết thúc bước (từ Bước 1 đến Bước 6), Agent bắt buộc in hộp chuyển tiếp theo đúng mẫu sau để người dùng chọn ngay **Model phù hợp trên thanh Model Selection của Antigravity**:

```markdown
---
### ⏩ Bước tiếp theo: [Tên bước tiếp theo]
- **🤖 Model khuyến nghị trên Antigravity**: **[Model ưu tiên 1]** *(hoặc [Model thay thế])* — *[Lý do 1 câu ngắn gọn]*
- **💡 Mẹo ngữ cảnh**: [Giữ nguyên cửa sổ chat hiện tại / Khuyên mở New Chat sạch để review khách quan]

Copy câu lệnh dưới đây và gửi vào ô chat:
> `[Câu prompt tự thân đầy đủ chứa mã <TICKET_ID> và đường dẫn .ai-workflow/prompts/...]`
```

---

## 4. Quy Tắc Đọc Thay Đổi Chưa Commit Khi Review (Working-Tree Diff Rule)

Vì Bước 2 và Bước 4 **không tạo `git commit`**, tại Bước 3 (`03-review.md`) và Bước 5 (`05-final-review.md`), `HEAD` vẫn trùng với commit gốc của nhánh `main` (hoặc commit đầu nhánh). Reviewer và cả 3 Subagents bắt buộc thu thập thay đổi theo 3 bước:
1. Chạy `git status --short` để liệt kê toàn bộ các tệp đã sửa (`M`, `A`, `D`) **và các tệp mới chưa theo dõi (`??`)** *(lưu ý `.ai-workflow/tasks/` đã được `.gitignore` che tự động)*.
2. Chạy `git diff HEAD` để đọc toàn bộ thay đổi của các tệp đã tracked *(không bao giờ dùng diff ba chấm `git diff main...HEAD` khi chưa commit vì sẽ ra rỗng)*.
3. Đọc trực tiếp toàn bộ nội dung mọi tệp mới untracked (`??`) thuộc `src/`, `tests/`, `alembic/`, `evals/`, `docs/` để không bỏ sót bất kỳ dòng code mới nào.

---

## 5. Chuẩn Quản Lý Jira `CA-*` & Nhánh Git Tự Động

1. **Ranh giới Không gian Jira & Cú pháp MCP (`atlassian-mcp-server` / `jira-cwb`)**:
   - Chỉ thao tác trên Jira Project **`CA`** (`Board ID 74`, site `nguyentien191004.atlassian.net`). **Nghiêm cấm tuyệt đối** đọc/ghi hoặc chuyển trạng thái các ticket thuộc `DA` (`DocuMind-AI`) hay `CWB` (`Co-Workspace Booking`).
   - Kiểm tra danh sách `<mcp_servers>` đang hoạt động trong phiên hiện tại (luôn đọc file schema `.json` trong `<appDataDir>/mcp/<serverName>/` trước khi gọi `call_mcp_tool`):
     - **Mặc định trên Antigravity (`atlassian-mcp-server`)** với `cloudId: "nguyentien191004.atlassian.net"`:
       - Đọc ticket: `getJiraIssue(cloudId, issueIdOrKey="CA-X", view="full")` + `listJiraIssueComments(cloudId, issueIdOrKey="CA-X")` (nếu có comment).
       - Đổi trạng thái: `transitionJiraIssue(cloudId, issueIdOrKey="CA-X", transitionId="21" | "31" | "41")`.
       - Đăng bình luận: `addOrEditJiraIssueComment(cloudId, issueIdOrKey="CA-X", commentBody="<markdown>")` *(bỏ trống `commentId` để tạo mới)*.
     - **Dự phòng nếu phiên CLI mount `jira-cwb`**: `jira_get_issue(issue_key, include="comments,transitions")`, `jira_transition_issue(issue_key, transition_id)`, `jira_add_comment(issue_key, body)`.
2. **Kiểm tra Working Tree Sạch ở Bước 1**:
   - Chạy `git status --porcelain`. Nếu đang còn thay đổi chưa commit từ ticket khác ➔ **DỪNG LẠI & BÁO NGƯỜI DÙNG NGAY**, không tự ý `stash`, `reset` hay `checkout` đè.
3. **Chuẩn Tên Nhánh Bắt Buộc (`feat/ca-<number>-<short-slug>`)**:
   - Cả `AGENTS.md` lẫn cổng cơ học `.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py` đều kiểm tra cứng regex **`^feat/ca-\d+-[a-z0-9][a-z0-9-]*$`**.
   - Vì vậy **mọi ticket `CA-*`** đều dùng đúng định dạng `feat/ca-<số-viết-thường>-<slug-2-đến-4-từ>` (ví dụ: `feat/ca-8-github-blob-fetcher`), tạo từ nhánh gốc **`main`**:
     ```powershell
     git checkout main
     git pull --ff-only
     git checkout -b feat/ca-<number>-<short-slug>
     ```
   - *(Nếu nhánh `feat/ca-<number>-...` của đúng ticket đó đã tồn tại sẵn và working tree sạch, checkout trực tiếp vào nhánh đó).*
   - **Tuyệt đối không** dùng `master`, `codex/`, `feature/`, `chore/`, hay `bugfix/` cho nhánh ticket `CA-*`.

---

## 6. Bộ Cổng Kiểm Chứng Chuẩn Của CodeSight-AI (`uv` + Mechanical Gates)

Dự án dùng Python `3.12+` quản lý độc quyền qua **`uv`** (`pyproject.toml` + `uv.lock`; **cấm tuyệt đối `pip install` toàn cục**):

1. **Lint & Format Check (Bắt buộc mọi task sửa Python)**:
   - `uv run ruff check .`
   - `uv run ruff format --check <các-file-py-đã-sửa>` *(hoặc format gọn các file thuộc phạm vi ticket bằng `uv run ruff format <files>`)*
2. **Strict Typecheck (Bắt buộc)**:
   - `uv run mypy src`
3. **Unit Tests & Branch Coverage Gate `>= 80%` (Bắt buộc)**:
   - `uv run pytest --cov=src --cov-fail-under=80`
   - *(Lưu ý quan trọng: `pyproject.toml` bật `branch = true` trong `[tool.coverage.run]`, tức đo **Branch Coverage** trên `src/codesight` — cần viết test phủ đủ cả nhánh đúng và nhánh lỗi/fallback của câu lệnh rẽ nhánh).*
4. **Live Docker Integration Tests (Bắt buộc khi chạm PostgreSQL `5433`, Redis `6381`, Qdrant `6333/6334`, Alembic migrations, hoặc Celery/Lua locks)**:
   - Đảm bảo stack chạy cô lập bằng `docker compose -p codesight up -d` *(không bao giờ chạy `docker compose down` không có `-p codesight`)*.
   - Chạy: `uv run pytest -m integration`
5. **Cổng Kiểm Tra Cơ Học Theo Chuyên Đề (Chạy thực tế khi liên quan & ở Bước 5/6/7)**:
   - **Mọi ticket `CA-*` (Bước 5, 6, 7)**: `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>`
   - **AST / Embeddings / Qdrant (`CA-11`..`CA-16`)**: `python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test` *(kèm `--collection codesight_symbols` khi Qdrant đang chạy)*
   - **Diff Mapping / Suggestion / GitHub Review API (`CA-17`..`CA-22`)**: `python .agents/skills/github-diff-suggestion-guard/scripts/validate_diff_and_payload.py --self-test`
   - **Golden PR Benchmark & KPI Sign-off (`CA-23`..`CA-28`)**: `python .agents/skills/codesight-kpi-benchmark/scripts/check_kpi_gates.py --report evals/results/latest_benchmark.json` *(thêm `--require-live` khi chốt Milestone 4)*
   - **Playwright / `/radar` UI**: Bắt buộc chạy ở chế độ hiển thị trình duyệt (**`--headed` / `headless: false`**) theo quy tắc người dùng.

Không bao giờ báo `PASS` cho lệnh chưa chạy thật trong lượt đó.

---

## 7. Điều Kiện Dừng Khẩn Cấp (Stop Conditions)

Dừng ngay lập tức và hỏi ý kiến người dùng khi gặp một trong các tình huống:
1. Jira Acceptance Criteria mâu thuẫn nội tại hoặc vi phạm trực tiếp các bất biến trong `AGENTS.md` (`NFR-3`, SLA `< 25s` / `< 100ms`, giới hạn port Docker).
2. Phát hiện cần tạo Alembic migration mới hoặc thêm package vào `pyproject.toml` (`uv add`) **ngoài** `plan.md` đã duyệt.
3. Working tree đang bẩn bởi thay đổi của một ticket khác khi bắt đầu Bước 1.
4. Bất kỳ cổng kiểm tra nào (`ruff`, `mypy`, `pytest --cov`, `preclose_audit.py`) đang `FAIL` thực sự — tuyệt đối không lách luật hay chuyển sang bước đóng ticket khi cổng kiểm tra còn đỏ.
