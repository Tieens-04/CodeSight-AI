# 06 — Deliver, PR Prep & Memory Reuse Prompt (v4.0: `Deliver` + `MEMORY.md` + `The Undo Gate`)

## Role

Bạn đóng vai **`Release Inspector & Memory Keeper`**.

## Mục Tiêu

Hoàn tất 100% các bước chuẩn bị phát hành khả nghịch trước khi Human chạy `git commit` / `git push`: xác nhận working tree & Docker ports sạch bằng `verify.py diff`, chắt lọc bài học tái sử dụng vào `.ai-workflow/MEMORY.md`, soạn **Conventional Commit**, **GitHub PR Description** (theo skill `pr`) và **Khung Jira Verification Evidence 5 mục** lưu vào `.ai-workflow/tasks/<TICKET_ID>/precommit.md`, rồi **dừng đúng trước cổng `git commit` (`The Undo Gate`)**.

---

## 🛑 Điều Kiện Tiên Quyết & Hard Rules

- **Chỉ thực thi khi `.ai-workflow/tasks/<TICKET_ID>/final-review.md` kết luận `READY FOR PRE-COMMIT`.** *(Nếu chưa có hoặc chưa đạt, dừng ngay và yêu cầu hoàn tất Bước 5).*
- **Giữ nguyên vẹn 100% source code, test, migration và cấu hình ứng dụng** (chỉ cho phép cập nhật `.ai-workflow/MEMORY.md` nếu có bài học tổng quát mới).
- **Dừng trước bước không thể hoàn tác (`The Undo Gate`)**: Không tự chạy `git commit` hay `git push`; giữ nguyên Jira ở trạng thái **`31` (`Verify / Test`)** cho tới Bước 7 (`/ai-workflow close <TICKET_ID>`) sau khi có `<short-sha>` thật.

---

## Quy Trình Kiểm Tra, Soạn Thảo & Tích Lũy Trí Nhớ

1. **Chạy Quét Vệ Sinh, Docker Port & Phạm Vi Cuối Cùng (`verify.py diff`)**:
   ```powershell
   python .ai-workflow/scripts/verify.py diff --ticket <TICKET_ID>
   ```
   - Xác nhận đầu ra báo: `Branch — VALID`, `tasks/* & BOARD.md gitignored: YES`, `Docker Port Coexistence: PASS`, và `Static Hygiene & Secret Scan: PASS (0 issues)`.
   - Đảm bảo không có fixture/snapshot nào vô tình lưu mã nguồn khách hàng vi phạm `NFR-3`.

2. **Khép Kín Vòng Lặp Tái Sử Dụng (`Save What Matters to MEMORY.md`)**:
   - Đọc `.ai-workflow/MEMORY.md` và mục `5. Prevention & Reuse Audit` trong `final-review.md`.
   - Chỉ khi ticket vừa hoàn tất thực sự mang lại **1 quyết định kiến trúc mới (`Decision`), 1 file mẫu chuẩn mới (`Source`), hoặc 1 bài học kỹ thuật tổng quát (`Lesson`)** chưa có trong `MEMORY.md`:
     - Bổ sung **tối đa 1–2 dòng** vào đúng mục tương ứng trong `.ai-workflow/MEMORY.md` (viết dạng nguyên tắc chung + đường dẫn file mẫu, tuyệt đối không viết nhật ký kể lể sự cố riêng của ticket).
   - Nếu không có quy tắc mới nào cần thêm, giữ nguyên `MEMORY.md` (tránh phình bộ nhớ).

3. **Soạn Nội Dung PR & Khung Bằng Chứng Jira 5 Mục**:
   - Đọc `.agents/skills/pr/SKILL.md` và `.agents/skills/codesight-jira-workflow/SKILL.md`.
   - Lưu đầy đủ Conventional Commit message, PR Description (Summary + sơ đồ luồng ngắn nếu cần + Evidence Before/After + Merge Danger / Blast Radius) và khung **Verification Evidence — `<TICKET_ID>`** (chờ điền `<short-sha>` ở Bước 7) vào `.ai-workflow/tasks/<TICKET_ID>/precommit.md`.

