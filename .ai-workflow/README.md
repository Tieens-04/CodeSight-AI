# CodeSight-AI — AI Development Workflow (v4.0: `pstack` + Grok Bot + Antigravity Edition)

Hệ thống điều phối phát triển phần mềm theo Jira ticket `CA-*` dành riêng cho **CodeSight-AI** trên **Google Antigravity**, hợp nhất 4 trụ cột:

1. **Kiến trúc 4 Vai trò Chuyên biệt của Lauren Tan (`pstack`)**: `Chief of Staff` (điều phối) → `Researcher` (khảo sát `/how` & `/why` bằng subagent) → `Engineer` (TDD + chống AI slop) → `Reviewer / Checker` (3 subagents độc lập + `Comment-Sicko` + `FAILED/SAW/FIX`) → `Memory Reuse` ([MEMORY.md](MEMORY.md)).
2. **Cơ chế Tiết kiệm Token của Grok Bot (`Wake-ups × Re-reads`)**: Gói bàn giao siêu gọn `state.md` (`<= 20` dòng), bảng `BOARD.md` 5 trạng thái, CLI kiểm chứng 1 lệnh [verify.py](scripts/verify.py) (thay cho 6–7 lệnh rời rạc), cầu chì **`2-Strike Blocked`** và cắt lịch sử chat giữa các vai trò.
3. **Cross-Model Review trên Antigravity**: Đảo họ Model **Gemini 4 ↔ Claude 5.5** giữa người viết code và người chấm bài để triệt tiêu điểm mù về `NFR-3`/concurrency và chia đều 2 bể hạn mức (Quota Pools).
4. **Cổng Cơ Học & Vòng Đời Jira `CA-*` Chuẩn Xác**: Tích hợp `uv run` (`ruff`, `mypy --strict`, `pytest --cov >= 80%` branch coverage), [preclose_audit.py](../.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py), và chuỗi trạng thái Jira **`21` (`In Progress`) → `31` (`Verify / Test`) → `The Undo Gate` (`git commit`) → `41` (`Done`)**.

---

## 1. Các Lệnh Khởi Động Nhanh Trên Antigravity

| Mục đích | Câu lệnh gõ vào ô chat Antigravity |
| :--- | :--- |
| **Khởi động ticket mới (Bước 1)** | `/ai-workflow CA-18` *(hoặc `Làm task CA-18`)* |
| **Xem bảng tiến độ toàn dự án (`THE BOARD`)** | `/ai-workflow board` *(hoặc chạy terminal: `python .ai-workflow/scripts/verify.py board`)* |
| **Kiểm tra / Tiếp tục 1 ticket đang làm dở** | `/ai-workflow CA-18` |
| **Chốt đóng Jira `41` (`Done`) sau khi `git commit` (Bước 7)** | `/ai-workflow close CA-18` |

Ở cuối mỗi bước, Agent tự động cập nhật `state.md` + `BOARD.md` và in ra **Hộp Chuyển Tiếp** gồm: **Model khuyên chọn**, **mẹo mở New Chat / `/compact`** và **câu lệnh copy-paste** cho bước kế tiếp.

---

## 2. Vòng Đời 7 Bước, 4 Vai Trò & Các Cổng Kiểm Soát (`Undo-Priced Gates`)

