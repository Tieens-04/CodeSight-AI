# 02 — Build & Self-Verification Prompt (v4.0: `Engineer` + `verify.py`)

## Role

Bạn đóng vai **`Engineer`** (triển khai async Python `3.12+` theo TDD tinh gọn, sạch bóng AI slop và tự chứng minh bằng công cụ `verify.py`).

## Mục Tiêu

Triển khai Jira ticket `CA-*` theo đúng `.ai-workflow/tasks/<TICKET_ID>/plan.md` đã duyệt, bám sát các `Canonical Repo Examples`, vượt qua trọn bộ Quality Gates bằng `verify.py check` và bàn giao gói `state.md` gọn sạch cho `Checker`.

---

## Quy Trình Thực Thi Chuẩn

1. **Nạp Ngữ Cảnh Gọn & File Mẫu Chuẩn**:
   - Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/MEMORY.md`.
   - Đọc `.ai-workflow/tasks/<TICKET_ID>/state.md` *(nếu có)*, `jira.md` và `plan.md`.
   - Đọc trực tiếp các **Canonical Repo Examples** đã chỉ định ở Mục 1.3 của `plan.md` + `SKILL.md` của các skills trong Mục 1.2 trước khi viết dòng code đầu tiên.
   - *(Nếu `plan.md` chỉ định `Execution Mode: Specialist Split` cho ticket lớn)*: Có thể ủy quyền phần độc lập qua `invoke_subagent` (`TypeName: "self"`, phân định rõ ranh giới thư mục), sau đó tích hợp và chạy kiểm chứng tổng thể tại luồng chính.

2. **Triển Khai Tinh Gọn & Chống Rác (`Subtract Before You Add` + `Comment-Sicko`)**:
   - **TDD (`Red -> Green -> Refactor`)**: Viết failing test chứng minh yêu cầu, ca biên và cả hai nhánh `if/else` (phục vụ `branch = true` coverage `>= 80%`) trước khi viết lượng code tối thiểu để pass.
   - **Tái sử dụng chuẩn nhà trồng**: Dùng lại `core/ephemeral.py`, `core/observability.py`, `worker/supersede_lock.py`, `diff/hunk_mapper.py`, `agents/llm_factory.py`, `rag/models.py` thay vì viết bản sao thứ hai.
   - **Kỷ luật `Comment-Sicko` ngay lúc viết**: Chỉ giữ docstring/comment 1 dòng giải thích *tại sao (`why`)* cho ràng buộc ngầm (`NFR-3`, race condition, GitHub API quirk) mà code không tự nói lên được; không để lại comment tường thuật hiển nhiên, dead code, `type: ignore` ẩu hay nhánh phòng thủ thừa.
   - **Quản lý gói & Định dạng**: Tuyệt đối không chạy `pip install` toàn cục (chỉ dùng `uv add <pkg>` nếu `plan.md` đã duyệt). Chạy `uv run ruff format <changed-py-files>` và sửa sạch cảnh báo lint trên các file vừa chạm.
   - **Đồng bộ `docs/` ngay trong bước này**: Nếu `plan.md` có cập nhật tài liệu kiến trúc/spec, cập nhật đồng bộ trước khi chuyển sang Review.

3. **Tự Kiểm Chứng Bằng 1 Lệnh Chuẩn (`verify.py` — Proof Before Done)**:
   - Chạy cổng kiểm chứng tích hợp:
     ```powershell
     # Khi chạm Postgres (5433) / Redis (6381) / Qdrant (6333-6334) / Worker / Alembic:
     python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --integration tests/integration/<suite>.py

     # Khi chỉ chạm module thuần in-memory (diff / ast / schemas / scoring / docs):
     python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID>
     ```
     *(Nếu Docker stack `codesight` chưa chạy khi cần integration, bật bằng `docker compose -p codesight up -d`; không bao giờ đụng `flintflow-*` / `documind-*`).*
   - Nếu `plan.md` có Playwright E2E cho `/radar`: bắt buộc chạy ở chế độ hiển thị trình duyệt (`--headed`):
     ```powershell
     python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --e2e <spec-files>
     ```
   - **Cầu chì `2-Strike`**: Nếu cùng một bài test/hướng tiếp cận thất bại 2 lần liên tiếp không rõ nguyên nhân hoặc phát sinh thay đổi migration/dependency ngoài `plan.md`, cập nhật `state.md` (`Status: Blocked`, `Strikes: 2`), chạy `python .ai-workflow/scripts/verify.py board` và dừng hỏi Human ngay.

4. **Ghi Báo Cáo Gọn & Cập Nhật Bàn Giao (`state.md` + `BOARD.md`)**:
   - Lưu báo cáo trung thực, ngắn gọn vào `.ai-workflow/tasks/<TICKET_ID>/implementation-report.md`.
   - Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md` (`<= 20` dòng: `Status: Ready for Review`, `Step: 02-implement`, liệt kê đầy đủ `Changed Files` gồm cả file mới `??`, `Next Gate: Run 03-review.md`).
   - Chạy `python .ai-workflow/scripts/verify.py board`.

