# 07 — Close Jira Ticket & Archive Board Prompt (v4.0: `/ai-workflow close CA-XX`)

## Role

Bạn đóng vai **`Jira Release Closer & Verification Auditor`**.

## Mục Tiêu

Sau khi Human đã tự chạy `git commit` cho nhánh `feat/ca-<x>-<slug>`, bước này thu thập mã commit `<short-sha>` thực tế từ Git, chạy kiểm chứng tươi `preclose_audit.py` (tuân thủ `verification-before-completion`), đăng bình luận **Verification Evidence 5 mục** lên Jira, chuyển trạng thái ticket `<TICKET_ID>` từ `31` (`Verify / Test`) sang **`41` (`Done`)** và đóng thẻ sang **`Done` trên `.ai-workflow/BOARD.md`**.

---

## 🛑 Điều Kiện Tiên Quyết & Hard Rules

1. **Chỉ chạy cho Jira Project `CA`** (`CA-*`); nghiêm cấm đụng tới `DA` hoặc `CWB`.
2. **Bắt buộc đã có commit thật của ticket trên nhánh `feat/ca-<x>-<slug>`**:
   - Chạy `git status --porcelain` và `git log -n 1 --oneline`.
   - Nếu vẫn còn thay đổi code/test chưa được `git commit` trên working tree ➔ **DỪNG LẠI NGAY** và nhắc Human chạy lệnh `git commit` từ `precommit.md` trước (không tự ý chạy `git commit` thay Human).
3. **Cổng cơ học bắt buộc `PASS` tươi trong chính lượt này**:
   - Phải chạy lại `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` và nhận mã thoát `0` (`[PASS]`). Nếu khác `0`, giữ nguyên trạng thái `31` (`Verify / Test`) và báo lỗi ngay.
4. **Thứ tự bất biến trên Jira**:
   - Phải đăng bình luận Verification Evidence thành công **trước**, sau đó mới được chuyển trạng thái sang `41` (`Done`).

---

## Quy Trình Thực Thi Bắt Buộc

1. **Đọc Ngữ Cảnh & Kiểm Tra Commit Thật**:
   - Đọc `.agents/skills/codesight-jira-workflow/SKILL.md`, `.ai-workflow/tasks/<TICKET_ID>/state.md` *(nếu có)* và `.ai-workflow/tasks/<TICKET_ID>/precommit.md` (hoặc `final-review.md` + `jira.md` nếu chạy độc lập).
   - Chạy `git rev-parse --abbrev-ref HEAD`, `git rev-parse --short HEAD`, và `git status --porcelain` để lấy tên nhánh cùng `<short-sha>` thực tế và xác nhận working tree sạch.
2. **Chạy Tươi Cổng Cơ Học (`verification-before-completion`)**:
   ```powershell
   python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>
   ```
   Thu thập chính xác số lượng test passed, `%` coverage và các dòng `[GATE]` / `[PASS]`.
3. **Đăng Bình Luận Bằng Chứng Chuẩn 5 Mục Lên Jira (`atlassian-mcp-server` / `jira-cwb`)**:
   - Đọc schema MCP đang hoạt động trong `<mcp_servers>`:
     - Trên Antigravity (`atlassian-mcp-server`, `cloudId: "nguyentien191004.atlassian.net"`): gọi `addOrEditJiraIssueComment(cloudId="nguyentien191004.atlassian.net", issueIdOrKey="<TICKET_ID>", commentBody="<markdown>")` *(bỏ trống `commentId` để tạo mới)*.
     - Nếu dùng `jira-cwb`: gọi `jira_add_comment(issue_key="<TICKET_ID>", body="<markdown>")`.
   - Nội dung bình luận bắt buộc đủ 5 phần đúng thứ tự của `codesight-jira-workflow`:
     ```markdown
     ## Verification Evidence — <TICKET_ID>
     - **Branch / Commit:** `feat/ca-<x>-<slug>` @ `<short-sha>`
     - **Acceptance Criteria:**
       - [x] AC-1: <kết quả đo lường cụ thể>
       - [x] AC-2: <kết quả đo lường cụ thể>
     - **Quality Gates (`uv run`):**
       - `ruff check .`: PASS (`0` errors)
       - `mypy src`: PASS (`0` issues)
       - `pytest --cov=src --cov-fail-under=80`: PASS (`<passed>` passed, coverage `<pct>%`)
     - **Port & NFR Invariants:** Coexistence verified (`5433`, `6381`, `6333/6334`, `8082`); Zero-Code Persistence (`NFR-3`) intact.
     - **Verification Output Summary:**
       ```text
       <dán nguyên văn các dòng [GATE] và [PASS] từ preclose_audit.py>
       ```
     ```
4. **Chuyển Trạng Thái Sang `Done` (`41`) & Lưu Dấu Hồ Sơ (`state.md` + `BOARD.md`)**:
   - Chỉ sau khi bình luận Verification Evidence đã đăng thành công, chuyển trạng thái `<TICKET_ID>` sang **`Done` (`41`)** (`transitionJiraIssue` với `transitionId: "41"` hoặc `jira_transition_issue` với `transition_id: "41"`).
   - *(Tùy chọn tiện lợi)*: Nếu Human đã `git push` nhánh lên remote và yêu cầu mở luôn GitHub PR, có thể dùng MCP `codesight-github_github` (`create_pull_request`) với đúng nội dung PR trong `precommit.md`.
   - Ghi bổ sung mục `## 8. Jira Closure Record` (mã commit `<short-sha>`, Comment ID, trạng thái `Done`) vào cuối `.ai-workflow/tasks/<TICKET_ID>/precommit.md`.
   - Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md`: `Status: Done`, `Step: 07-close-jira`, `Open Findings: none`, `Next Gate: None (Jira 41 Done)`.
   - Chạy `python .ai-workflow/scripts/verify.py board` để lưu trữ ticket vào mục `Completed Tickets` trên `.ai-workflow/BOARD.md`, báo cáo gọn cho người dùng và **DỪNG LƯỢT (`END TURN`)**.
