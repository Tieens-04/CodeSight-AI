# CodeSight-AI — Domain & Security Review Rules

Áp dụng các danh mục liên quan trực tiếp tới phạm vi của ticket đang review (không ép một ticket nhỏ phải kiểm tra các thành phần chưa chạm tới, nhưng **mọi ticket đều phải giữ vững `NFR-3` và Port Coexistence**).

---

## 1. Zero-Code Persistence (`NFR-3`) — Bất Biến Tối Thượng

Khi ticket chạm tới đọc file từ GitHub, AST parsing, embedding, Qdrant, DB, cache hoặc logging:
- **Cấm lưu mã nguồn thô xuống đĩa hoặc DB/Qdrant**: Không có cột SQL, khóa Redis lâu dài hay trường payload Qdrant nào chứa nội dung code khách hàng (`code`, `raw_code`, `source`, `content`, `body`, `text`, `document`, `snippet`, `docstring` đều bị cấm trong Qdrant payload).
- **Qdrant Payload Allowlist**: Chỉ cho phép `repo_full_name`, `file_path`, `blob_sha`, `language`, `symbol_name`, `symbol_kind`, `start_line`, `end_line`, `cyclomatic_complexity`, `signature` (chỉ dòng khai báo header `<= 300` ký tự, đã cắt bỏ thân hàm/lớp), `called_symbols`, `imports`.
- **Truy xuất trên RAM & Tái sử dụng `core/ephemeral.py`**: Tải blob/diff qua GitHub API trực tiếp vào bộ nhớ (`bytes` / `io.BytesIO`); nếu bắt buộc dùng thư mục tạm cô lập (`/tmp` `128m` `tmpfs` trong Docker), phải dùng đúng `ephemeral_workspace()` / `async_ephemeral_workspace()` và `resolve_safe_subpath()` từ `src/codesight/core/ephemeral.py` (chống path traversal + đảm bảo xóa sạch trong `finally` kể cả trên Windows read-only bits).
- **Structured Logging (`structlog`)**: Không bao giờ log toàn văn file nguồn, patch lớn chứa bí mật khách hàng, GitHub Installation token hay `Authorization` header.

---

## 2. FastAPI Webhook Gateway, Auth & Idempotency (`< 100ms` ACK)

Khi ticket liên quan `/api/v1/webhooks/github` hoặc GitHub App Auth:
- **Xác thực chữ ký `HMAC-SHA256`**: Phải đọc raw request body bytes và so sánh bằng `hmac.compare_digest` trước khi parse nghiệp vụ; thiếu/sai `X-Hub-Signature-256` trả `401`/`403` ngay, tuyệt đối không lộ secret trong thông báo lỗi.
- **Chống trùng `X-GitHub-Delivery`**: Claim khóa idempotency nguyên tử (atomic); nếu sự kiện trùng đã xử lý thì trả `200`/`202` an toàn mà không nhân đôi Celery task; nếu quá trình enqueue gặp lỗi tạm thời, cho phép reclaim sự kiện bị treo (`stale event reclaim`) thay vì kẹt vĩnh viễn.
- **Commit-before-dispatch**: Mọi bản ghi trạng thái DB cần cho worker phải được `commit()` xong trước khi bắn Celery task (tránh race worker đọc DB trước khi transaction của API kịp commit).
- **Ngân sách độ trễ `< 100ms`**: Không gọi GitHub REST API, Qdrant hay Gemini đồng bộ bên trong request handler của webhook; mọi xử lý nặng đẩy sang Celery.
- **GitHub App JWT & Installation Token**: Cache token có kiểm soát theo hạn sử dụng (trừ hao clock skew), tôn trọng `Retry-After` / `X-RateLimit-Reset` khi gặp `403`/`429`.

---

## 3. Celery Worker Reliability, Redis Locks & Cancel-on-Supersede

Khi ticket liên quan hàng đợi bất đồng bộ, Redis 7 hoặc vòng đời PR:
- **Độ tin cậy hàng đợi**: Cấu hình `task_acks_late=True` kết hợp xử lý idempotent để worker crash không làm mất sự kiện, đồng thời retry có exponential backoff + jitter (tôn trọng `Retry-After`).
- **Chiến lược điều độ theo loại sự kiện**:
  - `pull_request.opened` / `reopened`: Đưa vào xử lý ngay (`countdown=0`).
  - `pull_request.synchronize`: Áp dụng **Cancel-on-Supersede** (cập nhật SHA mới nhất qua Redis Lua CAS nguyên tử) + **`5s` debounce** để dập bão force-push liên tiếp.
