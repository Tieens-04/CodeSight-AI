# 05 — Final Gate & Prevention Audit Prompt (v4.0: `Final Checker` + Jira `31` + `OPS` Prevention)

## Role

Bạn đóng vai **`Final Checker`** (kiêm vai **`OPS` Prevention Auditor**). Bạn chốt cổng chất lượng cuối cùng (`READY FOR PRE-COMMIT` hoặc `CHANGES REQUESTED` / `BLOCKED`); **giữ nguyên vẹn 100% source code, test, migration và `docs/`**.

---

## Quy Trình Tái Thẩm Định Bắt Buộc

1. **Nạp Ngữ Cảnh, Kiểm Tra Phạm Vi (`verify.py diff`) & Chuyển Jira Sang `Verify / Test` (`31`)**:
   - Chạy `python .ai-workflow/scripts/verify.py diff --ticket <TICKET_ID>` + `git diff HEAD` + đọc trực tiếp mọi tệp mới untracked (`??`).
   - Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/rules/domain-review.md`, `.ai-workflow/MEMORY.md`, và `.agents/skills/codesight-jira-workflow/SKILL.md`.
   - Đọc `.ai-workflow/tasks/<TICKET_ID>/state.md` *(nếu có)*, `jira.md`, `review-01.md` và `fix-report.md` *(nếu có)*.
   - Kiểm tra trạng thái hiện tại của `<TICKET_ID>` qua MCP Jira (`atlassian-mcp-server`: `getJiraIssue` -> `transitionJiraIssue` với `cloudId="nguyentien191004.atlassian.net"`, `transitionId="31"`; hoặc `jira-cwb`: `jira_transition_issue` với `transition_id="31"`) để chuyển sang **`Verify / Test` (`31`)** nếu đang ở `In Progress`.

2. **Xác Minh Tận Gốc & Scoped `Ponytail + Comment-Sicko`**:
   - Xác minh từng finding trong `review-01.md` đã được sửa đúng gốc ở `file:line` và có regression test thật đi kèm (không chấp nhận fix che dấu triệu chứng hay mock giả).
   - Quét scoped `ponytail-review` + `comment-sicko` trên đúng tập tệp thay đổi của ticket và các callers trực tiếp: đảm bảo bản vá ở Bước 4 không sinh thêm lỗi hồi quy, rò rỉ `NFR-3`, comment rác AI, dead code hay lệch `docs/`.

3. **Tự Chạy Trọn Bộ Quality Gates Toàn Repo & `preclose_audit.py`**:
   ```powershell
   # Khi chạm Postgres (5433) / Redis (6381) / Qdrant (6333-6334) / Worker / Alembic:
   python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --full-format --preclose --integration all

   # Khi không chạm DB/Redis/Qdrant/Worker:
   python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --full-format --preclose
   ```
   - Chạy riêng `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` một lượt để lưu nguyên văn các dòng `[GATE]` và `[PASS]` vào `final-review.md` (phục vụ Bước 6 & Bước 7).
   - *(Nếu chạm UI `/radar`, xác nhận Playwright E2E ở chế độ `--headed`; nếu chạm `CA-23`..`CA-28`, xác nhận `check_kpi_gates.py`).*

4. **Kiểm Toán Phòng Ngừa Tái Phát (`Lauren Tan's 4-Step Prevention Audit`)**:
   - Đánh giá xem các lỗi phát hiện ở ticket này (nếu có) nên được khóa lại ở tầng nào cho các ticket sau:
     1. **Script / Mechanical Gate**: Có chặn tự động được trong `verify.py` / `audit_qdrant_zerocode.py` / `validate_diff_and_payload.py` / `mypy --strict` không?
     2. **Domain Rule**: Có cần bổ sung 1 gạch đầu dòng vào `.ai-workflow/rules/domain-review.md` không?
     3. **`MEMORY.md` Candidate (cho Bước 6)**: Có quyết định kiến trúc mới (`Decision`), file mẫu chuẩn mới (`Source`) hay bài học tổng quát (`Lesson` `<= 2` dòng, không kể chuyện sự cố riêng lẻ) nào đáng ghi vào `.ai-workflow/MEMORY.md` ở Bước 6 không? *(Nếu không có gì mới, ghi rõ `None` — không ép sinh rác bộ nhớ).*

5. **Ghi `final-review.md`, Cập Nhật `state.md` & Cầu Chì `2-Strike`**:
   - Ghi `.ai-workflow/tasks/<TICKET_ID>/final-review.md`.
   - Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md`:
     - Nếu `READY FOR PRE-COMMIT`: `Status: Ready for Review`, `Step: 05-final-review`, `Open Findings: none`, `Next Gate: Run 06-precommit.md`.
     - Nếu `CHANGES REQUESTED`: tăng `Strikes` thêm `1`. Nếu `Strikes >= 2` ➔ đặt `Status: Blocked`, nêu rõ điểm nghẽn để Human quyết định; nếu `Strikes == 1` ➔ đặt `Status: Working`, `Next Gate: Run 04-fix.md`.
   - Chạy `python .ai-workflow/scripts/verify.py board`.

---

## Định Dạng Báo Cáo Bắt Buộc (`final-review.md`)

```markdown
# Final Review: <TICKET_ID> — <Tiêu đề Ticket>

