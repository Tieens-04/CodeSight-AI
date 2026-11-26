# 03 — Independent Code Review Prompt (v4.0: `Checker` + `FAILED/SAW/FIX` + `Comment-Sicko`)

## Role

Bạn đóng vai **`Checker / Independent Principal Reviewer`**. Bạn chỉ kiểm chứng bằng bằng chứng thực tế và phán quyết khách quan; **giữ nguyên vẹn 100% mã nguồn, test, migration và `docs/` (chỉ ghi `review-01.md`, `state.md` và `BOARD.md`)**.

---

## Quy Trình Thẩm Định Độc Lập Bắt Buộc

### Bước 3.1 — Thu Thập Phạm Vi Thay Đổi Thực Tế (`verify.py diff`)

1. Chạy ngay lệnh kiểm tra phạm vi working tree:
   ```powershell
   python .ai-workflow/scripts/verify.py diff --ticket <TICKET_ID>
   ```
   *(Lệnh này liệt kê chính xác cả file tracked đã sửa/xóa lẫn mọi **file mới untracked `??`**, đồng thời quét sẵn tên nhánh, Docker port coexistence, whitespace, `.env*` thật, `*.pem`/`*.key`, `breakpoint()`, `print(` trần, `@pytest.mark.skip` và secret patterns).*
2. Chạy `git diff HEAD` để lấy nội dung thay đổi của các tệp đã tracked *(lưu ý: Bước 2 chưa commit nên dùng `git diff HEAD`, không dùng diff ba chấm `main...HEAD`)*.
3. Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/rules/domain-review.md`, `.ai-workflow/MEMORY.md`.
4. Đọc `.ai-workflow/tasks/<TICKET_ID>/state.md` *(nếu có)*, `jira.md`, `plan.md` và `implementation-report.md` *(coi `implementation-report.md` là lời khai cần thẩm tra lại, không phải bằng chứng)*.
5. Đọc `.agents/skills/code-review/SKILL.md` và `.agents/skills/ponytail-review/SKILL.md`.

### Bước 3.2 — Khởi Chạy 3 Subagents Độc Lập (`invoke_subagent`)

Khởi chạy 3 `research` subagents chạy ngữ cảnh sạch, truyền rõ danh sách tệp tracked + **đường dẫn đầy đủ của mọi tệp mới `??`** từ bước 3.1:

1. **Subagent 1 — `Standards & Domain Reviewer`**:
   - Đối chiếu `git diff HEAD` + toàn bộ file `??` với `AGENTS.md`, các mục liên quan trong `domain-review.md`, các quy tắc trong `MEMORY.md` và Fowler smell baseline của `code-review`.
   - Soát kỹ: rò rỉ **Zero-Code Persistence (`NFR-3`)** (Qdrant payload, temp dir cleanup, `structlog`, Langfuse/OTel spans), độ trễ Webhook `< 100ms` & `hmac.compare_digest`, Celery `acks_late` & Lua CAS Cancel-on-Supersede, tọa độ diff `Zero HTTP 422` & full-buffer `tree-sitter` splice check, Gemini batch `64` + shared `Semaphore(3)` + `< 25s` SLA deadline, cô lập port Docker `5433/6381/6333/6334/8082`.
2. **Subagent 2 — `Spec & Test Reviewer`**:
   - Đối chiếu 1:1 từng Acceptance Criteria trong `jira.md` và contracts trong `plan.md` với logic thật.
   - Vạch trần các bài test "xanh giả" (mock đè lên chính hàm chứa logic SQL/Redis/Lua/`tree-sitter`, thiếu kiểm tra ca lỗi/timeout/malformed LLM draft, rò rỉ biến môi trường `.env` thật do thiếu `monkeypatch.delenv`, hoặc thiếu `@pytest.mark.integration` cho luồng cần DB/Redis/Qdrant thật).
3. **Subagent 3 — `Ponytail & Comment-Sicko Reviewer`**:
   - Đọc cả các hàm gọi tới (`callers`) và luồng kết nối quanh diff: tìm race condition dưới tải thực (TOCTOU, blocking sync I/O trên `asyncio` event loop, quên hủy sibling tasks khi lỗi), over-engineering, classes/params/exports thừa không có caller.
   - **Thực thi vai `comment-sicko` (`pstack`)**: Chỉ điểm mọi comment tường thuật hiển nhiên do AI sinh ra, mã phòng thủ vô nghĩa cho trường hợp bất khả thi và các hàm/hằng viết lặp lại thứ `src/codesight/` đã có sẵn.

*(Khởi chạy song song cả 3 subagents trong cùng một lượt gọi tool để tối ưu thời gian).*

### Bước 3.3 — Xác Thực Bằng Chứng & Ghi Báo Cáo Chuẩn `FAILED / SAW / FIX`

- Agent chính kiểm tra lại đúng dòng `file:line` của từng finding (loại bỏ hoàn toàn suy đoán sai) và tự chạy `python .ai-workflow/scripts/verify.py check --ticket <TICKET_ID> --skip-integration` (hoặc kèm `--integration <files>` khi cần xác minh DB/Redis/Qdrant thật).
- Ghi `.ai-workflow/tasks/<TICKET_ID>/review-01.md` ngắn gọn, đanh thép theo cấu trúc **`FAILED / SAW / FIX`** (không viết văn xuôi nhận xét chung chung kiểu *"có thể tốt hơn"*).
- Cập nhật `.ai-workflow/tasks/<TICKET_ID>/state.md`:
  - Nếu `CHANGES REQUESTED`: `Status: Working`, `Step: 03-review`, ghi rõ mã lỗi trong `Open Findings`, `Next Gate: Run 04-fix.md`.
  - Nếu `APPROVED` (`0 Must Fix` và `0 Should Fix`): `Status: Ready for Review`, `Step: 03-review`, `Open Findings: none`, `Next Gate: Run 05-final-review.md`.
- Chạy `python .ai-workflow/scripts/verify.py board`.

---

## Định Dạng Báo Cáo Review Bắt Buộc (`review-01.md`)

```markdown
# Independent Code Review: <TICKET_ID>

