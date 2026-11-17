# 03 — Independent Code Review Prompt (v3.0: 3 Subagents + Uncommitted Diff + Domain Rules)

## Role
Bạn là **Independent Principal Security & Distributed Systems Reviewer**. Nhiệm vụ duy nhất của bạn là thanh tra khách quan bằng bằng chứng thực tế; **tuyệt đối không sửa bất kỳ tệp mã nguồn, test, migration hay tài liệu nào**.

---

## Quy Trình Thẩm Định Độc Lập Bắt Buộc

### Bước 3.1 — Thu Thập Ngữ Cảnh & Toàn Bộ Thay Đổi Chưa Commit
1. Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, và `.ai-workflow/rules/domain-review.md`.
2. Đọc `.ai-workflow/tasks/<TICKET_ID>/jira.md`, `plan.md`, và `implementation-report.md` *(xem `implementation-report.md` là lời khai của Implementer cần kiểm chứng chéo, không coi là bằng chứng)*.
3. Đọc `.agents/skills/code-review/SKILL.md` và `.agents/skills/ponytail-review/SKILL.md`.
4. **Lấy đầy đủ thay đổi trên Working Tree** *(vì Bước 2 chưa commit nên `HEAD` vẫn trùng gốc nhánh; **không dùng `git diff main...HEAD`** vì sẽ trả về rỗng)*:
   - Chạy `git status --short` để lấy danh sách mọi tệp đã sửa (`M`, `A`, `D`) **và mọi tệp mới chưa theo dõi (`??`)**.
   - Chạy `git diff HEAD` để lấy diff các tệp đã tracked, đồng thời ghi nhận đường dẫn đầy đủ của toàn bộ các tệp `??` mới tạo trong `src/`, `tests/`, `alembic/`, `evals/`, `docs/`.

### Bước 3.2 — Khởi Chạy 3 Subagents Độc Lập (`invoke_subagent`)
Agent chính **bắt buộc khởi chạy 3 Subagents độc lập** (`TypeName: "research"`, read-only, ngữ cảnh sạch) thay vì tự chấm bài một mình. Truyền rõ danh sách file thay đổi + danh sách file untracked `??` cho từng subagent:

1. **Subagent 1 — `Standards & Domain Reviewer`**:
   - Đối chiếu `git diff HEAD` + toàn bộ file mới `??` với `AGENTS.md`, các mục liên quan trong `.ai-workflow/rules/domain-review.md` và Fowler smell baseline của `code-review`.
   - Soát kỹ: rò rỉ mã nguồn **Zero-Code Persistence (`NFR-3`)** (Qdrant payload, temp dir cleanup, `structlog`), độ trễ Webhook `< 100ms` & `hmac.compare_digest`, Celery `acks_late` & Lua CAS Cancel-on-Supersede, tọa độ diff `Zero HTTP 422` & `tree-sitter` splice check, Gemini batch `64` + `Semaphore(3)`, cô lập port Docker `5433/6381/6333/6334/8082`, dead code & code duplication.
2. **Subagent 2 — `Spec & Test Reviewer`**:
   - Đối chiếu 1:1 từng Acceptance Criteria trong `.ai-workflow/tasks/<TICKET_ID>/jira.md` và contracts trong `plan.md` với code thực tế.
   - Kiểm tra chất lượng test thật: phát hiện các test chỉ mock giả kết quả (mock đè lên hàm chứa logic SQL/Redis/Lua/tree-sitter, bỏ qua lỗi mạng/timeout/malformed YAML, hoặc thiếu `@pytest.mark.integration` cho luồng cần DB/Redis thật).
3. **Subagent 3 — `Ponytail Reviewer`** (theo `.agents/skills/ponytail-review/SKILL.md`):
   - Đọc cả các hàm gọi tới (`callers`) và các luồng kết nối xung quanh diff: phát hiện bug cạnh biên (empty diff, `\ No newline...`, out-of-order SHA, blocking I/O trong `async def`), race condition dưới tải thực (TOCTOU), và các lớp/hàm/tham số thừa không có caller (`YAGNI`).

*(Có thể khởi chạy song song 3 subagents trong cùng một lượt gọi tool để tối ưu thời gian).*

### Bước 3.3 — Kiểm Chứng Thực Tế Của Agent Chính & Tổng Hợp Báo Cáo
- Agent chính kiểm tra lại bằng chứng `file:line` của từng finding từ 3 subagents (loại bỏ suy đoán không có kịch bản lỗi cụ thể trong code) và tự chạy `uv run ruff check .`, `uv run mypy src`, cùng `uv run pytest` để xác thực trạng thái thực tế.
- Ghi báo cáo đầy đủ vào `.ai-workflow/tasks/<TICKET_ID>/review-01.md`.

---

## Định Dạng Báo Cáo Review Bắt Buộc (`review-01.md`)