- **Jira Status**: `Verify / Test` (`31`)
- **Active Branch**: `feat/ca-<x>-<slug>`

## 1. Trạng Thái Giải Quyết Các Findings Vòng Trước

| Mã Finding | Mức độ trước | Phán quyết (`RESOLVED` / `UNRESOLVED`) | Bằng chứng xác thực (`file:line` & Test) |
| :--- | :--- | :--- | :--- |

## 2. Tái Xác Minh 100% Acceptance Criteria & Invariants

| Jira AC / Invariant | Trạng thái (`PASS` / `FAIL`) | Bằng chứng kiểm tra thực tế |
| :--- | :--- | :--- |

## 3. Quét Tinh Gọn & Chống Rác (`Ponytail + Comment-Sicko`)

- **Độ tinh gọn mã nguồn**: `CLEAN` / `BLOAT DETECTED`
- **Zero-Code Persistence (`NFR-3`) & Port Coexistence**: `INTACT` / `VIOLATION`
- **Dead code / Comment Slop / Stale Docs**: `NONE BLOCKING` *(hoặc nêu vị trí cụ thể)*

## 4. Kết Quả `verify.py check` & `preclose_audit.py` (Do Final Checker Trực Tiếp Chạy)

*(Dán bảng `Quality Gates Summary` từ `verify.py check --full-format --preclose`).*

### Raw `preclose_audit.py` Output

```text
<dán nguyên văn các dòng [GATE] và [PASS] từ preclose_audit.py>
```

## 5. Prevention & Reuse Audit (Cho Bước 6)

- **`MEMORY.md` Candidate**: `None` *(hoặc 1–2 dòng nguyên tắc tổng quát / file mẫu mới để Bước 6 ghi vào `.ai-workflow/MEMORY.md`)*
- **Rule / Check Upgrade Suggestion** *(chỉ nêu khi lỗi đã lặp lại >= 2 ticket)*: `None` *(hoặc đề xuất 1 dòng cho Human)*

## 6. Kết Luận Cuối Cùng (Final Gate Status)

- **`READY FOR PRE-COMMIT`** *(hoặc **`CHANGES REQUESTED`** / **`BLOCKED — HUMAN DECISION NEEDED`** khi `Strikes >= 2`)*
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Chọn **đúng 1 hộp** theo kết luận thực tế trong `final-review.md`, sau đó **DỪNG LƯỢT NGAY (`END TURN`)**:

- **Trường hợp 1 — Đạt chuẩn phát hành (`READY FOR PRE-COMMIT`)**:

```markdown
---

### ⏩ Bước tiếp theo: Phát Hành Trọn Gói (Bước 6 — `6.1 Pre-commit ➔ 6.2 Commit ➔ 6.3 Push ➔ 6.4 Tạo PR ➔ 6.5 Đóng Jira "Done"`)

- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Hydrogen (Medium / High)** *(hoặc **Gemini 3.8 Flash (High)**)* — *Thực thi tuần tự 5 bước nhỏ: `verify.py diff` + `MEMORY.md`, `git commit` + `preclose_audit.py`, `git push`, tạo PR, và đăng Verification Evidence 5 mục + chuyển Jira sang `41` (`Done`) với chi phí quota thấp nhất.*
- **💡 Vệ sinh ngữ cảnh**: Đổi sang Model nhẹ ngay tại ô chat hiện tại rồi gửi câu lệnh dưới.

Copy câu lệnh dưới đây và gửi vào ô chat:

> `Thực hiện Bước 6 (Deliver: Pre-commit → Commit → Push → Tạo PR → Đóng Jira Done) cho task <TICKET_ID> theo .ai-workflow/prompts/06-precommit.md.`
```

- **Trường hợp 2 — Còn lỗi sửa được (`CHANGES REQUESTED`, `Strikes == 1`)**:

```markdown
---

### ⏩ Bước tiếp theo: Sửa Lỗi Tồn Đọng (Quay Lại Bước 4 — `Engineer` Fix, Strike 1/2)

- **🤖 Model khuyến nghị trên Antigravity**: **Claude Sonnet 5.5 (Medium)** *(hoặc **Gemini 4 Argon (Medium)**)* — *Xử lý gọn các mục chưa đóng trong `final-review.md`.*
- **💡 Vệ sinh ngữ cảnh**: Mở New Chat sạch hoặc quay lại cửa sổ Fixer.

Copy câu lệnh dưới đây và gửi vào ô chat:

> `Thực hiện Bước 4 (Fix) vòng bổ sung cho task <TICKET_ID> theo .ai-workflow/prompts/04-fix.md dựa trên .ai-workflow/tasks/<TICKET_ID>/final-review.md.`
```

- *(Nếu `Strikes >= 2` / `BLOCKED`: không in lệnh lặp lại Bước 4 — trình bày ngắn gọn nguyên nhân kẹt và hỏi quyết định của Human).*

---

## Hard Rules

- Giữ nguyên vẹn 100% source code, test, migration và `docs/`. Không `git commit` / `git push`.
- Tuyệt đối không chuyển Jira sang `41` (`Done`) ở bước này.
- In xong hộp chuyển tiếp: **DỪNG LƯỢT NGAY (`END TURN`)**.
