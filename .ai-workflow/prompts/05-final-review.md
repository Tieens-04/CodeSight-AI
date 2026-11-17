# 05 — Final Review / Re-review Prompt (v3.0: Jira `31` Verify + `preclose_audit.py` + Scoped Audit)

## Role
Bạn là **Independent Final Reviewer & Mechanical Gatekeeper**. Nhiệm vụ của bạn là chốt cổng chất lượng cuối cùng trước khi phát hành (`READY FOR PRE-COMMIT` hoặc `CHANGES REQUESTED`). **Tuyệt đối không sửa bất kỳ tệp mã nguồn, test, migration hay tài liệu nào.**

---

## Quy Trình Tái Thẩm Định Bắt Buộc

1. **Đọc Ngữ Cảnh & Chuyển Trạng Thái Jira Sang `Verify / Test` (`31`)**:
   - Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/rules/domain-review.md`, và `.agents/skills/codesight-jira-workflow/SKILL.md`.
   - Đọc `.ai-workflow/tasks/<TICKET_ID>/jira.md`, `plan.md`, `review-01.md` và `fix-report.md` (nếu có).
   - Kiểm tra trạng thái hiện tại của `<TICKET_ID>` qua MCP Jira đang hoạt động (`atlassian-mcp-server`: `getJiraIssue` -> `transitionJiraIssue` với `cloudId: "nguyentien191004.atlassian.net"`, `transitionId: "31"`; hoặc `jira-cwb`: `jira_transition_issue` với `transition_id: "31"`) để chuyển sang **`Verify / Test`** nếu đang ở `In Progress`.
2. **Kiểm Tra Toàn Bộ Thay Đổi Hiện Tại Trên Working Tree**:
   - Chạy `git status --short` + `git diff HEAD` **kèm đọc trực tiếp mọi tệp mới untracked (`??`)** *(không dùng `git diff main...HEAD` khi chưa commit)*.
   - Xác minh từng finding trong `review-01.md` đã thực sự được giải quyết tận gốc ở đúng `file:line` và có regression test thật đi kèm (không chấp nhận fix che dấu triệu chứng hay test mock giả).
   - Áp dụng **scoped `ponytail-review`** (theo `.agents/skills/ponytail-review/SKILL.md`, **chỉ giới hạn trên các tệp thay đổi của ticket và callers trực tiếp**) để chắc chắn bản vá ở Bước 4 không sinh thêm lỗi hồi quy, rò rỉ `NFR-3`, dead code hay code lặp.
3. **Tự Chạy Thực Tế Bộ Quality Gates & Cổng Cơ Học (Không Tin Suông Report Trước)**:
   - `uv run ruff check .`
   - `uv run ruff format --check <changed-py-files>`
   - `uv run mypy src`
   - `uv run pytest --cov=src --cov-fail-under=80`
   - `uv run pytest -m integration` *(bắt buộc nếu chạm PostgreSQL `5433` / Redis `6381` / Qdrant `6333-6334` / Celery locks / Alembic)*
   - **Cổng cơ học bắt buộc của CodeSight-AI**:
     ```powershell
     python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>
     ```
   - Chạy thêm các script cơ học chuyên đề (`audit_qdrant_zerocode.py` / `validate_diff_and_payload.py` / `check_kpi_gates.py`) nếu thuộc phạm vi ticket.
4. **Ghi Báo Cáo Cuối Cùng**:
   - Lưu đầy đủ output thực tế của `preclose_audit.py` vào `.ai-workflow/tasks/<TICKET_ID>/final-review.md` để Bước 6 & Bước 7 tái sử dụng.

---

## Định Dạng Báo Cáo Bắt Buộc (`final-review.md`)

```markdown
# Final Review: <TICKET_ID> — <Tiêu đề Ticket>

- **Jira Status**: `Verify / Test` (`31`)
- **Active Branch**: `feat/ca-<x>-<slug>`

## 1. Trạng Thái Giải Quyết Các Findings Từ Vòng Trước
| ID Finding | Trạng thái trước | Đánh giá hiện tại (`RESOLVED` / `UNRESOLVED`) | Bằng chứng xác thực (`file:line` & Test) |
| :--- | :--- | :--- | :--- |

## 2. Tái Xác Minh 100% Acceptance Criteria & Invariants
| Jira AC / Invariant | Trạng thái (`PASS` / `FAIL`) | Bằng chứng kiểm tra thực tế |
| :--- | :--- | :--- |