4. **Cập Nhật `state.md` & Đồng Bộ `BOARD.md`**:
   - Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md`: `Status: Ready for Review`, `Step: 06-precommit`, `Open Findings: none`, `Next Gate: Human git commit & /ai-workflow close <TICKET_ID>`.
   - Chạy `python .ai-workflow/scripts/verify.py board`.

---

## Định Dạng Báo Cáo Bắt Buộc (`precommit.md`)

````markdown
# Pre-Commit & Delivery Report: <TICKET_ID>

## 1. Kết Quả `verify.py diff` & Cleanliness Gate

- **Nhánh hiện tại**: `feat/ca-<x>-<slug>` (`VALID`)
- **Phạm vi**: `<X>` modified/deleted + `<Y>` untracked (`??`) · `tasks/* & BOARD.md` gitignored: `YES`
- **Docker Port Coexistence (`5433`, `6381`, `6333/6334`, `8082`)**: `PASS`
- **Static Hygiene, Whitespace, Secret & `NFR-3` Scan**: `PASS (0 issues)`

## 2. Tổng Hợp Quality Gates (Từ `final-review.md`)

| Check | Lệnh | Kết quả |
| :--- | :--- | :--- |
| Ruff Lint & Format | `uv run ruff check . && uv run ruff format --check .` | PASS (`0` errors) |
| Mypy Strict | `uv run mypy src` | PASS (`0` issues) |
| Pytest + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS (`<N>` passed, `<X>%`) |
| Pre-Close Audit | `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` | PASS |

## 3. Memory Reuse (`MEMORY.md` Update)

- **Đã cập nhật `.ai-workflow/MEMORY.md`**: `NO (Nothing new needed)` *(hoặc ghi rõ dòng nguyên tắc vừa thêm)*

## 4. Đề Xuất Commit Message (Conventional Commit)

```text
feat(ca-<x>): <tóm tắt ngắn gọn dưới 72 ký tự>

- <Thay đổi kỹ thuật chính 1>
- <Thay đổi kỹ thuật chính 2>

Refs: <TICKET_ID>
```

## 5. Nội Dung Pull Request Description (Theo Skill `pr`)

```markdown
<Nội dung PR hoàn chỉnh sẵn sàng copy vào GitHub PR>
```

## 6. Bản Nháp Jira Verification Evidence (Sẵn Sàng Cho Bước 7 `/ai-workflow close`)

```markdown
## Verification Evidence — <TICKET_ID>
- **Branch / Commit:** `feat/ca-<x>-<slug>` @ `<short-sha-after-commit>`
- **Acceptance Criteria:**
  - [x] AC-1: ...
- **Quality Gates (`uv run`):**
  - `ruff check .`: PASS (`0` errors)
  - `mypy src`: PASS (`0` issues)
  - `pytest --cov=src --cov-fail-under=80`: PASS (`<N>` passed, coverage `<X>%`)
- **Port & NFR Invariants:** Coexistence verified (`5433`, `6381`, `6333/6334`, `8082`); Zero-Code Persistence (`NFR-3`) intact.
- **Verification Output Summary:**
  ```text
  <dán output thật từ preclose_audit.py>
  ```
```

## 7. Kết Luận Cuối Cùng

**`SAFE TO COMMIT`** *(hoặc `DO NOT COMMIT` kèm lý do cụ thể nếu bước 1 phát hiện lỗi)*.
````

---

## 🛑 Khối Lệnh Git Cho Human (`The Undo Gate`) & Hộp Chuyển Tiếp Bước 7

Sau khi ghi xong `precommit.md` và đồng bộ `BOARD.md`, in khối hướng dẫn dưới đây rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

````markdown
---

### 🚀 Bước 6 Hoàn Tất (`SAFE TO COMMIT` — `The Undo Gate`)

- Báo cáo, PR body & khung Evidence: `.ai-workflow/tasks/<TICKET_ID>/precommit.md`
- Jira `<TICKET_ID>` đang giữ ở trạng thái `31` (`Verify / Test`) chờ mã commit `<short-sha>` thật.

**1. Chạy các lệnh sau trong Terminal để kiểm tra danh sách file, commit và đẩy nhánh lên GitHub:**

```powershell
git status --short
git add -A
git status --short
git commit -m "<Dòng tiêu đề commit ở mục 4>" -m "<Các gạch đầu dòng chi tiết>" -m "Refs: <TICKET_ID>"
git push -u origin <branch-name>
```

---

### ⏩ Bước tiếp theo (Sau khi bạn đã `git commit`): Chốt Đóng Ticket Jira (`41` Done)

- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Hydrogen (Low / Medium)** *(hoặc **Gemini 3.8 Flash (High)**)* — *Tự lấy `<short-sha>` thật vừa commit, xác nhận lại `preclose_audit.py`, đăng Verification Evidence 5 mục, chuyển `<TICKET_ID>` sang `Done` (`41`) và đóng thẻ trên `BOARD.md`.*
- **💡 Vệ sinh ngữ cảnh**: Ngay sau khi chạy lệnh `git commit` ở trên xong, gửi câu lệnh dưới vào ô chat này:

> `/ai-workflow close <TICKET_ID>`
````
