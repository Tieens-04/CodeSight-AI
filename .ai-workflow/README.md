# CodeSight-AI — AI Development Workflow (v3.0: Antigravity Native & Cross-Model Edition)

Quy trình phát triển phần mềm chuẩn hóa theo Jira ticket `CA-*` dành riêng cho **CodeSight-AI** trên **Google Antigravity**, kết hợp:
- **Slash Skill khởi động & điều phối nhanh (`/ai-workflow`)** tại [SKILL.md](../.agents/skills/ai-workflow/SKILL.md).
- **Kỷ luật `1 Lượt = 1 Bước` (Hard Stop)** kèm **Hộp Gợi Ý Câu Lệnh + Model Chuyển Tiếp Tự Động**.
- **Chiến lược Đảo Họ Model (Cross-Model Review)** giữa **Gemini 4** và **Claude 5.5** để vừa triệt tiêu điểm mù khi thẩm định bảo mật/`NFR-3`/concurrency, vừa chia đều 2 bể hạn mức (Quota Pools) trên Antigravity.
- **Tích hợp chặt với cổng cơ học CodeSight-AI**: `uv run` (`ruff`, `mypy`, `pytest --cov >= 80%` branch coverage), [preclose_audit.py](../.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py), và quy trình trạng thái Jira **`21` (`In Progress`) → `31` (`Verify / Test`) → `41` (`Done`)** qua MCP `atlassian-mcp-server` (hoặc `jira-cwb`).

---

## 1. Cách Khởi Động Nhanh Nhất Trên Antigravity

Mở khung chat Antigravity, chọn Model cho Bước 1 (xem bảng mục 3) và gõ **1 câu ngắn gọn**:

```text
/ai-workflow CA-8
```
*(hoặc gõ tự nhiên: `Làm task CA-8 theo .ai-workflow`)*

Từ đó về sau, **ở cuối mỗi bước Agent sẽ tự động in ra một Hộp Chuyển Tiếp** gồm:
1. **Model khuyên chọn** trên thanh Model Selection cho bước kế tiếp.
2. **Câu lệnh chính xác** để bạn chỉ việc copy-paste vào ô chat.

Sau khi xong Bước 6 và bạn đã tự chạy lệnh `git commit` trên Terminal, bạn chỉ cần gõ thêm:

```text
/ai-workflow close CA-8
```
để Agent tự lấy `<short-sha>` thật, chạy xác nhận `preclose_audit.py`, đăng **Verification Evidence 5 mục** và chuyển `CA-8` sang **`Done` (`41`)**!

---

## 2. Sơ Đồ Vòng Đời 6 Bước + Chốt Đóng Jira (Human Gates)