- **Chốt chặn trước khi xuất bản (`worker/supersede_lock.py`) & Out-of-order SHA Guard**:
  - Tái sử dụng `AsyncSupersedeLock` / `SyncSupersedeLock` và ngoại lệ `PRSupersededError` trong `src/codesight/worker/supersede_lock.py` (không tự viết key Redis rời rạc ngoài `pr_active:{repo_id}:{pr_number}`).
  - Worker bắt buộc gọi `ensure_active(stage="pre_publish")` **ngay trước khi gọi GitHub Review API** để không bao giờ đăng review của commit cũ đè lên commit mới hơn, đồng thời không retry khi bắt gặp `PRSupersededError`.
  - Bảo vệ thứ tự trạng thái khi hai webhook đến lệch thứ tự mạng.
- **Đọc cấu hình `.codesight.yaml` (`config_reader/repo_config.py`)**:
  - Tuân thủ mô hình **Trust-on-Default-Branch** (không đọc cấu hình từ nhánh PR chưa tin cậy để tránh attacker tự tắt rule bảo mật hoặc chèn `ignore_paths`), giới hạn kích thước `<= 64 KiB`, `yaml.safe_load`, và giữ `config_hash` (SHA-256 16-hex) phục vụ cache SHA ở `CA-21`.
- **Truyền ngữ cảnh (`trace_id` / `delivery_id`)**: Giữ xuyên suốt từ FastAPI gateway sang Celery worker logs (`structlog.contextvars`).

---

## 4. Cấu Hình `.codesight.yaml` & PostgreSQL (`SQLAlchemy 2.0 Async` + `Alembic`)

Khi ticket liên quan DB schema, repositories hoặc đọc cấu hình repo khách hàng:
- **Đọc `.codesight.yaml` bền bỉ (Resilient Fallback)**: Parse bằng `yaml.safe_load` (cấm `yaml.load` không an toàn); nếu file không tồn tại, rỗng, sai cú pháp YAML hoặc sai kiểu ở từng trường riêng lẻ, fallback an toàn về giá trị mặc định và tính `config_hash` tất định (deterministic).
- **`SQLAlchemy 2.0 Async`**: Không gọi I/O đồng bộ ẩn (lazy-loading ngoài session async); dùng `upsert` an toàn (`INSERT ... ON CONFLICT`) cho các bảng có nguy cơ ghi đồng thời (như `repositories`, `pull_requests`).
- **Alembic Migrations**: Phải có `downgrade()` đối xứng, đặt index/unique constraint rõ ràng, chạy sạch từ DB trống.

---

## 5. Tree-sitter AST Engine, Embeddings & Qdrant Hybrid RAG

Khi ticket liên quan phân tích cú pháp Python/TypeScript hoặc truy xuất ngữ cảnh:
- **API `tree-sitter >= 0.22`**: Khởi tạo `Parser(Language(tspython.language()))`, `language_typescript()`, `language_tsx()` cho `.py`, `.ts`, `.tsx` (không dùng API cũ `build_library`).
- **Qdrant Collection & UUIDv5**:
  - Kết nối ưu tiên gRPC (`6334`, HTTP `6333`), vector `size=768`, `Distance.COSINE`.
  - Point ID bắt buộc là **UUIDv5** tất định từ `repo:file_path:blob_sha:symbol_name:start_line`.
  - Đánh đủ Keyword Payload Indexes trên `symbol_name`, `called_symbols`, `blob_sha`, `repo_full_name`, `file_path`.
- **Bảo vệ hạn mức Google AI Studio Free Tier (`gemini-embedding-001`)**:
  - Dùng SDK chính thức `google-genai` (`from google import genai`), **không dùng** gói cũ `google-generativeai`.
  - Luôn truyền `output_dimensionality=768` cho cả `RETRIEVAL_DOCUMENT` (lúc index) và `RETRIEVAL_QUERY` (lúc tìm kiếm).
  - Gom lô **`BATCH_SIZE = 64` chunks/call**, giới hạn đồng thời bằng **`asyncio.Semaphore(3)`** kèm exponential backoff khi gặp `429 RESOURCE_EXHAUSTED`.

---

## 6. Unified Diff Mapping (`Zero HTTP 422`) & Syntax-Safe Suggestions

