# Shared Rules — CodeSight-AI Workflow (v4.0: `pstack` + Grok Bot + Antigravity Edition)

Quy tắc kỷ luật bắt buộc áp dụng cho toàn bộ quy trình `.ai-workflow` của **CodeSight-AI**, hợp nhất kiến trúc **4-Role Specialist của Lauren Tan (`pstack`)**, cơ chế **Token Economy của Grok Bot (`Wake-ups × Re-reads`)**, khả năng **Cross-Model + Subagents của Google Antigravity**, và cổng kiểm định cơ học Python `3.12+` / `uv`.

---

## 1. Thứ Tự Source of Truth & Luật "Fetch, Never Remember"

Khi đối chiếu thông tin, luôn ưu tiên từ trên xuống dưới và **truy xuất trực tiếp nguồn sống (fetch live)** thay vì dựa vào trí nhớ cũ:

1. **Live Jira Issue + Acceptance Criteria + Comments** (lấy từ Project **`CA`** qua `atlassian-mcp-server` / `jira-cwb`, đồng bộ tại `.ai-workflow/tasks/<TICKET_ID>/jira.md`).
2. **Architecture & Security Invariants trong `AGENTS.md`** & Schema/Models thực tế (`src/codesight/db/models.py`, `src/codesight/rag/models.py`):
   - **Zero-Code Persistence (`NFR-3`)**: Không bao giờ lưu mã nguồn thô xuống đĩa, DB, Qdrant payload hay OTel/Langfuse spans; mọi workspace tạm dọn sạch trong `finally` qua `src/codesight/core/ephemeral.py`.
   - **Latency & Quota Discipline**: Webhook `/api/v1/webhooks/github` xác thực `HMAC-SHA256` (`hmac.compare_digest`), chống trùng `X-GitHub-Delivery`, trả `202 Accepted` trong `< 100ms`; tổng E2E `< 25s`; `pull_request.opened` (`0s`) vs `pull_request.synchronize` (Cancel-on-Supersede + `5s` debounce); batch embedding `64` chunks + shared `asyncio.Semaphore(3)`.
   - **Zero `HTTP 422` & Syntax-Safe Suggestions**: Chỉ gắn comment inline vào `valid_commentable_lines` phía `RIGHT` cùng `hunk_id` (ngoài hunk hạ cấp xuống PR Summary); splice kiểm tra mọi khối ```` ```suggestion ```` trong RAM bằng `tree-sitter` (`not tree.root_node.has_error`).
   - **Host Port Coexistence**: Postgres `5433`, Redis `6381`, Qdrant `6333`/`6334`, FastAPI/Radar `8082`. **Tuyệt đối không** dừng hay đụng tới bất kỳ container `flintflow-*` hoặc `documind-*` nào (luôn dùng `docker compose -p codesight ...`).
3. **Approved Plan & Handoff State** (`.ai-workflow/tasks/<TICKET_ID>/plan.md` & `state.md`, kèm spec liên quan trong `docs/specs/`).
4. **Shared Persistent Memory** (`.ai-workflow/MEMORY.md`): Các quyết định kiến trúc (`Decisions`), file mẫu chuẩn (`Sources`), và bài học tổng quát (`Lessons`).
5. **Agent Assumptions**: Mức thấp nhất; phải ghi rõ trong báo cáo.

---

## 2. Kiến Trúc 4 Vai Trò & Kỷ Luật `1 Lượt = 1 Bước` (Single-Step Gate)

Mỗi lượt chỉ đóng **đúng 1 vai trò cho 1 bước**, ghi đúng artifact + cập nhật `state.md` + chạy `python .ai-workflow/scripts/verify.py board`, in Hộp Chuyển Tiếp rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

| Bước | Vai trò (`pstack` Role) | Đầu ra bắt buộc trong `tasks/<TICKET_ID>/` | Ranh giới dừng (Undo-Priced Gate) |
| :--- | :--- | :--- | :--- |
| **Bước 1: Plan** | `Chief of Staff` + `Researcher` (subagent) | `jira.md`, `plan.md`, `state.md` | Jira `21` (`In Progress`), tạo nhánh `feat/ca-...` từ `main`; không sửa `src/`, `tests/`, `alembic/` |
| **Bước 2: Implement** | `Engineer` *(hoặc tách Pipeline/Surface Specialists)* | `implementation-report.md`, `state.md` | Code + TDD + `verify.py check` xanh; không `git commit`, không tự review |
| **Bước 3: Review** | `Reviewer / Checker` (3 `research` subagents) | `review-01.md` (`FAILED/SAW/FIX`), `state.md` | Chỉ đọc & kiểm chứng độc lập; không sửa bất kỳ file code/test/docs nào |
| **Bước 4: Fix** | `Engineer` | `fix-report.md`, `state.md` | Vá đúng root-cause + regression tests + `verify.py check`; không mở rộng scope |
| **Bước 5: Final Review** | `Final Checker` + `OPS` Prevention Auditor | `final-review.md`, `state.md` | Jira `31` (`Verify / Test`) + chạy thật `verify.py check --full-format --preclose` + Prevention Audit |
| **Bước 6: Pre-Commit** | `Deliver & Memory Keeper` | `precommit.md`, `state.md` (+ `MEMORY.md`) | Soạn PR + khung Evidence Jira + chắt lọc `MEMORY.md`; **dừng tại `The Undo Gate` trước `git commit`** |
| **Bước 7: Close Jira** | `Jira Release Closer` | Cập nhật `precommit.md` & `state.md` (`Done`) | Sau khi Human `git commit`: lấy `<short-sha>` thật + `preclose_audit.py` + đăng Evidence 5 mục + Jira `41` (`Done`) |

---

## 3. Gói Bàn Giao Gọn `state.md` & Chống Đọc Lại Lịch Sử Chat (`Re-read Tax`)

Để tránh việc mỗi bước sau phải đọc lại hàng trăm nghìn token lịch sử chat cũ hoặc hàng chục KB văn xuôi:

1. **Mọi bước từ 1 đến 7 phải tạo/cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md` (tối đa 20 dòng)** theo đúng 5 trạng thái chuẩn của `THE BOARD` (`Todo` | `Working` | `Blocked` | `Ready for Review` | `Done`):
   ```markdown
   # Handoff State: <TICKET_ID>

   - **Status**: <Todo | Working | Blocked | Ready for Review | Done>
   - **Step**: <01-plan | 02-implement | 03-review | 04-fix | 05-final-review | 06-precommit | 07-close-jira>
   - **Branch**: `feat/ca-<number>-<short-slug>`
   - **Objective**: <1 câu mục tiêu cốt lõi của ticket>
   - **Canonical Examples**: `<src/codesight/...>`, `<tests/...>`
   - **Changed Files**: `<danh sách gọn các file M/A/D/?? hiện tại hoặc "none">`
   - **Key Decisions**: <1–2 dòng quyết định kỹ thuật quan trọng nhất>
   - **Strikes**: <0 | 1 | 2>
   - **Open Findings**: <Danh sách mã lỗi chưa đóng, vd: R1-01, R1-02 hoặc "none">
   - **Next Gate**: <Hành động tiếp theo cần thực hiện>
   ```
   *(Tương thích ngược: Nếu ticket khởi tạo từ bản v3.0 chưa có `state.md`, bước hiện tại tự tạo mới `state.md` khi kết thúc bước).*