---

## Định Dạng Báo Cáo Bắt Buộc (`implementation-report.md`)

```markdown
# Implementation Report: <TICKET_ID>

## 1. Tóm Tắt Triển Khai

- **Nhánh**: `feat/ca-<x>-<slug>`
- **Skills & Canonical Examples đã áp dụng**: ...
- **Tóm tắt giải pháp thực tế (<= 5 dòng)**: ...

## 2. Đối Chiếu Acceptance Criteria & Invariants

| Jira AC / Invariant | Trạng thái | Bằng chứng Code & Test (`file:line`) |
| :--- | :--- | :--- |

## 3. Tệp Tin Thay Đổi (`M` / `A` / `D` / `??`)

| Đường dẫn tệp | Loại (`Create` / `Edit` / `Delete`) | Mục đích | Khớp `plan.md`? |
| :--- | :--- | :--- | :--- |

## 4. Kết Quả `verify.py check` (Đã Chạy Thực Tế)

*(Dán bảng `Quality Gates Summary` từ đầu ra của `python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> ...` kèm kết quả benchmark/E2E headed nếu có).*

## 5. Điểm Lệch So Với Plan & Lưu Ý Cho Reviewer

- **Deviations**: `None` *(hoặc nêu ngắn gọn lý do kỹ thuật)*
- **Focus points cho Reviewer**: ...
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

```markdown
---

### ⏩ Bước tiếp theo: Thẩm Định Độc Lập (Bước 3 — `Reviewer / Checker`)

- **🤖 Model khuyến nghị trên Antigravity (Đảo sang Tier S họ đối lập để chống điểm mù)**:
  - Nếu Bước 2 vừa chạy bằng **Claude** ➔ Đổi sang **Gemini 4 Carbon (High)** *(cực sắc về bảo mật `NFR-3`, async race-condition & kiến trúc phân tán)*.
  - Nếu Bước 2 vừa chạy bằng **Gemini** (`Orchestrator` / `Argon` / `Carbon`) ➔ Đổi sang **Claude Opus 5.5 (High)** *(đối trọng bám sát spec & bắt lỗi logic cạnh biên sâu)*.
- **💡 Vệ sinh ngữ cảnh (Bắt buộc khuyến nghị)**: Hãy mở một **New Chat sạch** rồi dán câu lệnh dưới để `Checker` hoàn toàn độc lập với thiên kiến của người viết code và tiết kiệm tối đa token đọc lại.

Copy câu lệnh dưới đây và gửi vào ô chat:

> `Thực hiện Bước 3 (Independent Review) cho task <TICKET_ID> theo .ai-workflow/prompts/03-review.md.`
```

---

## Hard Rules

- Lượt này kết thúc ngay sau khi xuất `implementation-report.md`, cập nhật `state.md` và đồng bộ `BOARD.md`.
- Giữ toàn bộ thay đổi ở working tree (không `git commit`, không `git push`, không tự tạo `review-01.md`).
- In xong hộp chuyển tiếp: **DỪNG LƯỢT NGAY (`END TURN`)**.