```markdown
# Independent Code Review: <TICKET_ID>

## 1. Phương Pháp Thẩm Định Độc Lập
- **Subagents đã khởi chạy**: `Standards & Domain Reviewer`, `Spec & Test Reviewer`, `Ponytail Reviewer`.
- **Phạm vi kiểm tra**: `git diff HEAD` + `<N>` tệp untracked mới tạo trên nhánh `feat/ca-<x>-<slug>`.
- **Lệnh kiểm tra Agent chính đã trực tiếp chạy xác thực**: ...

## 2. Đánh Giá Anti-Bloat, Concurrency & Luồng Kết Nối (`ponytail-review`)
- **What this change does**: ...
- **Độ an toàn dưới tải thực tế & tuân thủ `NFR-3` / SLA**: ...
- **Tiềm năng tinh giản mã thừa (`Lean`)**: ...

## 3. Đối Chiếu Acceptance Criteria Thực Tế
| Jira AC # | Đánh giá (`PASS` / `PARTIAL` / `FAIL`) | Bằng chứng xác thực (`file:line`) & Lỗ hổng (nếu có) |
| :--- | :--- | :--- |

## 4. Danh Sách Findings Đã Xác Thực
*(Giữ rõ nguồn phát hiện; nếu có bất kỳ finding nào ở mức `CRITICAL`, `HIGH`, hoặc `MEDIUM / Should Fix`, kết luận bắt buộc là `CHANGES REQUESTED`).*

### [F-01] — [CRITICAL | HIGH | MEDIUM | LOW] Tiêu đề phát hiện
- **Nguồn phát hiện**: Standards / Spec / Ponytail
- **Phân loại**: Must Fix / Should Fix / Nice to Have
- **Vị trí**: `src/codesight/...:line`
- **Kịch bản lỗi cụ thể (Concrete Failure Case)**: ...
- **Tại sao nguy hiểm (Impact)**: ...
- **Khuyến nghị khắc phục tối giản**: ...
- **Bằng chứng từ code (Evidence)**: ...

## 5. Kết Luận Review
- **Trạng thái**: **`CHANGES REQUESTED`** *(hoặc **`APPROVED`** chỉ khi `0` Must Fix & `0` Should Fix)*
- **Danh sách gom nhóm đầu việc cần xử lý ở Bước 4 (đã khử trùng lặp)**: ...
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Chọn **đúng 1 trong 2 hộp** dưới đây tùy theo kết luận thực tế trong `review-01.md`, sau đó **DỪNG LƯỢT NGAY (`END TURN`)**:

* **Trường hợp 1 — Có lỗi cần sửa (`CHANGES REQUESTED`)**:
```markdown
---
### ⏩ Bước tiếp theo: Khắc Phục Lỗi Review (Bước 4 — Fix)
- **🤖 Model khuyến nghị trên Antigravity**: **Claude Sonnet 5.5 (Medium / High)** hoặc **Gemini 4 Argon (Medium / High)** / **Orchestrator (Argon)** *(nâng lên **Gemini 4 Carbon (Medium)** hoặc **Claude Opus 5.5 (Medium)** nếu sửa Lua CAS race condition, `NFR-3` leak hoặc AST/diff phức tạp)* — *Sửa gọn đúng root-cause, nhanh và không đẻ thêm code thừa.*
- **💡 Mẹo ngữ cảnh**: Có thể quay lại cửa sổ chat Implement hoặc tiếp tục ngay tại đây sau khi chọn Model.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 4 (Fix) cho task <TICKET_ID> theo .ai-workflow/prompts/04-fix.md. Xử lý tận gốc các findings trong .ai-workflow/tasks/<TICKET_ID>/review-01.md, bổ sung regression tests và lưu báo cáo vào fix-report.md.`
```

* **Trường hợp 2 — Hoàn toàn sạch (`APPROVED`)**:
```markdown
---
### ⏩ Bước tiếp theo: Tái Thẩm Định Cuối Cùng & Cổng Cơ Học (Bước 5 — Final Review)
- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Argon (High / Medium)** *(hoặc **Claude Sonnet 5.5 (Medium)**)* — *Chuyển Jira sang `Verify / Test` (`31`), chạy `preclose_audit.py` cùng toàn bộ Quality Gates và chốt `final-review.md`.*
- **💡 Mẹo ngữ cảnh**: Tiếp tục ngay tại đây hoặc trong cửa sổ mới.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 5 (Final Review) cho task <TICKET_ID> theo .ai-workflow/prompts/05-final-review.md và lưu kết quả vào final-review.md.`
```

---

## Hard Rules
- **Không sửa bất kỳ file code, test, migration hay docs nào** (chỉ ghi duy nhất `review-01.md`).
- Sau khi in hộp chuyển tiếp: **DỪNG LẠI NGAY LẬP TỨC (`END TURN`)**.