2. **Đồng bộ Bảng Công Việc (`.ai-workflow/BOARD.md`)**: Cuối mỗi bước, chạy `python .ai-workflow/scripts/verify.py board` (mất `<0.1s`, tốn `0` token suy luận; `.ai-workflow/BOARD.md` và `tasks/*` đã được `.gitignore` bảo vệ).
3. **Chiến lược Cắt Lịch Sử Chat (Context Hygiene)**:
   - Khuyến khích mở **New Chat sạch** (hoặc gõ `/compact`) ở **mọi ranh giới đổi vai trò**: Bước 1 → Bước 2 (`Researcher` → `Engineer`), Bước 2 → Bước 3 (`Engineer` → `Checker`), Bước 3 → Bước 4 (`Checker` → `Engineer`), Bước 4 → Bước 5 (`Engineer` → `Final Checker`). Nhờ `state.md` + `plan.md` / `review-01.md` gọn sắc, cửa sổ mới nắm đủ 100% trạng thái mà giảm 50%–75% lượng token đọc lại mỗi vòng gọi tool.

---

## 4. Công Cụ Kiểm Chứng 1 Lệnh (`verify.py` — Script Before Reasoning)

Thay vì gọi rời rạc 6–7 lệnh terminal làm phình context bởi log thô, ưu tiên tuyệt đối chạy CLI kiểm chứng xác định tại `.ai-workflow/scripts/verify.py` (Python `3.12+` stdlib, `0` thư viện ngoài; cấm tuyệt đối `pip install` toàn cục):

