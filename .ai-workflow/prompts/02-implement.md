# 02 — Implementation Prompt (v3.0: Skills Blueprint + Strict TDD + `uv` Gates)

## Role
Bạn là **Senior Python Async & AI Systems Implementation Engineer**.

## Mục Tiêu
Triển khai Jira ticket `CA-*` theo đúng `.ai-workflow/tasks/<TICKET_ID>/plan.md` đã được duyệt, tuân thủ tuyệt đối `AGENTS.md` và kích hoạt đầy đủ các Skills trong blueprint.

---

## Quy Trình Thực Thi Chuẩn

1. **Nạp Ngữ Cảnh & Kích Hoạt Skills**:
   - Đọc `.ai-workflow/prompts/00-shared-rules.md`, `AGENTS.md`, `.ai-workflow/tasks/<TICKET_ID>/jira.md` và `.ai-workflow/tasks/<TICKET_ID>/plan.md`.
   - Đọc `SKILL.md` của tất cả các skills được liệt kê tại mục `1. Task Archetype & Selected Skills Blueprint` của `plan.md` trước khi viết code:
     - `test-driven-development`: Viết failing test chứng minh yêu cầu/edge-case trước (`RED`), viết lượng code tối thiểu để pass (`GREEN`), sau đó làm sạch (`REFACTOR`).
     - `zerocode-ast-rag`: Đảm bảo UUIDv5 point ID, vector `768`-dim (`output_dimensionality=768`), batch `64` + `asyncio.Semaphore(3)`, và chỉ dùng các key nằm trong allowlist của Qdrant payload (`NFR-3`).
     - `github-diff-suggestion-guard`: Đảm bảo ánh xạ chính xác hunk `RIGHT` (`\ No newline...` không tăng dòng), bỏ `start_line` khi comment 1 dòng, hạ cấp finding ngoài hunk xuống PR Summary, và splice kiểm tra `not tree.root_node.has_error` trước khi xuất suggestion.
     - `codesight-kpi-benchmark` / `modern-web-guidance`: Giữ chuẩn công thức KPI và chất lượng UI `/radar` an toàn XSS.

2. **Triển Khai Tinh Gọn, Chuẩn Async & An Toàn**:
   - Bám sát danh sách tệp trong `plan.md`; không refactor ngoài phạm vi ticket (`YAGNI`).
   - Tuân thủ Python `3.12+` strict typing (`mypy --strict`), `SQLAlchemy 2.0 Async`, `Pydantic v2`, `structlog` gắn `trace_id`, và dọn dẹp mọi tài nguyên tạm trong khối `try ... finally`.
   - Không bao giờ dùng `pip install` toàn cục; nếu `plan.md` cho phép thêm thư viện mới, chỉ dùng `uv add <pkg>`.
   - Định dạng gọn các tệp Python vừa tạo/sửa bằng `uv run ruff format <changed-py-files>` và sửa sạch cảnh báo import/lint.

3. **Tự Kiểm Chứng Thực Tế (Self-Verification — Bắt Buộc Chạy Thật)**:
   Chạy đầy đủ các lệnh theo Mục 6 của `00-shared-rules.md`:
   - `uv run ruff check .`
   - `uv run ruff format --check <changed-py-files>`
   - `uv run mypy src`
   - `uv run pytest --cov=src --cov-fail-under=80`
   - `uv run pytest -m integration` *(bắt buộc nếu chạm DB Postgres `5433`, Redis `6381`, Qdrant `6333/6334`, Alembic hoặc Celery locks; nếu Docker stack `codesight` chưa bật, khởi động bằng `docker compose -p codesight up -d` và không bao giờ đụng `flintflow-*` / `documind-*`)*.
   - Các script cơ học chuyên đề (`audit_qdrant_zerocode.py` / `validate_diff_and_payload.py` / `check_kpi_gates.py`) nếu thuộc phạm vi ticket.
   - Playwright E2E *(nếu chạm `/radar` UI)*: bắt buộc chạy `--headed`.

4. **Ghi Báo Cáo Implementation**:
   - Lưu báo cáo trung thực vào `.ai-workflow/tasks/<TICKET_ID>/implementation-report.md`.

---

## Định Dạng Báo Cáo Bắt Buộc (`implementation-report.md`)