```text
[BẠN gõ: "/ai-workflow CA-XX"]
       │
       ▼
[Bước 1: Goal, Research & Plan — Chief of Staff + Researcher (01-plan.md)]
  • Đọc MEMORY.md + kéo live Jira CA-XX ➔ jira.md & chuyển `21` (In Progress)
  • Kiểm tra working tree sạch ➔ tạo nhánh `feat/ca-xx-<slug>` từ `main`
  • Ủy quyền subagent `Researcher` chạy `/how` + `/why` (git log) + chọn Canonical Examples
  • Xuất plan.md (Startup Checklist) + state.md + đồng bộ BOARD.md
       │
       ▼ ──► [GATE 1]: Duyệt plan.md ➔ Khuyên mở New Chat sạch (hoặc `/compact`) & đổi sang Model Engineer
[Bước 2: Build — Engineer (02-implement.md)]
  • Đọc gọn state.md + plan.md + Canonical Repo Examples (không đọc lại lịch sử mò file Bước 1)
  • TDD + Anti-Slop (`Comment-Sicko` ngay lúc viết) + đồng bộ docs/
  • Chạy 1 lệnh: `python .ai-workflow/scripts/verify.py check --ticket CA-XX [--integration ...]`
  • Xuất implementation-report.md + cập nhật state.md (`Ready for Review`)
       │
       ▼ ──► [GATE 2]: Mở New Chat sạch + Đảo sang họ Model Tier S đối lập (Gemini ↔ Claude)
[Bước 3: Independent Review — Checker + 3 Subagents (03-review.md)]
  • Chạy `verify.py diff --ticket CA-XX` (gom đủ cả tracked diff lẫn file mới `??` + quét hygiene/ports)
  • 3 Subagents độc lập: `Standards & Domain` + `Spec & Test` + `Ponytail & Comment-Sicko`
  • Xuất review-01.md theo chuẩn đanh thép `FAILED / SAW / FIX` (KHÔNG sửa code)
       │
       ▼ ──► [GATE 3]: Phân nhánh theo kết quả review-01.md
   ┌───┴──────────────────────────────────────────┐
   │ CHANGES REQUESTED                            │ APPROVED (0 Must/Should Fix)
   ▼                                              │
[Bước 4: Fix — Engineer (04-fix.md)]              │
  • Đọc gọn state.md + các khối FAILED/SAW/FIX    │
  • Tái hiện lỗi ➔ vá gốc ➔ chạy verify.py check  │
  • Cầu chì 2-Strike: kẹt 2 lần ➔ `Blocked`       │
   │                                              │
   └──────────────► [Bước 5: Final Gate & Prevention Audit — Final Checker (05-final-review.md)] ◄──┘
                      • Chuyển Jira sang `31` (Verify / Test)
                      • Chạy thật `verify.py check --ticket CA-XX --full-format --preclose [--integration all]`
                      • Tái xác minh 100% AC + Scoped Ponytail/Comment-Sicko + 4-Step Prevention Audit
                      • Xuất final-review.md (`READY FOR PRE-COMMIT` hoặc cầu chì `Blocked` nếu Strikes >= 2)
                            │
                            ▼ ──► [GATE 4]: Đổi sang Model siêu nhẹ (Hydrogen / Flash)
                    [Bước 6: Deliver & Reuse — Release & Memory Keeper (06-precommit.md)]
                      • 6.1 Pre-commit: `verify.py diff` + soạn PR (skill `pr`) + khung Evidence + `MEMORY.md`
                      • 6.2 Commit: commit theo từng phần (atomic commits theo tầng DB/Core/Tests/Docs, không `git add -A` gộp 1 lần) ➔ `<short-sha>` + `preclose_audit.py`
                      • 6.3 Push: `git push -u origin feat/ca-xx-<slug>`
                      • 6.4 Tạo PR: `gh pr create --base main` (hoặc GitHub MCP) ➔ lấy PR URL
                      • 6.5 Đóng task trên Jira: đăng Evidence 5 mục + chuyển `41` (`"Done"`) + `BOARD.md`
                      *(Fallback độc lập cho riêng 6.5 nếu đã tự commit thủ công: `/ai-workflow close CA-XX` — `07-close-jira.md`)*
```

---

## 3. Công Cụ Kiểm Chứng 1 Lệnh (`verify.py`)

Nằm tại [verify.py](scripts/verify.py) (Python `3.12+` stdlib thuần, `0` dependencies mới; `tasks/*` và `BOARD.md` được `.gitignore` che tự động):

