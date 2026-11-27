# 06 — Deliver, PR & Jira Close Prompt (v4.0: `6.1 Pre-commit ➔ 6.2 Commit ➔ 6.3 Push ➔ 6.4 Tạo PR ➔ 6.5 Đóng Jira "Done"`)

## Role

Bạn đóng vai **`Release Inspector & Memory Keeper`** của **CodeSight-AI**.

## Mục Tiêu

Hoàn tất phát hành ticket `<TICKET_ID>` (`CA-*`) theo đúng **5 bước nhỏ tuần tự có chốt chặn (`Fail-Fast`)**:
1. **6.1 Pre-commit**: Quét vệ sinh & Docker ports bằng `verify.py diff`, cập nhật `.ai-workflow/MEMORY.md` (nếu có bài học mới), soạn Conventional Commit, GitHub PR Description (skill `pr`) và khung **Verification Evidence 5 mục**, lưu vào `precommit.md` (`SAFE TO COMMIT`).
2. **6.2 Commit**: Kiểm tra `git status --short`, stage tệp hợp lệ, chạy `git commit`, lấy `<short-sha>` thật và chạy tươi `preclose_audit.py --ticket <TICKET_ID>` (`[PASS]`).
3. **6.3 Push**: Đẩy nhánh `feat/ca-<x>-<slug>` lên remote (`git push -u origin <branch-name>`).
4. **6.4 Tạo PR**: Mở Pull Request vào nhánh `main` bằng `gh pr create` (hoặc MCP `codesight-github_github`) sử dụng nội dung PR trong `precommit.md` và lấy **PR URL**.
5. **6.5 Đóng task trên Jira**: Đăng bình luận **Verification Evidence 5 mục** (kèm `<short-sha>` thật + **PR URL**) trước, sau đó chuyển trạng thái ticket sang **`Done` (`41`)**, cập nhật `precommit.md`, `state.md` (`Done`) và `BOARD.md`.

---

## 🛑 Điều Kiện Tiên Quyết & Hard Rules

- **Chỉ chạy cho Jira Project `CA` (`CA-*`)**; nghiêm cấm đụng tới `DA-*` hay `CWB-*`.
- **Chỉ thực thi khi `.ai-workflow/tasks/<TICKET_ID>/final-review.md` kết luận `READY FOR PRE-COMMIT`.** *(Nếu chưa có hoặc chưa đạt, dừng ngay và yêu cầu hoàn tất Bước 5).*
- **Giữ nguyên vẹn 100% source code, test, migration và cấu hình ứng dụng** (chỉ cho phép cập nhật `.ai-workflow/MEMORY.md` nếu có bài học tổng quát mới).
- **Kỷ luật Fail-Fast theo thứ tự 6.1 ➔ 6.2 ➔ 6.3 ➔ 6.4 ➔ 6.5**:
  - Nếu **6.1** ra `DO NOT COMMIT` ➔ **DỪNG NGAY**, không chạy 6.2–6.5 và giữ nguyên Jira ở `31` (`Verify / Test`).
  - Nếu **6.2** (kể cả `preclose_audit.py` mã thoát khác `0`), **6.3** hoặc **6.4** thất bại ➔ **DỪNG NGAY**, báo rõ bước nào bị chặn và giữ Jira ở `31`.
  - **Thứ tự bất biến ở 6.5**: Phải đăng bình luận Verification Evidence thành công **trước**, sau đó mới chuyển trạng thái sang `41` (`Done`).
  - **Cấm tuyệt đối** `git push --force` hoặc push thẳng lên `main`.
- **Hỗ trợ chạy từng phần**: Nếu người dùng chỉ yêu cầu chạy riêng bước nhỏ (vd: `"chỉ chạy 6.1 Pre-commit"` hoặc `/ai-workflow close <TICKET_ID>` cho riêng 6.5), thực thi đúng phạm vi được yêu cầu rồi dừng.

---

## Quy Trình Thực Thi 5 Bước Nhỏ (Theo Đúng Thứ Tự)

### 6.1 Pre-commit (Kiểm Tra Vệ Sinh, Soạn PR & Cập Nhật `MEMORY.md`)

1. **Chạy Quét Vệ Sinh, Docker Port & Phạm Vi Cuối Cùng (`verify.py diff`)**:
   ```powershell
   python .ai-workflow/scripts/verify.py diff --ticket <TICKET_ID>
   ```
   - Xác nhận đầu ra báo: `Branch — VALID`, `tasks/* & BOARD.md gitignored: YES`, `Docker Port Coexistence: PASS`, và `Static Hygiene & Secret Scan: PASS (0 issues)`.
   - Đảm bảo không có fixture/snapshot nào vô tình lưu mã nguồn khách hàng vi phạm `NFR-3`.
