---
name: ai-workflow
description: Use when the user invokes /ai-workflow, starts or closes a Jira CA-* ticket ("Làm task CA-XX", "/ai-workflow CA-XX", "/ai-workflow close CA-XX"), or sends a workflow hand-off prompt referencing plan.md or 01-plan.md through 07-close-jira.md.
---

# CodeSight-AI — AI Workflow Dispatcher (v3.0)

Router skill điều phối quy trình phát triển theo Jira ticket `CA-*` của dự án **CodeSight-AI** trên **Google Antigravity**.

## 1. Nạp Quy Tắc Nền (Bắt Buộc Mọi Lượt)

Trước khi thực hiện bất kỳ thao tác nào:
1. Đọc [`.ai-workflow/prompts/00-shared-rules.md`](../../../.ai-workflow/prompts/00-shared-rules.md) để nắm kỷ luật **`1 Lượt = 1 Bước` (Hard Stop)**, chuẩn tạo nhánh `feat/ca-<x>-<slug>` từ `main`, quy tắc đọc diff chưa commit, bộ cổng `uv run` + `preclose_audit.py`, và mẫu **Hộp Gợi Ý Chuyển Tiếp Kèm Model Antigravity**.
2. Tuân thủ tuyệt đối mọi kiến trúc & ràng buộc bảo mật trong [`AGENTS.md`](../../../AGENTS.md) (Zero-Code Persistence `NFR-3`, `< 100ms` Webhook ACK, Cancel-on-Supersede, Zero `HTTP 422`, cô lập port Docker `FlintFlow`/`DocuMind`).

## 2. Định Tuyến Bước Thực Thi (Chọn Đúng 1 Bước Cho Lượt Hiện Tại)

Xác định đúng **1 bước duy nhất** dựa trên câu lệnh người dùng (hoặc trạng thái các tệp hiện có trong `.ai-workflow/tasks/<TICKET_ID>/` nếu người dùng chỉ gõ `/ai-workflow CA-XX`):

| Tín hiệu từ người dùng / Trạng thái file | Bước cần chạy | File hướng dẫn bắt buộc đọc | Output duy nhất của lượt |
| :--- | :--- | :--- | :--- |
| Khởi động ticket mới (`/ai-workflow CA-XX`, `"Làm task CA-XX"`, chưa có `plan.md`) | **Bước 1: Plan** | [`.ai-workflow/prompts/01-plan.md`](../../../.ai-workflow/prompts/01-plan.md) | `jira.md` + Jira `21` (`In Progress`) + nhánh `feat/ca-...` từ `main` + `plan.md` |
| Duyệt plan / Yêu cầu triển khai (`"Duyệt plan..."`, `02-implement.md`) | **Bước 2: Implement** | [`.ai-workflow/prompts/02-implement.md`](../../../.ai-workflow/prompts/02-implement.md) | Code + Tests (`TDD`) + `implementation-report.md` |
| Yêu cầu review độc lập (`"Thực hiện Bước 3..."`, `03-review.md`) | **Bước 3: Review** | [`.ai-workflow/prompts/03-review.md`](../../../.ai-workflow/prompts/03-review.md) + [`domain-review.md`](../../../.ai-workflow/rules/domain-review.md) | `review-01.md` *(tuyệt đối không sửa code)* |
| Yêu cầu sửa findings (`"Thực hiện Bước 4..."`, `04-fix.md`) | **Bước 4: Fix** | [`.ai-workflow/prompts/04-fix.md`](../../../.ai-workflow/prompts/04-fix.md) | Code vá lỗi + Regression tests + `fix-report.md` |
| Yêu cầu tái thẩm định (`"Thực hiện Bước 5..."`, `05-final-review.md`) | **Bước 5: Final Review** | [`.ai-workflow/prompts/05-final-review.md`](../../../.ai-workflow/prompts/05-final-review.md) + [`domain-review.md`](../../../.ai-workflow/rules/domain-review.md) | Jira `31` (`Verify / Test`) + chạy `preclose_audit.py` + `final-review.md` *(không sửa code)* |
| Yêu cầu pre-commit / tạo PR (`"Thực hiện Bước 6..."`, `06-precommit.md`) | **Bước 6: Pre-Commit** | [`.ai-workflow/prompts/06-precommit.md`](../../../.ai-workflow/prompts/06-precommit.md) | `precommit.md` (PR + Commit + khung Evidence) + in khối lệnh Git cho Human |
| Chốt đóng Jira sau khi Human đã commit (`/ai-workflow close CA-XX`, `07-close-jira.md`) | **Bước 7: Close Jira** | [`.ai-workflow/prompts/07-close-jira.md`](../../../.ai-workflow/prompts/07-close-jira.md) | Xác thực `<short-sha>` + `preclose_audit.py` + đăng `jira_add_comment` (5 mục) + Jira `41` (`Done`) |

## 3. Tiêu Chuẩn Hoàn Tất Lượt (Completion Criterion)

Mỗi lượt **bắt buộc kết thúc ngay lập tức** khi:
1. Artifact tương ứng của bước đó đã được ghi vào `.ai-workflow/tasks/<TICKET_ID>/`.
2. Đã in **Hộp Gợi Ý Bước Tiếp Theo** (gồm khuyến nghị **Model trên Antigravity** và câu lệnh copy-paste tự thân đầy đủ) ở cuối câu trả lời.
3. **Dừng lượt ngay lập tức (`END TURN`)** — tuyệt đối không tự ý chạy lấn sang bước kế tiếp.