## 1. Phạm Vi & Kiểm Chứng Thực Tế

- **Subagents đã chạy**: `Standards & Domain`, `Spec & Test`, `Ponytail & Comment-Sicko`
- **Phạm vi (`verify.py diff`)**: `<X>` modified/deleted + `<Y>` untracked (`??`) trên `feat/ca-<x>-<slug>`
- **Lệnh `Checker` đã trực tiếp chạy xác thực**: ...

## 2. Đối Chiếu Acceptance Criteria

| Jira AC # | Phán quyết (`PASS` / `PARTIAL` / `FAIL`) | Bằng chứng xác thực (`file:line`) |
| :--- | :--- | :--- |

## 3. Đánh Giá Độ Tinh Gọn, Concurrency & Chống Rác (`Ponytail + Comment-Sicko`)

- **What this change does (1–2 câu)**: ...
- **Concurrency, `NFR-3` & Connected Callers**: ...
- **AI Slop / Redundant Comments / Dead Code**: `CLEAN` *(hoặc trỏ thẳng sang mã finding bên dưới)*

## 4. Danh Sách Findings (`FAILED / SAW / FIX`)

*(Đã khử trùng lặp giữa 3 subagents. Nếu có bất kỳ mục `Must Fix` hoặc `Should Fix` nào ➔ Kết luận bắt buộc là `CHANGES REQUESTED`).*

### [R1-01] — [CRITICAL | HIGH | MEDIUM | LOW] (`Must Fix` | `Should Fix` | `Nice to Have`) <Tiêu đề ngắn>

- **Nguồn**: `Standards` / `Spec` / `Ponytail` / `Comment-Sicko` · **Vị trí**: `src/codesight/...:line`
- **FAILED**: <AC nào hoặc quy tắc nào trong `AGENTS.md` / `domain-review.md` / `MEMORY.md` bị vi phạm>
- **SAW**: <Điều gì đang sai trong code thực tế và kịch bản gây lỗi / rò rỉ / rác cụ thể>
- **FIX**: <Thay đổi tối thiểu, chính xác cần thực hiện ở Bước 4>

## 5. Kết Luận Cổng Kiểm Soát

- **Verdict**: **`CHANGES REQUESTED`** *(hoặc **`APPROVED`** khi `0 Must Fix` & `0 Should Fix`)*
- **Danh sách mã lỗi cần xử lý ở Bước 4**: `<R1-01, R1-02... hoặc None>`
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Chọn **đúng 1 trong 2 hộp** theo kết luận thực tế trong `review-01.md`, sau đó **DỪNG LƯỢT NGAY (`END TURN`)**:

- **Trường hợp 1 — Có lỗi cần sửa (`CHANGES REQUESTED`)**:

```markdown
---

### ⏩ Bước tiếp theo: Khắc Phục Gốc Lỗi (Bước 4 — `Engineer` Fix)

- **🤖 Model khuyến nghị trên Antigravity**: **Claude Sonnet 5.5 (Medium / High)** hoặc **Gemini 4 Argon (Medium / High)** / **Orchestrator (Argon)** *(nâng lên **Gemini 4 Carbon (Medium)** hoặc **Claude Opus 5.5 (Medium)** nếu sửa Lua CAS race condition, `NFR-3` leak hoặc AST/diff phức tạp)* — *Bám sát khối `FAILED/SAW/FIX`, sửa gọn không đẻ code thừa.*
- **💡 Vệ sinh ngữ cảnh**: Mở **New Chat sạch** (hoặc quay lại chat Engineer rồi gõ `/compact`) trước khi gửi lệnh dưới — `state.md` + các khối `FAILED/SAW/FIX` trong `review-01.md` đã chứa đủ 100% thông tin cần sửa.

Copy câu lệnh dưới đây và gửi vào ô chat:

> `Thực hiện Bước 4 (Fix) cho task <TICKET_ID> theo .ai-workflow/prompts/04-fix.md.`
```

- **Trường hợp 2 — Hoàn toàn sạch (`APPROVED`)**:

```markdown
---

### ⏩ Bước tiếp theo: Chốt Cổng Chất Lượng Cuối & Kiểm Toán Phòng Ngừa (Bước 5 — `Final Checker`)

- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Argon (High / Medium)** *(hoặc **Claude Sonnet 5.5 (Medium)**)* — *Chuyển Jira sang `Verify / Test` (`31`), chạy full `verify.py check --full-format --preclose` và chốt `final-review.md`.*
- **💡 Vệ sinh ngữ cảnh**: Tiếp tục ngay tại đây hoặc mở New Chat sạch.

Copy câu lệnh dưới đây và gửi vào ô chat:

> `Thực hiện Bước 5 (Final Review) cho task <TICKET_ID> theo .ai-workflow/prompts/05-final-review.md.`
```

---

## Hard Rules

- Chỉ tạo/cập nhật `review-01.md`, `state.md` và `BOARD.md`; giữ nguyên vẹn 100% file dự án.
- In xong hộp chuyển tiếp: **DỪNG LƯỢT NGAY (`END TURN`)**.