```powershell
# 1. Xem phạm vi diff + file mới ?? + quét nhánh, Docker port coexistence, debug/skip, secrets (<0.2s):
python .ai-workflow/scripts/verify.py diff --ticket CA-18

# 2. Chạy trọn bộ Quality Gates (Ruff scoped + Ruff check + Mypy strict + Pytest >=80% + Domain guards):
python .ai-workflow/scripts/verify.py check --ticket CA-18 --integration tests/integration/test_hybrid_retriever.py

# 3. Chạy chốt cổng Final Review (Ruff format toàn repo + preclose_audit.py + Integration):
python .ai-workflow/scripts/verify.py check --ticket CA-18 --full-format --preclose --integration all

# 4. Đồng bộ bảng trạng thái toàn bộ các ticket CA-* (.ai-workflow/BOARD.md — 0 LLM tokens):
python .ai-workflow/scripts/verify.py board

# 5. Tự kiểm thử chính công cụ verify.py:
uv run python .ai-workflow/scripts/test_verify.py -v
```

> 💡 **Quy tắc Playwright Headed**: Mọi bài kiểm thử trình duyệt cho `/radar` luôn chạy ở chế độ hiển thị cửa sổ trình duyệt (`--headed` / `headless: false`) qua `--e2e <spec>`.

---

## 4. Chiến Lược Chọn Model & Cắt Lịch Sử Chat (`Anti-Re-Read Tax`)

Công thức chi phí token thực tế là **`Số vòng gọi công cụ × Độ dài lịch sử chat đọc lại`**. Nhờ đã có `state.md` (`<= 20` dòng) + `MEMORY.md`, bạn **nên mở New Chat sạch (hoặc gõ `/compact`)** mỗi khi chuyển vai:

| Bước | Vai trò (`pstack`) | Model Khuyên Dùng Số 1 (Cross-Model) | Phương Án Tiết Kiệm | Ngữ Cảnh Chat |
| :--- | :--- | :--- | :--- | :--- |
| **Bước 1: Plan** | `Chief` + `Researcher` | **Gemini 4 Carbon (High)** hoặc **Claude Opus 5.5 (High)** | **Gemini 4 Argon (High)** | Mở chat mới cho ticket |
| **Bước 2: Build** | `Engineer` | **Claude Sonnet 5.5 (High / Medium)** hoặc **Orchestrator (Carbon)** | **Orchestrator (Argon)** / **Gemini 4 Argon (High)** | **Mở New Chat sạch** (hoặc `/compact`) |
| **Bước 3: Review** | `Checker` (3 Subagents) | **Đảo họ Tier S với Bước 2:**<br>• Bước 2 Claude ➔ **Gemini 4 Carbon (High)**<br>• Bước 2 Gemini ➔ **Claude Opus 5.5 (High)** | **Gemini 4 Carbon (Medium / High)** | **Bắt buộc mở New Chat sạch** |
| **Bước 4: Fix** | `Engineer` | **Claude Sonnet 5.5 (Medium / High)** | **Gemini 4 Argon (Medium)** / **Orchestrator (Argon)** | **Mở New Chat sạch** (chỉ nạp `state.md` + `review-01.md`) |
| **Bước 5: Final** | `Final Checker` + `OPS` | **Gemini 4 Argon (High / Medium)** hoặc **Claude Sonnet 5.5 (Medium)** | **Gemini 4 Argon (Medium)** | Mở New Chat sạch hoặc `/compact` |
| **Bước 6: Deliver** | `Release & Memory` | **Gemini 4 Hydrogen (Medium / High)** | **Gemini 3.8 Flash (High)** | Dùng ngay chat hiện tại |
| **Bước 7: Close** | `Jira Release Closer` | **Gemini 4 Hydrogen (Low / Medium)** | **Gemini 3.8 Flash (High)** | Gửi ngay sau khi chạy `git commit` |

---

## 5. Cấu Trúc Thư Mục `.ai-workflow` (v4.0)