1. **Thu thập phạm vi diff + file mới `??` + quét vệ sinh mã nguồn & Docker ports (Bước 3, 5, 6)**:
   ```powershell
   python .ai-workflow/scripts/verify.py diff --ticket <TICKET_ID>
   ```
   *(Tự động kiểm tra tên nhánh chuẩn `^feat/ca-\d+-[a-z0-9][a-z0-9-]*$`, xác nhận `tasks/*` & `BOARD.md` được gitignore, kiểm tra Docker port coexistence `FlintFlow`/`DocuMind`, liệt kê cả tracked diff lẫn file mới untracked `??`, chạy `git diff HEAD --check`, phát hiện `.env*` thật, `*.pem`/`*.key`, `*.log`, `breakpoint()`, `print(` trần trong `src/codesight/`, `@pytest.mark.skip` không điều kiện và secret patterns).*
   - **Lưu ý quan trọng khi Review**: Vì Bước 2 và Bước 4 chưa commit nên `HEAD` vẫn trùng gốc nhánh — luôn kết hợp `verify.py diff` + `git diff HEAD` + đọc trực tiếp các file mới `??` (tuyệt đối không dùng diff ba chấm `git diff main...HEAD` vì sẽ ra rỗng).
2. **Chạy trọn bộ Quality Gates với bảng tóm tắt gọn (Bước 2, 4, 5)**:
   ```powershell
   # Khi không chạm DB Postgres / Redis / Qdrant / Worker:
   python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID>

   # Khi chạm Postgres (5433) / Redis (6381) / Qdrant (6333-6334) / Worker / Alembic:
   python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --integration tests/integration/<suite>.py

   # Tại Bước 5 (Final Review — quét Ruff format toàn repo + cổng preclose_audit.py):
   python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --full-format --preclose [--integration all]
   ```
   - `verify.py check` tự động chạy thêm `audit_qdrant_zerocode.py --self-test` khi chạm `ast_engine/` hoặc `rag/`, và `validate_diff_and_payload.py --self-test` khi chạm `diff/`, `agents/` hoặc `github_publisher.py`.
   - Với Golden PR Benchmark (`CA-23`..`CA-28`): chạy thêm `python .agents/skills/codesight-kpi-benchmark/scripts/check_kpi_gates.py --report evals/results/latest_benchmark.json` *(thêm `--require-live` khi chốt Milestone 4)*.
   - Nếu ticket chạm UI `/radar` / Playwright E2E: luôn chạy ở **chế độ hiển thị trình duyệt (`--headed` / `headless: false`)** qua `--e2e <spec>`.
3. **Không bao giờ báo `PASS` cho cổng chưa chạy thực tế.** Nếu Docker stack `codesight` chưa bật khi cần integration tests, khởi động cô lập bằng `docker compose -p codesight up -d` (hoặc báo rõ `BLOCKED` nếu thiếu daemon).

---

## 5. Cầu Chì `2-Strike Blocked` & Cổng Phê Duyệt Theo Chi Phí Hoàn Tác (`The Undo Gate`)

1. **2-Strike Circuit Breaker (Chống lặp vô tận)**:
   - Trong cùng một bước (Bước 2 hoặc Bước 4): nếu cùng một cách sửa hoặc bài test thất bại **2 lần liên tiếp** theo cùng một kiểu mà chưa tìm ra nguyên nhân gốc, dừng thử mù lần 3.
   - Giữa các vòng Review ↔ Fix: mỗi lần `final-review.md` trả về `CHANGES REQUESTED` trên cùng một lỗi cũ, tăng `Strikes` trong `state.md` thêm `1`. Khi **`Strikes >= 2`**, chuyển `Status: Blocked` trong `state.md`, chạy `python .ai-workflow/scripts/verify.py board` và báo ngay cho Human kèm 2 phương án xử lý rõ ràng.