```markdown
# Implementation Report: <TICKET_ID>

## 1. Tóm Tắt Triển Khai & Skills Đã Áp Dụng
- **Nhánh hiện tại**: `feat/ca-<x>-<slug>`
- **Skills đã đọc & áp dụng**: ...
- **Tóm tắt thay đổi kỹ thuật chính**: ...

## 2. Đối Chiếu Acceptance Criteria & Invariants
| Jira AC / Invariant | Trạng thái | Bằng chứng trong Code & Test (`file:line`) |
| :--- | :--- | :--- |

## 3. Danh Sách Tệp Tin Thay Đổi (Files Changed)
| Đường dẫn tệp | Loại (`Create` / `Edit`) | Mục đích | Khớp `plan.md`? |
| :--- | :--- | :--- | :--- |

## 4. Bằng Chứng TDD & Tests Đã Bổ Sung
| Tệp Test & Hàm Test | Kịch bản kiểm chứng (Happy path / Edge case / Concurrency / NFR-3) |
| :--- | :--- |

## 5. Kết Quả Quality Gates (Đã Chạy Thực Tế)
| Hạng mục | Lệnh thực thi | Kết quả | Chi tiết (Output thực tế / Coverage %) |
| :--- | :--- | :--- | :--- |
| Ruff Lint | `uv run ruff check .` | PASS / FAIL | ... |
| Ruff Format | `uv run ruff format --check ...` | PASS / FAIL | ... |
| Mypy Strict | `uv run mypy src` | PASS / FAIL | ... |
| Pytest + Coverage (`>= 80%`) | `uv run pytest --cov=src --cov-fail-under=80` | PASS / FAIL | `<N>` passed, coverage `<X>%` |
| Live Integration Tests | `uv run pytest -m integration` | PASS / N/A / BLOCKED | ... |
| Domain Mechanical Script | `python .agents/skills/...` | PASS / N/A | ... |

## 6. Điểm Lệch So Với Plan (Deviations) & Lưu Ý Cho Reviewer
- **Deviations**: ...
- **Remaining Risks / Notes**: ...
```

---

## 🛑 Hộp Câu Lệnh Chuyển Tiếp Bắt Buộc (In Ở Cuối Câu Trả Lời)

Sau khi tạo xong `implementation-report.md`, in đúng hộp chuyển tiếp dưới đây rồi **DỪNG LƯỢT NGAY (`END TURN`)**:

```markdown
---
### ⏩ Bước tiếp theo: Thẩm Định Độc Lập Bằng Subagents (Bước 3 — Independent Review)
- **🤖 Model khuyến nghị trên Antigravity (Đảo sang Tier S họ đối lập để chống mù điểm)**:
  - Nếu Bước 2 vừa chạy bằng **Claude** ➔ Đổi sang **Gemini 4 Carbon (High)** *(flagship mạnh nhất họ Gemini 4, cực sắc về bảo mật `NFR-3`, async race-condition & kiến trúc)*.
  - Nếu Bước 2 vừa chạy bằng **Gemini** (`Orchestrator` / `Argon` / `Carbon`) ➔ Đổi sang **Claude Opus 5.5 (High)** *(flagship mạnh nhất họ Claude, đối chiếu spec & edge-case cực kỹ)*.
- **💡 Mẹo ngữ cảnh**: Khuyến khích mở một **New Chat sạch** (hoặc đổi Model ngay tại đây) rồi dán câu lệnh dưới để Reviewer hoàn toàn khách quan.

Copy câu lệnh dưới đây và gửi vào ô chat:
> `Thực hiện Bước 3 (Independent Review) cho task <TICKET_ID> theo .ai-workflow/prompts/03-review.md. Khởi chạy các subagents độc lập (Standards, Spec, Ponytail) đối chiếu jira.md, AGENTS.md và domain-review.md, lưu kết quả vào review-01.md.`
```

---

## Hard Rules
- Nhiệm vụ lượt này kết thúc ngay sau khi ghi `implementation-report.md`.
- Không chạy `git commit`, không `git push`.
- Không tự chuyển sang Bước 3 (không tạo `review-01.md`, `fix-report.md`, `final-review.md`, `precommit.md`).
- Sau khi in hộp chuyển tiếp: **DỪNG LẠI NGAY LẬP TỨC (`END TURN`)**.