```text
[BẠN gõ: "/ai-workflow CA-XX"]
       │
       ▼
[Bước 1: Plan (01-plan.md)]
  • Kéo Jira CA-XX + comments (`atlassian-mcp-server` / `jira-cwb`) ➔ lưu jira.md & chuyển `21` (In Progress)
  • Kiểm tra working tree sạch ➔ tạo nhánh `feat/ca-xx-<slug>` từ `main`
  • Khảo sát code + chọn Dynamic Skills thật ➔ xuất plan.md
       │
       ▼ ──► [GATE 1]: Bạn duyệt plan.md, đổi sang Model Implement & dán prompt gợi ý
[Bước 2: Implement (02-implement.md)]
  • TDD (Red -> Green -> Refactor) theo Skills Blueprint + giữ chặt NFR-3 & port coexistence
  • Chạy thật: `uv run ruff check .` + `mypy src` + `pytest --cov=src --cov-fail-under=80` (+ `-m integration`)
  • Xuất implementation-report.md (KHÔNG commit)
       │
       ▼ ──► [GATE 2]: Đổi sang họ Model Tier S đối lập (khuyên mở New Chat sạch) & dán prompt gợi ý
[Bước 3: Independent Review (03-review.md + domain-review.md)]
  • Quét cả `git diff HEAD` lẫn mọi file mới untracked (`??`)
  • Khởi chạy 3 Subagents độc lập: Standards & Domain + Spec & Test + Ponytail
  • Xuất review-01.md (KHÔNG sửa code)
       │
       ▼ ──► [GATE 3]: Xem kết quả review-01.md
   ┌───┴──────────────────────────────────────┐
   │ CHANGES REQUESTED (Có findings)          │ APPROVED (0 lỗi blocking)
   ▼                                          │
[Bước 4: Fix (04-fix.md)]                     │
  • Vá tận gốc + thêm regression test thật    │
  • Chạy lại bộ `uv run` ➔ xuất fix-report.md │
   │                                          │
   └──────────────► [Bước 5: Final Review (05-final-review.md)] ◄───┘
                      • Chuyển Jira sang `31` (Verify / Test)
                      • Đối chiếu 100% findings RESOLVED & scoped ponytail-review
                      • Chạy thật `uv run` + `preclose_audit.py --ticket CA-XX` ➔ xuất final-review.md
                            │
                            ▼ ──► [GATE 4]: Đạt `READY FOR PRE-COMMIT` ➔ chuyển sang Model nhẹ (Hydrogen/Flash)
                    [Bước 6: Pre-Commit & PR Prep (06-precommit.md)]
                      • Quét sạch git status / secrets / NFR-3 / Docker ports
                      • Soạn PR bằng skill `pr` + khung Verification Evidence 5 mục ➔ xuất precommit.md
                      • In khối lệnh Git để BẠN tự `git commit` & `git push`
                            │
                            ▼ ──► [GATE 5]: BẠN chạy `git commit` (& `git push`), rồi gõ `/ai-workflow close CA-XX`
                    [Bước 7 (Tùy chọn): Close Jira (07-close-jira.md)]
                      • Lấy `<short-sha>` thật từ HEAD + xác nhận lại `preclose_audit.py`
                      • Gọi `jira_add_comment` (5 mục) ➔ chuyển `CA-XX` sang `41` (Done)
```

---

## 3. Thứ Tự Sức Mạnh & Chiến Lược Chọn Model Trên Antigravity

### 3.1. Xếp hạng thực tế các Model trên Antigravity (Từ Mạnh nhất ➔ Nhẹ/Nhanh nhất)
Trong cùng một dòng model, mức suy luận xếp theo thứ tự: **`XHigh` > `High` > `Medium` > `Low`**.

1. **Tier S — Flagship Frontier (Đỉnh nhất về suy luận sâu, kiến trúc & bắt bug hiểm)**:
   - **Gemini 4 Carbon (`High` > `Medium` > `Low`)**: Model mạnh và sâu nhất của Google DeepMind (đặc biệt sắc bén về kiến trúc bất đồng bộ, bảo mật `NFR-3`/HMAC, Redis Lua CAS concurrency và phân tích luồng phức tạp).
   - **Claude Opus 5.5 (`High` > `Medium` > `Low`)**: Siêu mô hình đầu bảng của Anthropic (đối trọng ngang ngửa với Carbon, cực mạnh về coding quy mô lớn và đối chiếu chi tiết từng dòng spec/AC).
2. **Tier S- / A+ — Chế độ Nhạc trưởng Đa Model (Đặc sản tiết kiệm Quota của Antigravity)**:
   - **Orchestrator (Carbon)**: Tự động dùng **Carbon** lập kế hoạch/gỡ ca khó + giao việc sửa file/chạy `uv run` lặp lại cho **Hydrogen** (nhanh hơn ~19%, tiết kiệm ~24% quota mà chất lượng gần ngang Carbon thuần).
   - **Orchestrator (Argon)**: Dùng **Argon** làm nhạc trưởng + **Hydrogen** thực thi.
