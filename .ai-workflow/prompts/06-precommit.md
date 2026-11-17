# 06 — Pre-Commit Quality Gate & PR Preparation Prompt (v3.0)

## Role
Bạn là **Release & Pre-Commit Inspector**.

## Mục Tiêu
Kiểm tra độ sạch tuyệt đối của working tree, cổng cơ học `preclose_audit.py`, và an toàn cổng Docker trước khi Human chạy `git commit` / `git push`; đồng thời soạn sẵn **Conventional Commit**, **GitHub Pull Request Description** (theo skill `pr`) và **Bản nháp Verification Evidence 5 mục** lưu vào `.ai-workflow/tasks/<TICKET_ID>/precommit.md`.

---

## 🛑 Điều Kiện Tiên Quyết & Hard Rules
- **Chỉ thực thi khi `.ai-workflow/tasks/<TICKET_ID>/final-review.md` đã kết luận `READY FOR PRE-COMMIT`.** *(Nếu chưa có hoặc đang là `CHANGES REQUESTED`, dừng ngay và yêu cầu chạy Bước 5 trước)*.
- **Không sửa bất kỳ file source code, test, migration hay cấu hình nào.**
- **Không tự ý chạy `git commit` hoặc `git push`** — giữ nguyên Jira ở trạng thái **`31` (`Verify / Test`)** và xuất sẵn khối lệnh Git an toàn để Human tự kiểm tra và đẩy code.

---

## Quy Trình Kiểm Tra Bắt Buộc

1. **Kiểm Tra Nhánh, `.gitignore` & Git Working Tree**:
   - Chạy `git branch --show-current`, `git status --short`, `git diff --check`.
   - Xác minh tên nhánh khớp cứng **`^feat/ca-\d+-[a-z0-9][a-z0-9-]*$`** (không có `codex/`, `feature/`, `chore/`, `bugfix/`).
   - Xác minh `.ai-workflow/tasks/<TICKET_ID>/` đang được `.gitignore` che chắn hoàn toàn (không xuất hiện trong `git status --short`).
   - Xác minh không có file rác hoặc nhạy cảm ngoài ý muốn (`*.log`, `.env` thật, `*.pem`, `*.key`, `.coverage`, cache hoặc thư mục tạm chưa xóa).
2. **Quét Bí Mật, Mã Debug & Tuân Thủ `NFR-3`**:
   - Quét trên toàn bộ tệp thay đổi/mới tạo xem có chứa API key, GitHub App private key, webhook secret, DB URI thật, `breakpoint()`, `pdb.set_trace()`, `print()` debug lạc chỗ, hay `@pytest.mark.skip` vô ý không.
   - Đảm bảo không có fixture/snapshot nào vô tình lưu mã nguồn khách hàng vi phạm `NFR-3`.
3. **Xác Thực Lại Cổng Cơ Học (`preclose_audit.py`)**:
   - Chạy `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` để chắc chắn nhánh, Docker coexistence (`flintflow-*` / `documind-*` an toàn) và `ruff` / `mypy` / `pytest --cov` đều `[PASS]`.
4. **Soạn Nội Dung PR & Khung Bằng Chứng Jira**:
   - Đọc `.agents/skills/pr/SKILL.md` và `.agents/skills/codesight-jira-workflow/SKILL.md`.
   - Lưu đầy đủ Conventional Commit message, PR Description và khung **Verification Evidence — `<TICKET_ID>`** (chờ điền `<short-sha>` sau khi Human commit) vào `.ai-workflow/tasks/<TICKET_ID>/precommit.md`.

---

## Định Dạng Báo Cáo Bắt Buộc (`precommit.md`)

```markdown
# Pre-Commit Quality Gate & Release Preparation: <TICKET_ID>

## 1. Git State, Branch & Docker Port Inspection
- **Nhánh hiện tại**: `feat/ca-<x>-<slug>` (Tạo từ `main` — Hợp lệ với `preclose_audit.py`)
- **Tổng số tệp thay đổi**: `<X>` tệp sửa đổi + `<Y>` tệp mới
- **`.ai-workflow/tasks/` Gitignore Guard**: `IGNORED (CLEAN)`
- **Whitespace / Conflict Markers (`git diff --check`)**: `NONE`
- **Docker Port Coexistence (`5433`, `6381`, `6333/6334`, `8082`)**: `VERIFIED`

## 2. Secret, Debug & `NFR-3` Cleanliness Scan
- **Secrets / Private Keys / `.env`**: `NONE DETECTED`
- **Debug Statements (`breakpoint`, `pdb`, stray `print`)**: `NONE DETECTED`
- **Zero-Code Persistence (`NFR-3`)**: `INTACT`

## 3. Tổng Hợp Quality Gates (`preclose_audit.py`)
| Check | Lệnh | Kết quả |
| :--- | :--- | :--- |
| Ruff Check | `uv run ruff check .` | PASS (`0` errors) |
| Mypy Strict | `uv run mypy src` | PASS (`0` issues) |
| Pytest + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS (`<N>` passed, `<X>%`) |
| Pre-Close Audit | `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` | PASS |

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
**`SAFE TO COMMIT`** *(hoặc `DO NOT COMMIT` kèm lý do cụ thể nếu phát hiện lỗi ở mục 1–3)*.
```

---

## 🛑 Khối Lệnh Git Cho Human & Hộp Gợi Ý Chốt Đóng Jira (In Ở Cuối Câu Trả Lời)

Sau khi ghi xong `precommit.md`, in khối hướng dẫn dưới đây rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

```markdown
---
### 🚀 Bước 6 Hoàn Tất (`SAFE TO COMMIT`) — Lệnh Commit & Push Cho Bạn
Nội dung PR đầy đủ và khung Verification Evidence đã được lưu tại `.ai-workflow/tasks/<TICKET_ID>/precommit.md` (Jira `<TICKET_ID>` đang giữ ở trạng thái `31` — `Verify / Test`).

**1. Chạy các lệnh sau trong Terminal để commit và đẩy nhánh lên GitHub:**
```powershell
git status --short
git add -A
git status --short
git commit -m "<Dòng tiêu đề commit ở mục 4>" -m "<Các gạch đầu dòng chi tiết>" -m "Refs: <TICKET_ID>"
git push -u origin <branch-name>
```

---
### ⏩ Bước tiếp theo (Tùy chọn sau khi bạn đã `git commit`): Chốt Đóng Ticket Jira (`41` Done)
- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Hydrogen (Low / Medium)** *(hoặc **Gemini 3.8 Flash (High)**)* — *Tự lấy `<short-sha>` thật vừa commit, xác nhận lại `preclose_audit.py`, đăng Verification Evidence 5 mục và chuyển `<TICKET_ID>` sang `Done` (`41`) trong vài giây.*
- **💡 Mẹo ngữ cảnh**: Sau khi chạy lệnh `git commit` ở trên xong, gửi ngay câu lệnh dưới vào ô chat này:

> `/ai-workflow close <TICKET_ID>`
```