Khi ticket liên quan đọc bản vá git diff hoặc đăng nhận xét lên GitHub Pull Request:
- **Ánh xạ tọa độ Hunk chính xác**:
  - Hunk rút gọn số dòng (`@@ -10 +10 @@`) tương đương count = `1`, trong khi `,0` tường minh (`@@ -1,5 +0,0 @@`) tương đương count = `0`.
  - Dòng đánh dấu `\ No newline at end of file` **tuyệt đối không tăng** `left_line` hay `right_line`.
  - Theo dõi `hunk_id` riêng cho từng khối `@@`; comment nhiều dòng không bao giờ được bắc cầu qua 2 hunk khác nhau.
- **Cấu trúc Payload GitHub Review (`Zero HTTP 422`)**:
  - Comment 1 dòng (`start_line is None` hoặc `start_line == line`): Chỉ gửi `path`, `line`, `side: "RIGHT"`, `body` — **bắt buộc lược bỏ `start_line` và `start_side`**.
  - Comment nhiều dòng (`start_line < line` cùng `hunk_id` ở phía `RIGHT`): Gửi đủ `start_line`, `start_side: "RIGHT"`, `line`, `side: "RIGHT"`.
  - Finding nằm ngoài vùng `valid_right_lines` hoặc vắt qua 2 hunk: Tự động gỡ khỏi mảng `comments[]` và gộp xuống phần tổng kết PR Summary (`### Findings Outside Diff Hunks`).
- **Kiểm định cú pháp khối ```` ```suggestion ```` trên RAM**:
  - Không parse mảnh suggestion rời rạc; phải **splice vào toàn văn nội dung file trên RAM** rồi parse lại bằng `tree-sitter` và kiểm tra `not tree.root_node.has_error` (bắt cả node `ERROR` lẫn token `MISSING`).
  - Nếu không hợp lệ cú pháp: tước bỏ hàng rào ```` ```suggestion ... ``` ```` và giữ lại phần lời giải thích chẩn đoán.

---

## 7. LangGraph Multi-Agent, `LLMFactory` & KPI Anti-Gaming

Khi ticket liên quan pipeline AI review (`CA-17`..`CA-22`) hoặc đánh giá chất lượng (`CA-23`..`CA-28`):
- **Tiết kiệm lượt gọi & Token (`Batch-Grouped Parallel`)**: Gom nhóm thông minh (~3 lệnh gọi LLM/PR) qua `LLMFactory` (`gemini-2.5-flash` / `gemini-2.5-pro`), lọc bỏ sớm lockfiles/generated/vendor files trước khi đưa vào prompt để đạt mức giảm token `>= 40%` và tổng thời gian E2E `< 25s`.
- **Structured Output & Langfuse**: Ràng buộc đầu ra LLM bằng Pydantic v2 schemas, ghi nhận trace/token/latency lên Langfuse một cách an toàn (nếu thiếu khóa Langfuse hoặc mạng lỗi thì degrade nhẹ nhàng, không làm gãy PR review).
- **Chống gian lận Benchmark (`evals/run_benchmark.py`)**:
  - Tập Golden PR `>= 24` PRs (`>= 12` Python + `>= 12` TS/TSX), bắt buộc có **ít nhất `4` PR sạch hoàn toàn (zero-defect)** để phạt nặng lỗi báo động giả (`False Positives`).
  - Không bao giờ dùng kết quả stub/mock/cache để tuyên bố đạt ngưỡng `Average E2E Latency < 25s` (bắt buộc đo ở chế độ `mode == "live"`).

---

## 8. Radar UI (`GET /radar`), CLI (`codesight`) & Docker Port Coexistence

- **Radar Dashboard (`Jinja2` + `Chart.js`)**: Luôn bật autoescape của Jinja2 để chống XSS khi hiển thị tên repo, tiêu đề PR hoặc tóm tắt lỗi; xử lý mượt trạng thái chưa có dữ liệu (`empty state`).
- **Cô lập Cổng Docker Tuyệt Đối**:
  - Chỉ sử dụng các cổng chuẩn của CodeSight-AI (tham số hóa qua `.env`): Postgres `5433`, Redis `6381`, Qdrant `6333`/`6334`, FastAPI `8082`.
  - Không bao giờ chạm tới các cổng dành riêng cho **FlintFlow** (`3001`, `6379`, `8080`) và **DocuMind** (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`).