3. **Tier A — Balanced Frontier Engineering (Chủ lực viết code, TDD hàng ngày)**:
   - **Claude Sonnet 5.5 (`High` > `Medium` > `Low`)** & **Gemini 4 Argon (`High` > `Medium` > `Low`)**.
4. **Tier B — Fast Execution Engine (Siêu nhanh, nhẹ, tiết kiệm quota)**:
   - **Gemini 4 Hydrogen (`XHigh` > `High` > `Medium` > `Low`)**: Dòng model nhẹ nhất họ Gemini 4, tối ưu tốc độ chạy checklist, soạn PR và gọi MCP Jira.
5. **Tier C — Ultra-Light / Phụ trợ**:
   - **Gemini 3.8 Flash (`High` > `Medium` > `Low`)** *(mặc định cho `browser` subagent)* & **GPT-OSS 120B (`Medium`)**.

### 3.2. Bảng chọn Model theo từng bước (Chia đều 2 bể Quota Gemini & Claude)

| Bước | File Prompt | Khuyên dùng số 1 (Cross-Model Chuẩn) | Phương án 2 (Thuần Gemini / Tiết kiệm) | Có nên mở New Chat? |
| :--- | :--- | :--- | :--- | :--- |
| **Bước 1: Plan** | `01-plan.md` | **Gemini 4 Carbon (High)** hoặc **Claude Opus 5.5 (High)** | **Gemini 4 Argon (High)** *(task vừa/nhỏ)* | Bắt đầu chat mới cho mỗi ticket |
| **Bước 2: Implement** | `02-implement.md` | **Claude Sonnet 5.5 (High / Medium)** hoặc **Orchestrator (Carbon)** | **Orchestrator (Argon)** / **Gemini 4 Argon (High)** | Giữ nguyên chat Bước 1 (đổi Model nếu cần) |
| **Bước 3: Review** | `03-review.md` | **Đảo ngược họ Tier S với Bước 2:**<br>• Bước 2 Claude ➔ **Gemini 4 Carbon (High)**<br>• Bước 2 Gemini ➔ **Claude Opus 5.5 (High)** | **Gemini 4 Carbon (Medium / High)** | **Nên mở New Chat sạch** để triệt tiêu thiên kiến người viết code |
| **Bước 4: Fix** | `04-fix.md` | **Claude Sonnet 5.5 (Medium / High)** | **Gemini 4 Argon (Medium / High)** / **Orchestrator (Argon)** | Quay lại chat Bước 2 hoặc dùng tiếp chat hiện tại |
| **Bước 5: Final Review** | `05-final-review.md` | **Gemini 4 Argon (High / Medium)** hoặc **Claude Sonnet 5.5 (Medium)** | **Gemini 4 Argon (Medium)** | Mở New Chat sạch hoặc đổi sang Model Reviewer |
| **Bước 6: Pre-Commit** | `06-precommit.md` | **Gemini 4 Hydrogen (Medium / High)** | **Gemini 3.8 Flash (High)** | Dùng ngay ô chat hiện tại cho nhanh |
| **Bước 7: Close Jira** | `07-close-jira.md` | **Gemini 4 Hydrogen (Low / Medium)** | **Gemini 3.8 Flash (High)** | Gửi ngay sau khi chạy `git commit` |

> 💡 **Lưu ý về Subagents ở Bước 3**: Công cụ `invoke_subagent` trên Antigravity mặc định chạy với `Model: "inherit"` (thừa kế đúng Model bạn đang chọn trên thanh công cụ ở lượt đó). Vì vậy chọn Model Tier S ở Bước 3 giúp cả 3 Subagents (`Standards & Domain`, `Spec & Test`, `Ponytail`) đều suy luận sắc bén nhất.

---

## 4. Cấu Trúc Thư Mục & Chính Sách Git

Bộ khung quy trình được commit vào Git để đồng bộ cùng dự án, trong khi toàn bộ hồ sơ nháp từng ticket trong `.ai-workflow/tasks/CA-XX/` được `.gitignore` tự động để không làm bẩn PR:

