---
name: ai-workflow
description: Use when the user invokes /ai-workflow, "/ai-workflow board", "/ai-workflow status", "/ai-workflow close CA-XX", starts or resumes a Jira CA-* ticket ("Làm task CA-XX"), or sends a workflow hand-off prompt referencing state.md, plan.md, or 01-plan.md through 07-close-jira.md.
---

# CodeSight-AI — AI Workflow Chief of Staff (v4.0)

Router skill (`Chief of Staff`) điều phối quy trình phát triển theo Jira ticket `CA-*` của **CodeSight-AI** trên **Google Antigravity** — hợp nhất kiến trúc **4-Role Specialist (`pstack`)**, cơ chế **Token Economy (`Grok Bot`)** và chu trình **Jira `21 → 31 → 41`**.

## 1. Nạp Quy Tắc Nền (Bắt Buộc Trước Mọi Hành Động)

1. Đọc [`.ai-workflow/prompts/00-shared-rules.md`](../../../.ai-workflow/prompts/00-shared-rules.md) để nắm kỷ luật **`1 Lượt = 1 Bước` (Hard Stop)**, gói bàn giao gọn `state.md` (≤ 20 dòng), CLI kiểm chứng 1 lệnh `python .ai-workflow/scripts/verify.py`, cầu chì **`2-Strike Blocked`**, và mẫu **Hộp Chuyển Tiếp**.
2. Giữ nguyên vẹn 100% kiến trúc & ràng buộc bảo mật trong [`AGENTS.md`](../../../AGENTS.md) (`NFR-3` Zero-Code Persistence, `< 100ms` Webhook ACK, Cancel-on-Supersede, Zero `HTTP 422`, cô lập port Docker `FlintFlow`/`DocuMind`).

## 2. Định Tuyến Lượt Thực Thi (Chọn Đúng 1 Nhánh Cho Lượt Hiện Tại)

| Tín hiệu từ người dùng / Trạng thái `tasks/<TICKET_ID>/` | Vai trò & Bước kích hoạt | Tài liệu đọc thực thi | Đầu ra duy nhất của lượt |
| :--- | :--- | :--- | :--- |
| `/ai-workflow board` hoặc `/ai-workflow status` | **Chief of Staff: Board Sweep** | Chạy `python .ai-workflow/scripts/verify.py board` | Đồng bộ `.ai-workflow/BOARD.md` + tóm tắt ≤ 8 dòng các ticket đang mở/bị chặn |
| Ticket mới (`/ai-workflow CA-XX`, `"Làm task CA-XX"`, chưa có `plan.md`) | **Bước 1: Goal & Plan** (`Chief` + `Researcher`) | [`01-plan.md`](../../../.ai-workflow/prompts/01-plan.md) + [`MEMORY.md`](../../../.ai-workflow/MEMORY.md) | `jira.md` + Jira `21` (`In Progress`) + nhánh `feat/ca-...` từ `main` + `plan.md` + `state.md` + `BOARD.md` |
| Duyệt plan / Yêu cầu triển khai (`02-implement.md`) | **Bước 2: Build** (`Engineer`) | [`02-implement.md`](../../../.ai-workflow/prompts/02-implement.md) + [`MEMORY.md`](../../../.ai-workflow/MEMORY.md) | Code + Tests (`TDD`) + `verify.py check` + `implementation-report.md` + `state.md` + `BOARD.md` |
| Yêu cầu review độc lập (`03-review.md`) | **Bước 3: Review** (`Checker` + 3 Subagents) | [`03-review.md`](../../../.ai-workflow/prompts/03-review.md) + [`domain-review.md`](../../../.ai-workflow/rules/domain-review.md) | `review-01.md` (`FAILED/SAW/FIX`, không sửa code) + `state.md` + `BOARD.md` |
| Yêu cầu sửa findings (`04-fix.md`) | **Bước 4: Fix** (`Engineer`) | [`04-fix.md`](../../../.ai-workflow/prompts/04-fix.md) | Code vá gốc lỗi + Regression tests + `verify.py check` + `fix-report.md` + `state.md` + `BOARD.md` |
| Yêu cầu tái thẩm định (`05-final-review.md`) | **Bước 5: Final Gate** (`Final Checker` + `OPS`) | [`05-final-review.md`](../../../.ai-workflow/prompts/05-final-review.md) + [`domain-review.md`](../../../.ai-workflow/rules/domain-review.md) | Jira `31` (`Verify / Test`) + `verify.py check --full-format --preclose` + Prevention Audit + `final-review.md` + `state.md` |
| Yêu cầu bàn giao / phát hành (`06-precommit.md`) | **Bước 6: Deliver** (`6.1 Pre-commit` ➔ `6.2 Commit` ➔ `6.3 Push` ➔ `6.4 Tạo PR` ➔ `6.5 Đóng Jira "Done"`) | [`06-precommit.md`](../../../.ai-workflow/prompts/06-precommit.md) + [`MEMORY.md`](../../../.ai-workflow/MEMORY.md) | `precommit.md` + `MEMORY.md` (nếu có) + Commit + `preclose_audit.py` + Push + GitHub PR + Evidence 5 mục & Jira `41` (`Done`) + `state.md` + `BOARD.md` |
| Chốt đóng Jira độc lập (fallback cho 6.5 nếu đã commit thủ công: `/ai-workflow close CA-XX`, `07-close-jira.md`) | **Bước 6.5 Standalone: Close Jira** (`Release Closer`) | [`07-close-jira.md`](../../../.ai-workflow/prompts/07-close-jira.md) | Xác thực `<short-sha>` + `preclose_audit.py` + đăng Evidence 5 mục + Jira `41` (`Done`) + `state.md` (`Done`) + `BOARD.md` |

*(Nếu người dùng chỉ gõ `/ai-workflow CA-XX` cho một ticket đang làm dở đã có `plan.md`, đọc `.ai-workflow/tasks/<TICKET_ID>/state.md` — hoặc chạy `python .ai-workflow/scripts/verify.py board` nếu chưa có `state.md` — để báo trạng thái hiện tại và in ngay Hộp Chuyển Tiếp cho bước kế tiếp).*

## 3. Tiêu Chuẩn Hoàn Tất Lượt (Completion Criterion)

Lượt hiện tại **bắt buộc kết thúc ngay lập tức** khi:
1. Artifact bắt buộc của bước đó cùng `state.md` đã được ghi vào `.ai-workflow/tasks/<TICKET_ID>/`, và `python .ai-workflow/scripts/verify.py board` đã đồng bộ `.ai-workflow/BOARD.md`.
2. Đã in **Hộp Gợi Ý Bước Tiếp Theo** (gồm khuyến nghị **Model trên Antigravity**, mẹo cắt lịch sử chat chống `Re-read Tax`, và câu lệnh copy-paste tự thân đầy đủ).
3. **Dừng lượt ngay lập tức (`END TURN`)** — tuyệt đối không tự ý chạy lấn sang bước kế tiếp.
