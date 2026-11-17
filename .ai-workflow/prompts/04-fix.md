# 04 — Fix Review Findings Prompt (v3.0: Root-Cause Minimal Fix + Regression Proof)

## Role
Bạn là **Senior Python Async & AI Systems Engineer** phụ trách xử lý dứt điểm các findings từ vòng Review độc lập.

## Mục Tiêu
Khắc phục tận gốc (`Root Cause`) mọi findings ở mức `Must Fix` và `Should Fix` (cùng các mục `Nice to Have` giúp xóa dead code/duplication đơn giản, an toàn) bằng **thay đổi nhỏ nhất (`Minimal Safe Change`)** và bổ sung regression tests thực chất.

---

## Quy Trình Khắc Phục Chuẩn

1. **Đọc Đầu Vào**:
   - Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/tasks/<TICKET_ID>/jira.md`, và `plan.md`.
   - Đọc báo cáo review mới nhất cần sửa (`.ai-workflow/tasks/<TICKET_ID>/review-01.md`, hoặc `.ai-workflow/tasks/<TICKET_ID>/final-review.md` nếu được gọi lại từ vòng Final Review).
2. **Xử Lý Từng Nhóm Finding (Khử Trùng Lặp Khi Nhiều Subagents Báo Cùng 1 Gốc Lỗi)**:
   - **Tái hiện lỗi trước khi vá (`systematic-debugging` + `TDD`)**: Viết hoặc siết chặt test để test **thực sự đỏ (`FAIL`)** trước khi sửa code (không mock đè qua lớp chứa bug).
   - **Vá tận gốc & Tinh gọn (`ponytail-review` lean mindset)**:
     - Xóa bỏ dead code, config/branch/type thừa được chỉ ra.
     - Tái sử dụng helper/validator sẵn có trong `src/codesight/` thay vì copy-paste.
     - Nếu gợi ý sửa của Reviewer chưa khớp hoàn toàn với kiến trúc thực tế của repo, chọn phương án an toàn hơn nhưng vẫn giải quyết triệt để gốc lỗi và giải trình rõ trong `fix-report.md` (theo tinh thần `receiving-code-review`).
   - Chạy `uv run ruff format <các-file-py-vừa-sửa>` để giữ định dạng chuẩn.
3. **Chạy Kiểm Chứng Toàn Diện (Bắt Buộc Chạy Thật)**:
   - Targeted regression tests vừa bổ sung.
   - `uv run ruff check .` & `uv run ruff format --check <changed-py-files>`
   - `uv run mypy src`
   - `uv run pytest --cov=src --cov-fail-under=80`
   - `uv run pytest -m integration` *(nếu chạm PostgreSQL `5433` / Redis `6381` / Qdrant `6333-6334` / Celery locks / Alembic)*.
   - Các script cơ học chuyên đề (`audit_qdrant_zerocode.py` / `validate_diff_and_payload.py` / `check_kpi_gates.py`) nếu liên quan.
4. **Ghi Báo Cáo Khắc Phục**:
   - Lưu vào `.ai-workflow/tasks/<TICKET_ID>/fix-report.md` *(nếu sửa vòng bổ sung sau `final-review.md`, thêm mục `Vòng Fix Bổ Sung` trong `fix-report.md` để giữ trọn lịch sử tại một nơi)*.

---

## Định Dạng Báo Cáo Bắt Buộc (`fix-report.md`)

```markdown
# Fix Report: <TICKET_ID>

## 1. Bảng Tổng Hợp Xử Lý Findings
| ID Finding | Mức độ | Nguyên nhân gốc rễ (Root Cause) | Thay đổi thực tế (`file:line`) | Regression Test chứng minh | Trạng thái |
| :--- | :--- | :--- | :--- | :--- | :--- |

## 2. Chi Tiết Bản Vá & Quyết Định Kỹ Thuật
### [ID Finding]: <Tiêu đề>
- **Root Cause**: ...
- **Giải pháp đã áp dụng**: ...
- **Bằng chứng Red -> Green**: ...

*(Nếu có mục `LOW / Nice to Have` chủ động giữ lại không sửa, giải thích ngắn gọn lý do kỹ thuật).*

## 3. Kết Quả Kiểm Thử Toàn Diện Sau Fix
| Hạng mục | Lệnh thực thi | Kết quả | Ghi chú |
| :--- | :--- | :--- | :--- |
| Targeted Regression Tests | `uv run pytest ...` | PASS | ... |
| Ruff Lint & Format | `uv run ruff check .` | PASS | `0` errors |
| Mypy Strict | `uv run mypy src` | PASS | `0` issues |
| Full Suite + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS | `<N>` passed, `<X>%` |
| Live Integration Tests | `uv run pytest -m integration` | PASS / N/A | ... |
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Sau khi ghi xong `fix-report.md`, in hộp chuyển tiếp dưới đây rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

```markdown
---
### ⏩ Bước tiếp theo: Tái Thẩm Định Cuối Cùng & Cổng Cơ Học (Bước 5 — Final Review)
- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Argon (High / Medium)** hoặc **Claude Sonnet 5.5 (Medium / High)** *(nếu vòng trước có lỗi nặng về `NFR-3`, bảo mật HMAC/Auth hoặc Lua CAS race-condition dùng **Gemini 4 Carbon (Medium)** hoặc **Claude Opus 5.5 (Medium)**)* — *Xác minh độc lập toàn bộ bản vá, chuyển Jira sang `31` (`Verify / Test`) và chạy chốt `preclose_audit.py`.*
- **💡 Mẹo ngữ cảnh**: Khuyến khích mở **New Chat sạch** hoặc đổi sang Model Reviewer trên thanh chọn.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 5 (Final Review) cho task <TICKET_ID> theo .ai-workflow/prompts/05-final-review.md. Kiểm tra lại toàn bộ findings từ review-01.md và fix-report.md, chạy preclose_audit.py cùng các Quality Gates và lưu kết quả vào final-review.md.`
```

---

## Hard Rules
- Chỉ sửa trong phạm vi giải quyết các findings từ Review; không mở rộng tính năng ngoài scope.
- Không chạy `git commit`, không `git push`.
- Không tự tạo `final-review.md` hay `precommit.md`.
- Sau khi in hộp chuyển tiếp: **DỪNG LẠI NGAY LẬP TỨC (`END TURN`)**.