```text
.ai-workflow/
├── README.md                      # Tài liệu hướng dẫn tổng quan này (tracked)
├── prompts/                       # Bộ prompt chuẩn 7 tệp (tracked)
│   ├── 00-shared-rules.md         # Luật chung: 1 Turn = 1 Step, nhánh feat/ca-*, diff chưa commit, cổng uv
│   ├── 01-plan.md                 # Bước 1: Kéo Jira CA-* + chuyển 21 + tạo nhánh từ main + lập plan.md
│   ├── 02-implement.md            # Bước 2: TDD + code + uv gates + xuất implementation-report.md
│   ├── 03-review.md               # Bước 3: 3 Subagents review độc lập + xuất review-01.md
│   ├── 04-fix.md                  # Bước 4: Vá root-cause + regression tests + xuất fix-report.md
│   ├── 05-final-review.md         # Bước 5: Chuyển Jira 31 + preclose_audit.py + xuất final-review.md
│   ├── 06-precommit.md            # Bước 6: Vệ sinh Git + soạn PR + xuất precommit.md & lệnh commit
│   └── 07-close-jira.md           # Bước 7: Lấy <short-sha> thật + đăng Evidence 5 mục + đóng Jira 41
├── rules/
│   └── domain-review.md           # Checklist kiến trúc & bảo mật chuyên sâu của CodeSight-AI (tracked)
└── tasks/
    ├── .gitkeep                   # Giữ cấu trúc thư mục rỗng trên Git (tracked)
    └── CA-XX/                     # Hồ sơ làm việc của từng ticket (gitignored tự động)
        ├── jira.md
        ├── plan.md
        ├── implementation-report.md
        ├── review-01.md
        ├── fix-report.md          # (Sinh ra khi review-01.md có findings)
        ├── final-review.md
        └── precommit.md
```

---

## 5. Bảng Tra Nhanh Câu Lệnh Các Bước (Cheat Sheet)

| Tình huống | Câu lệnh gửi vào Antigravity |
| :--- | :--- |
| **Bắt đầu ticket mới (Bước 1)** | `/ai-workflow CA-XX` |
| **Chạy Bước 2 (Implement)** | `Duyệt plan. Thực hiện Bước 2 (Implement) cho task CA-XX theo .ai-workflow/prompts/02-implement.md và các skills trong blueprint của plan.md.` |
| **Chạy Bước 3 (Review độc lập)** | `Thực hiện Bước 3 (Independent Review) cho task CA-XX theo .ai-workflow/prompts/03-review.md. Khởi chạy các subagents độc lập (Standards, Spec, Ponytail) đối chiếu jira.md, AGENTS.md và domain-review.md, lưu kết quả vào review-01.md.` |
| **Chạy Bước 4 (Fix lỗi review)** | `Thực hiện Bước 4 (Fix) cho task CA-XX theo .ai-workflow/prompts/04-fix.md. Xử lý tận gốc các findings trong .ai-workflow/tasks/CA-XX/review-01.md, bổ sung regression tests và lưu báo cáo vào fix-report.md.` |
| **Chạy Bước 5 (Final Review)** | `Thực hiện Bước 5 (Final Review) cho task CA-XX theo .ai-workflow/prompts/05-final-review.md. Kiểm tra lại toàn bộ findings từ review-01.md và fix-report.md, chạy preclose_audit.py cùng các Quality Gates và lưu kết quả vào final-review.md.` |
| **Chạy Bước 6 (Pre-Commit & PR)** | `Thực hiện Bước 6 (Pre-Commit) cho task CA-XX theo .ai-workflow/prompts/06-precommit.md. Dùng skill pr soạn nội dung PR, chuẩn bị khung Verification Evidence cho Jira và lưu báo cáo vào precommit.md.` |
| **Chốt đóng Jira sau khi commit (Bước 7)** | `/ai-workflow close CA-XX` |