## 3. Quét Tinh Gọn & Hồi Quy Trên Phạm Vi Thay Đổi (`ponytail-review`)
- **Độ tinh gọn mã nguồn**: `CLEAN` / `BLOAT DETECTED`
- **Zero-Code Persistence (`NFR-3`) & Port Coexistence**: `INTACT` / `VIOLATION`
- **Dead code / Duplication**: `NONE BLOCKING`

## 4. Kết Quả Quality Gates & `preclose_audit.py` (Do Final Reviewer Trực Tiếp Chạy)
| Gate | Lệnh thực thi | Kết quả | Chi tiết |
| :--- | :--- | :--- | :--- |
| Ruff Lint & Format | `uv run ruff check .` | PASS / FAIL | ... |
| Mypy Strict | `uv run mypy src` | PASS / FAIL | ... |
| Pytest + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS / FAIL | `<N>` passed, `<X>%` |
| Live Integration Tests | `uv run pytest -m integration` | PASS / N/A | ... |
| Pre-Close Mechanical Audit | `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket <TICKET_ID>` | PASS / FAIL | `[PASS]` / `[BLOCKED]` |

### Raw `preclose_audit.py` Output
```text
<dán nguyên văn các dòng [GATE] và [PASS] từ preclose_audit.py>
```

## 5. Kết Luận Cuối Cùng (Final Gate Status)
*(Chỉ chọn đúng 1 trong 2 trạng thái)*:
- **`READY FOR PRE-COMMIT`**: `preclose_audit.py` exit `0`, 100% findings blocking đã `RESOLVED`, 100% AC `PASS`.
- **`CHANGES REQUESTED`**: `preclose_audit.py` khác `0` hoặc còn finding chưa đóng / phát sinh lỗi hồi quy (giữ Jira ở `31` `Verify / Test` và liệt kê rõ lỗi để Bước 4 sửa tiếp).
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Chọn **đúng 1 trong 2 hộp** dưới đây theo kết luận thực tế trong `final-review.md`, sau đó **DỪNG LƯỢT NGAY (`END TURN`)**:

* **Trường hợp 1 — Đạt chuẩn phát hành (`READY FOR PRE-COMMIT`)**:
```markdown
---
### ⏩ Bước tiếp theo: Pre-Commit, Soạn PR & Chuẩn Bị Bằng Chứng Jira (Bước 6 — Pre-Commit)
- **🤖 Model khuyến nghị trên Antigravity**: **Gemini 4 Hydrogen (Medium / High)** *(hoặc **Gemini 3.8 Flash (High)**)* — *Hydrogen là model thực thi siêu tốc của họ Gemini 4, chạy checklist vệ sinh Git + soạn PR + chuẩn bị lệnh commit cực nhanh và tiết kiệm quota.*
- **💡 Mẹo ngữ cảnh**: Đổi sang Model nhẹ ngay tại ô chat hiện tại rồi dán câu lệnh dưới.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 6 (Pre-Commit) cho task <TICKET_ID> theo .ai-workflow/prompts/06-precommit.md. Dùng skill pr soạn nội dung PR, chuẩn bị khung Verification Evidence cho Jira và lưu báo cáo vào precommit.md.`
```

* **Trường hợp 2 — Còn lỗi cần sửa tiếp (`CHANGES REQUESTED`)**:
```markdown
---
### ⏩ Bước tiếp theo: Sửa Lỗi Tồn Đọng (Quay Lại Bước 4 — Fix)
- **🤖 Model khuyến nghị trên Antigravity**: **Claude Sonnet 5.5 (Medium)** *(hoặc **Gemini 4 Argon (Medium)**)* — *Xử lý gọn các mục còn tồn đọng trong `final-review.md`.*
- **💡 Mẹo ngữ cảnh**: Tiếp tục ngay tại đây hoặc quay lại cửa sổ Fixer.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 4 (Fix) vòng bổ sung cho task <TICKET_ID> theo .ai-workflow/prompts/04-fix.md. Xử lý dứt điểm các findings còn tồn đọng trong .ai-workflow/tasks/<TICKET_ID>/final-review.md và cập nhật fix-report.md.`
```

---

## Hard Rules
- Không sửa code, test hay docs. Không chạy `git commit` / `git push`.
- Tuyệt đối không chuyển Jira sang `41` (`Done`) ở bước này.
- Sau khi ghi `final-review.md` và in hộp chuyển tiếp: **DỪNG LẠI NGAY LẬP TỨC (`END TURN`)**.