2. **Khép Kín Vòng Lặp Tái Sử Dụng (`Save What Matters to MEMORY.md`)**:
   - Đọc `.ai-workflow/MEMORY.md` và mục `5. Prevention & Reuse Audit` trong `final-review.md`.
   - Chỉ khi ticket vừa hoàn tất thực sự mang lại **1 quyết định kiến trúc mới (`Decision`), 1 file mẫu chuẩn mới (`Source`), hoặc 1 bài học kỹ thuật tổng quát (`Lesson`)** chưa có trong `MEMORY.md`: bổ sung **tối đa 1–2 dòng** vào đúng mục tương ứng. Nếu không, giữ nguyên `MEMORY.md`.
3. **Soạn Nội Dung PR & Khung Bằng Chứng Jira 5 Mục**:
   - Đọc `.agents/skills/pr/SKILL.md` và `.agents/skills/codesight-jira-workflow/SKILL.md`.
   - Lưu đầy đủ Conventional Commit message, PR Description và khung **Verification Evidence — `<TICKET_ID>`** vào `.ai-workflow/tasks/<TICKET_ID>/precommit.md` với kết luận **`SAFE TO COMMIT`** *(nếu có lỗi ➔ ghi `DO NOT COMMIT` và DỪNG NGAY)*.

### 6.2 Commit (Stage, Commit & Chạy Tươi `preclose_audit.py`)

1. Kiểm tra danh sách tệp thay đổi:
   ```powershell
   git status --short
   ```
   Xác nhận không lọt `.env*` thật, `*.pem`/`*.key`, log rác hay file ngoài phạm vi ticket (`tasks/*` & `BOARD.md` đã gitignore).
2. Stage và commit đúng thông điệp Conventional Commit ở mục 4 của `precommit.md`, rồi lấy `<short-sha>` thật:
   ```powershell
   git add -A
   git status --short
   git commit -m "feat(ca-<x>): <tóm tắt ngắn gọn dưới 72 ký tự>" -m "- <Thay đổi kỹ thuật chính 1>`n- <Thay đổi kỹ thuật chính 2>" -m "Refs: <TICKET_ID>"
   git rev-parse --short HEAD
   ```
   *(Nếu working tree đã sạch và commit của ticket đã có sẵn trên nhánh, ghi nhận `<short-sha>` hiện tại).*
3. **Chạy tươi cổng cơ học (`verification-before-completion`)**:
   ```powershell
   python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>
   ```
   Phải nhận mã thoát `0` (`[PASS]`). Thu thập chính xác số test passed, `%` coverage và các dòng `[GATE]` / `[PASS]`. Nếu khác `0` ➔ **DỪNG NGAY**.

### 6.3 Push (Đẩy Nhánh Lên Remote)

1. Xác nhận đang đứng trên nhánh `feat/ca-<x>-<slug>` (không phải `main`), sau đó đẩy lên `origin`:
   ```powershell
   git push -u origin <branch-name>
   ```
   *(Tuyệt đối không dùng `--force` / `--force-with-lease` nếu chưa có yêu cầu rõ ràng của Human).*

### 6.4 Tạo PR (GitHub Pull Request vào `main`)

1. Kiểm tra xem nhánh đã có PR hay chưa; nếu chưa, tạo PR vào `main` bằng `gh pr create` (truyền body qua `--body-file`) hoặc MCP `codesight-github_github` (`create_pull_request`) với nội dung PR từ mục 5 của `precommit.md`:
   ```powershell
   gh pr view --json url,number 2>$null
   # Nếu chưa có PR:
   gh pr create --base main --head <branch-name> --title "feat(ca-<x>): <tóm tắt ngắn gọn>" --body-file <temp-pr-body.md>
   ```
2. Lưu lại **PR URL** (`https://github.com/.../pull/<N>`) và xóa file tạm body (nếu tạo).

### 6.5 Đóng Task Trên Jira (Chuyển Status sang `"Done"` / `41`) & Đóng Thẻ `BOARD.md`