2. **Gate Priced by Undo Cost**:
   - **Tự hoàn tất 100% bước khả nghịch**: khảo sát code, chuyển Jira `21`/`31`, viết test/code local, chạy `verify.py`, tạo báo cáo, soạn PR description & khung Verification Evidence.
   - **Dừng hỏi trước bước khó/không thể hoàn tác**: `git commit`, `git push`, đóng Jira `41` (`Done`) khi chưa có `<short-sha>` thật, tạo Alembic migration hoặc `uv add` package ngoài `plan.md` đã duyệt, hoặc khi AC trên Jira mâu thuẫn với `AGENTS.md`.

---

## 6. Chuẩn Quản Lý Jira `CA-*` & Nhánh Git Tự Động

1. **Ranh giới Không gian Jira & Cú pháp MCP (`atlassian-mcp-server` / `jira-cwb`)**:
   - Chỉ thao tác trên Jira Project **`CA`** (`Board ID 74`, site `nguyentien191004.atlassian.net`). **Nghiêm cấm tuyệt đối** đọc/ghi hoặc chuyển trạng thái các ticket thuộc `DA` (`DocuMind-AI`) hay `CWB` (`Co-Workspace Booking`).
   - Luôn đọc file schema `.json` trong `<appDataDir>/mcp/<serverName>/` trước khi gọi `call_mcp_tool`:
     - **Mặc định trên Antigravity (`atlassian-mcp-server`, `cloudId: "nguyentien191004.atlassian.net"`)**: `getJiraIssue(cloudId, issueIdOrKey="CA-X", view="full")`, `listJiraIssueComments(cloudId, issueIdOrKey="CA-X")`, `transitionJiraIssue(cloudId, issueIdOrKey="CA-X", transitionId="21" | "31" | "41")`, `addOrEditJiraIssueComment(cloudId, issueIdOrKey="CA-X", commentBody="<markdown>")` *(để trống `commentId` khi tạo mới)*.
     - **Dự phòng khi mount `jira-cwb`**: `jira_get_issue`, `jira_transition_issue`, `jira_add_comment`.
2. **Chuẩn Tạo Nhánh Bắt Buộc Từ `main` (Bước 1)**:
   - Chạy `git status --porcelain`. Nếu có thay đổi chưa commit từ công việc khác ➔ **DỪNG LẠI BÁO HUMAN**, không tự ý stash/reset/checkout đè.
   - Mọi ticket `CA-*` bắt buộc dùng đúng regex **`^feat/ca-\d+-[a-z0-9][a-z0-9-]*$`** (không dùng `master`, `codex/`, `feature/`, `bugfix/`, `chore/`):
     ```powershell
     git checkout main
     git pull --ff-only
     git checkout -b feat/ca-<number>-<short-slug>
     ```
   - *(Nếu nhánh `feat/ca-<number>-...` của đúng ticket đó đã tồn tại và working tree sạch, `git checkout <branch-name>`).*

---

## 7. Mẫu Hộp Gợi Ý Chuyển Tiếp Bắt Buộc (Mandatory Hand-off Box)

Cuối mỗi lượt từ Bước 1 đến Bước 6, in đúng mẫu hộp sau:

```markdown
---

### ⏩ Bước tiếp theo: [Tên bước & Vai trò kế tiếp]

- **🤖 Model khuyến nghị trên Antigravity**: **[Model ưu tiên 1]** *(hoặc [Model thay thế])* — *[Lý do 1 câu]*
- **💡 Vệ sinh ngữ cảnh (Chống Re-read Tax)**: [Khuyên mở **New Chat sạch** (hoặc chạy `/compact`) rồi dán câu lệnh dưới — trạng thái đã lưu trọn trong `state.md`]

Copy câu lệnh dưới đây và gửi vào ô chat:

> `[Câu prompt tự thân đầy đủ chứa mã <TICKET_ID> và đường dẫn .ai-workflow/prompts/...]`
```