```text
.ai-workflow/
├── README.md                      # Tài liệu hướng dẫn tổng quan v4.0 (tracked)
├── MEMORY.md                      # Bộ nhớ dài hạn chống mục nát: Decisions, Canonical Sources, Prompts, Lessons (tracked)
├── BOARD.md                       # Bảng quản lý 5 trạng thái Todo | Working | Blocked | Ready for Review | Done (gitignored)
├── scripts/
│   ├── verify.py                  # CLI kiểm chứng 1 lệnh (diff | check | board) — Python 3.12 stdlib (tracked)
│   └── test_verify.py             # Unit tests bảo vệ verify.py (tracked)
├── prompts/
│   ├── 00-shared-rules.md         # Luật nền (state.md <= 20 dòng, 2-Strike Blocked, Undo Gate, verify.py)
│   ├── 01-plan.md                 # Bước 1: Jira 21 + nhánh main + Researcher (/how & /why) + Startup Checklist
│   ├── 02-implement.md            # Bước 2: Canonical Examples + TDD + Comment-Sicko + verify.py check
│   ├── 03-review.md               # Bước 3: verify.py diff + 3 Subagents + khung FAILED / SAW / FIX
│   ├── 04-fix.md                  # Bước 4: Đọc gọn state.md + vá gốc + cầu chì 2-Strike
│   ├── 05-final-review.md         # Bước 5: Jira 31 + full verify.py check --preclose + 4-Step Prevention Audit
│   ├── 06-precommit.md            # Bước 6: PR + khung Evidence Jira + cập nhật MEMORY.md + dừng tại The Undo Gate
│   └── 07-close-jira.md           # Bước 7: Lấy <short-sha> thật + đăng Evidence 5 mục + đóng Jira 41 + lưu trữ BOARD.md
├── rules/
│   └── domain-review.md           # Checklist bất biến kiến trúc & bảo mật chuyên sâu của CodeSight-AI (tracked)
└── tasks/
    ├── .gitkeep                   # Giữ cấu trúc thư mục rỗng trên Git (tracked)
    └── CA-XX/                     # Hồ sơ riêng của từng Jira ticket (gitignored tự động)
        ├── state.md               # Gói bàn giao siêu gọn <= 20 dòng (mới ở v4.0)
        ├── jira.md
        ├── plan.md
        ├── implementation-report.md
        ├── review-01.md
        ├── fix-report.md          # (Chỉ sinh khi review-01.md có findings)
        ├── final-review.md
        └── precommit.md
```

---

## 6. Bảng Tra Nhanh Câu Lệnh Các Bước (Cheat Sheet)

| Tình huống | Câu lệnh gửi vào Antigravity |
| :--- | :--- |
| **Xem bảng tiến độ (`THE BOARD`)** | `/ai-workflow board` |
| **Bắt đầu ticket mới (Bước 1)** | `/ai-workflow CA-XX` |
| **Chạy Bước 2 (Implement)** | `Duyệt plan. Thực hiện Bước 2 (Implement) cho task CA-XX theo .ai-workflow/prompts/02-implement.md.` |
| **Chạy Bước 3 (Review độc lập)** | `Thực hiện Bước 3 (Independent Review) cho task CA-XX theo .ai-workflow/prompts/03-review.md.` |
| **Chạy Bước 4 (Fix lỗi review)** | `Thực hiện Bước 4 (Fix) cho task CA-XX theo .ai-workflow/prompts/04-fix.md.` |
| **Chạy Bước 5 (Final Review)** | `Thực hiện Bước 5 (Final Review) cho task CA-XX theo .ai-workflow/prompts/05-final-review.md.` |
| **Chạy Bước 6 (Pre-commit → Commit → Push → PR → Jira Done)** | `Thực hiện Bước 6 (Deliver: Pre-commit → Commit → Push → Tạo PR → Đóng Jira Done) cho task CA-XX theo .ai-workflow/prompts/06-precommit.md.` |
| **Chốt đóng Jira độc lập (fallback 6.5 nếu đã commit thủ công)** | `/ai-workflow close CA-XX` |