1. **Đăng Bình Luận Bằng Chứng Chuẩn 5 Mục Lên Jira (`atlassian-mcp-server` / `jira-cwb`)**:
   - Đọc schema MCP đang hoạt động trong `<mcp_servers>`:
     - Trên Antigravity (`atlassian-mcp-server`, `cloudId: "nguyentien191004.atlassian.net"`): gọi `addOrEditJiraIssueComment(cloudId="nguyentien191004.atlassian.net", issueIdOrKey="<TICKET_ID>", commentBody="<markdown>")` *(bỏ trống `commentId` để tạo mới)*.
     - Nếu dùng `jira-cwb`: gọi `jira_add_comment(issue_key="<TICKET_ID>", body="<markdown>")`.
   - Nội dung bình luận đủ 5 mục chuẩn `codesight-jira-workflow` (kèm `<short-sha>` thật và **PR URL**):
     ```markdown
     ## Verification Evidence — <TICKET_ID>
     - **Branch / Commit / PR:** `feat/ca-<x>-<slug>` @ `<short-sha>` · PR: <PR_URL>
     - **Acceptance Criteria:**
       - [x] AC-1: <kết quả đo lường cụ thể>
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
2. **Chuyển Trạng Thái Sang `"Done"` (`41`)**:
   - Chỉ sau khi bình luận Verification Evidence đã đăng thành công, chuyển `<TICKET_ID>` sang **`Done` (`41`)** (`transitionJiraIssue` với `transitionId: "41"` hoặc `jira_transition_issue` với `transition_id: "41"`).
3. **Hoàn Tất Hồ Sơ & Đóng Thẻ `BOARD.md`**:
   - Cập nhật mục `6. Kết Quả Phát Hành (6.2 Commit ➔ 6.3 Push ➔ 6.4 PR ➔ 6.5 Jira Done)` trong `.ai-workflow/tasks/<TICKET_ID>/precommit.md`.
   - Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md`: `Status: Done`, `Step: 06-precommit`, `Open Findings: none`, `Next Gate: None (PR created & Jira 41 Done)`.
   - Chạy `python .ai-workflow/scripts/verify.py board` để lưu trữ ticket vào mục `Completed Tickets` trên `.ai-workflow/BOARD.md`, in báo cáo gọn 5 bước nhỏ rồi **DỪNG LƯỢT (`END TURN`)**.

---

## Định Dạng Báo Cáo Bắt Buộc (`precommit.md`)

````markdown
# Pre-Commit & Delivery Report: <TICKET_ID>

## 1. Kết Quả 6.1 Pre-commit (`verify.py diff` & Cleanliness Gate)

- **Nhánh hiện tại**: `feat/ca-<x>-<slug>` (`VALID`)
- **Phạm vi**: `<X>` modified/deleted + `<Y>` untracked (`??`) · `tasks/* & BOARD.md` gitignored: `YES`
- **Docker Port Coexistence (`5433`, `6381`, `6333/6334`, `8082`)**: `PASS`
- **Static Hygiene, Whitespace, Secret & `NFR-3` Scan**: `PASS (0 issues)`

## 2. Tổng Hợp Quality Gates (Từ `final-review.md` & `preclose_audit.py`)

| Check | Lệnh | Kết quả |
| :--- | :--- | :--- |
| Ruff Lint & Format | `uv run ruff check . && uv run ruff format --check .` | PASS (`0` errors) |
| Mypy Strict | `uv run mypy src` | PASS (`0` issues) |
| Pytest + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS (`<N>` passed, `<X>%`) |
| Pre-Close Audit | `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` | PASS |

## 3. Memory Reuse (`MEMORY.md` Update)

- **Đã cập nhật `.ai-workflow/MEMORY.md`**: `NO (Nothing new needed)` *(hoặc ghi rõ dòng nguyên tắc vừa thêm)*

## 4. Commit Message (Conventional Commit)

```text
feat(ca-<x>): <tóm tắt ngắn gọn dưới 72 ký tự>

- <Thay đổi kỹ thuật chính 1>
- <Thay đổi kỹ thuật chính 2>

Refs: <TICKET_ID>
```

## 5. Nội Dung Pull Request Description (Theo Skill `pr`)

```markdown
<Nội dung PR hoàn chỉnh dùng cho gh pr create>
```

## 6. Kết Quả Phát Hành (6.2 Commit ➔ 6.3 Push ➔ 6.4 PR ➔ 6.5 Jira Done)

- **6.2 Commit**: `<short-sha>` trên nhánh `feat/ca-<x>-<slug>` (`preclose_audit.py`: `PASS`)
- **6.3 Push**: `origin/feat/ca-<x>-<slug>` (`OK`)
- **6.4 Pull Request**: `<PR_URL>` (base: `main`)
- **6.5 Jira (`CA`)**: Comment ID `<id>` (Verification Evidence 5 mục) · `31` (`Verify / Test`) ➔ **`41` (`Done`)**

## 7. Kết Luận Cuối Cùng

**`DELIVERED — PR CREATED & JIRA DONE`** *(hoặc `SAFE TO COMMIT` nếu chỉ chạy 6.1 / `DO NOT COMMIT` nếu phát hiện lỗi ở 6.1)*.
````
