# PRODUCT OVERVIEW: AUTONOMOUS PR REVIEWER & TECHNICAL DEBT RADAR

**Phiên bản:** 1.0.0  
**Tác giả:** [Tên của bạn]  
**Đối tượng mục tiêu:** Kỹ sư Phần mềm (Software Engineers), Tech Leads, DevOps Teams  
**Trạng thái:** Sẵn sàng phát triển (Ready for Development)

---

## 1. Tóm tắt điều hành (Executive Summary)

**Autonomous PR Reviewer & Technical Debt Radar** là một hệ thống AI Agent tự động hóa quy trình kiểm duyệt mã nguồn (Code Review) và giám sát nợ kỹ thuật trên nền tảng GitHub/GitLab. Khác với các công cụ phân tích tĩnh truyền thống (Static Analysis) chỉ dựa trên quy tắc cứng nhắc, hệ thống sử dụng **Cây cú pháp trừu tượng (AST)** kết hợp với **Tìm kiếm ngữ nghĩa (Code RAG)** và **Kiến trúc Multi-Agent** để thấu hiểu ngữ cảnh toàn bộ dự án, từ đó phát hiện các lỗ hổng bảo mật sâu, điểm nghẽn hiệu năng và vi phạm kiến trúc ngay khi một Pull Request (PR) được tạo.

---

## 2. Bối cảnh & Vấn đề cần giải quyết (Problem Statement)

Trong quy trình phát triển phần mềm hiện đại (CI/CD), bước Code Review thường trở thành nút thắt cổ chai (bottleneck):

1. **Quá tải cho Senior Engineer:** Các kỹ sư có kinh nghiệm mất 20–30% thời gian làm việc chỉ để đọc và phản hồi các lỗi lặp đi lặp lại trên PR của thành viên khác.
2. **Hạn chế của công cụ truyền thống:** Các công cụ như SonarQube, ESLint, hay Flake8 rất tốt trong việc bắt lỗi cú pháp (formatting, unused variables), nhưng hoàn toàn "mù" trước logic nghiệp vụ, lỗ hổng bảo mật chuỗi (cross-file vulnerabilities) hoặc sự sai lệch so với tài liệu thiết kế hệ thống.
3. **Nhiễu thông tin từ LLM thông thường:** Việc ném toàn bộ `git diff` vào ChatGPT thường dẫn đến tình trạng "ảo giác" (hallucination), thiếu ngữ cảnh các file liên quan, và đưa ra hàng loạt nhận xét sáo rỗng gây khó chịu cho lập trình viên (Alert Fatigue).
4. **Tích lũy Nợ kỹ thuật (Technical Debt):** Các đoạn code "chạy được nhưng khó bảo trì" thường bị bỏ qua dưới áp lực deadline, dẫn đến chi phí sửa chữa khổng lồ về sau.

---

## 3. Giải pháp & Đề xuất giá trị (Value Proposition)

Hệ thống hoạt động như một **AI Staff Engineer** túc trực 24/7 trên repository của bạn:

* **Phân tích nhận biết ngữ cảnh (Context-Aware Review):** Không chỉ đọc những dòng code thay đổi, hệ thống dùng `tree-sitter` truy vết các hàm gọi tới nó (callers/callees) và đối chiếu với tài liệu kiến trúc của dự án.
* **Phản hồi chính xác đến từng dòng code (Line-level Actionable Feedback):** Tự động bình luận trực tiếp vào đúng vị trí dòng code trên giao diện GitHub PR kèm theo đoạn mã đề xuất sửa lỗi (GitHub Suggestion Block) có thể bấm "Commit suggestion" ngay lập tức.
* **Lọc nhiễu thông minh (Zero-Noise Guarantee):** Sử dụng cơ chế tự phản biện (Self-Reflection Agent) để loại bỏ các góp ý không cần thiết, chỉ giữ lại các cảnh báo có độ tin cậy cao về Bảo mật, Hiệu năng và Kiến trúc.
* **Định lượng Nợ kỹ thuật (Technical Debt Scoring):** Chấm điểm chất lượng PR trên thang điểm 100 và cảnh báo nếu PR làm gia tăng độ phức tạp thuật toán hoặc phá vỡ cấu trúc module.

---

## 4. Chân dung người dùng mục tiêu (User Personas)

| Vai trò | Nỗi đau chính (Pain Points) | Lợi ích nhận được từ sản phẩm |
| :--- | :--- | :--- |
| **Junior / Mid Developer** | Phải chờ đợi lâu để được review; đôi khi mắc lỗi bảo mật hoặc hiệu năng cơ bản mà không nhận ra. | Nhận phản hồi tức thì sau 15–30 giây mở PR; học hỏi được các best practices thông qua giải thích chi tiết và gợi ý sửa lỗi từ AI. |
| **Senior Developer / Tech Lead** | Mất nhiều thời gian bắt lỗi vặt; khó kiểm soát việc tuân thủ kiến trúc khi team mở rộng quy mô. | Giảm 60% thời gian review thủ công; chỉ cần tập trung đánh giá tính đúng đắn của nghiệp vụ cốt lõi. |
| **Engineering Manager** | Không có bức tranh tổng quan về chất lượng code và mức độ phình to của nợ kỹ thuật qua từng sprint. | Theo dõi được xu hướng chất lượng PR, tỷ lệ lỗi bảo mật và chỉ số nợ kỹ thuật thông qua báo cáo tổng hợp. |

---

## 5. Phạm vi tính năng cốt lõi (Core Features & Functional Requirements)

### Giai đoạn 1: MVP (Minimum Viable Product)
* **FR-01 — Tích hợp GitHub App & Webhook:** Lắng nghe sự kiện `pull_request` (`opened`, `synchronize`) và xác thực chữ ký bảo mật `HMAC-SHA256`.
* **FR-02 — Bộ phân tích Diff thông minh (Smart Diff Parser):** Tải `git diff`, tự động loại bỏ các file không chứa logic nghiệp vụ (như `package-lock.json`, `*.svg`, file migration tự sinh) và ánh xạ chính xác tọa độ dòng code sang định dạng của GitHub API.
* **FR-03 — Lập chỉ mục mã nguồn bằng AST (AST-based Code Indexing):** Sử dụng `tree-sitter` để chia nhỏ toàn bộ repository thành các khối ngữ nghĩa (Functions, Classes, Methods) và lưu trữ vector embedding vào Qdrant.
* **FR-04 — Luồng kiểm duyệt Multi-Agent (LangGraph Workflow):**
  * *Context Agent:* Truy xuất các hàm liên quan và coding convention từ Vector DB.
  * *Security & Performance Auditor:* Kiểm tra lỗ hổng (OWASP Top 10, lộ secret, SQLi) và nút thắt hiệu năng (truy vấn N+1, độ phức tạp vòng lặp cao).
  * *Critic Agent:* Đánh giá lại các phát hiện, chấm điểm độ nghiêm trọng (`CRITICAL`, `WARNING`, `INFO`) và loại bỏ cảnh báo giả.
* **FR-05 — Đăng tải Review tự động:** Gửi một bản review tổng hợp lên GitHub PR bao gồm bảng tóm tắt chất lượng PR ở phần bình luận chung và các inline-comment kèm khối code gợi ý sửa (` ```suggestion `).

### Giai đoạn 2: Mở rộng nâng cao (Post-MVP)
* **FR-06 — Tương tác hội thoại trên PR (Interactive Chat):** Cho phép lập trình viên reply trực tiếp vào bình luận của Bot (ví dụ: `@ai-reviewer giải thích rõ hơn tại sao đoạn này gây rò rỉ bộ nhớ?`) để thảo luận thêm.
* **FR-07 — Tự động cập nhật Vector DB:** Đồng bộ lại chỉ mục AST mỗi khi có PR được merge vào nhánh `main`.
* **FR-08 — Bảng điều khiển giám sát (Observability Dashboard):** Tích hợp Langfuse theo dõi chi phí token, độ trễ xử lý mỗi PR và tỷ lệ chấp nhận gợi ý (Acceptance Rate) của lập trình viên.

---

## 6. Yêu cầu phi chức năng (Non-Functional Requirements)

1. **Hiệu năng & Độ trễ (Performance & Latency):**
   * Endpoint nhận Webhook phải phản hồi mã `202 Accepted` trong vòng **< 300ms** để tránh timeout từ GitHub.
   * Thời gian hoàn tất phân tích và đăng comment cho một PR kích thước trung bình (< 500 dòng thay đổi) phải đạt **< 30 giây**.
2. **Khả năng mở rộng & Độ tin cậy (Scalability & Reliability):**
   * Xử lý bất đồng bộ thông qua hàng đợi phân tán (Celery + Redis), hỗ trợ tự động thử lại (Exponential Backoff) khi gặp giới hạn tốc độ (Rate Limit) từ LLM Provider.
3. **Bảo mật & Quyền riêng tư (Security & Privacy):**
   * Mã nguồn của khách hàng không được lưu trữ vĩnh viễn trên ổ đĩa (sử dụng thư mục tạm trong bộ nhớ và xóa ngay sau khi xử lý).
   * Toàn bộ khóa bí mật (GitHub Private Key, LLM API Keys) được quản lý qua biến môi trường chuẩn bảo mật.
4. **Tính khả quan sát (Observability):**
   * Mọi bước chạy của từng Agent phải được ghi log có cấu trúc (Structured JSON Logging) kèm `trace_id` để dễ dàng truy vết lỗi.

---

## 7. Kiến trúc hệ thống & Công nghệ sử dụng (Technical Architecture)

```text
+-------------------+       Webhook (POST)        +-----------------------+
|   GitHub / GitLab | --------------------------> |   FastAPI Gateway     |
|   (Pull Request)  | <-------------------------- | (HMAC Auth & Validate)|
+-------------------+     GitHub REST/GraphQL     +-----------+-----------+
         ^                                                    |
         | Post Inline Comments &                             | Enqueue Task
         | Summary Report                                     v
+--------+----------+                             +-----------+-----------+
|  Critic & GitHub  |                             |     Redis Broker      |
|  Commenter Agent  |                             |   (Celery Queue)      |
+--------+----------+                             +-----------+-----------+
         ^                                                    |
         | Filtered Findings                                  | Consume Task
+--------+----------+                             +-----------v-----------+
| Security & Perf   | <---- Context & AST ------> |   LangGraph Worker    |
|  Auditor Agents   |                             | (Multi-Agent Engine)  |
+-------------------+                             +-----------+-----------+
                                                              |
                                      +-----------------------+-----------------------+
                                      |                                               |
                                      v                                               v
                          +-----------+-----------+                       +-----------+-----------+
                          |  Tree-sitter Parser   |                       |    Qdrant Vector DB   |
                          | (AST Code Extraction) |                       |   (Code & Docs RAG)   |
                          +-----------------------+                       +-----------------------+
```

### Ngăn xếp công nghệ (Tech Stack)
* **Ngôn ngữ chính:** Python 3.12+
* **Web Framework:** FastAPI, Pydantic v2
* **AI Orchestration:** LangGraph, LangChain Core
* **Code Analysis & RAG:** Tree-sitter (`tree-sitter-python`, `tree-sitter-typescript`), Qdrant
* **Task Queue & Caching:** Celery, Redis
* **LLMOps & Evaluation:** Langfuse, DeepEval
* **Hạ tầng & Triển khai:** Docker, Docker Compose, GitHub Actions

---

## 8. Chỉ số đo lường thành công (Success Metrics & KPIs)

Để chứng minh hiệu quả thực tế của hệ thống (đặc biệt hữu ích khi trình bày trong buổi phỏng vấn kỹ thuật), dự án được đánh giá dựa trên các chỉ số sau:

* **Precision (Độ chính xác của cảnh báo):** $\ge 85\%$ các bình luận do AI tạo ra được đánh giá là hợp lệ (không phải báo động giả).
* **Suggestion Acceptance Rate (Tỷ lệ chấp nhận sửa lỗi):** $\ge 40\%$ các khối code gợi ý (`suggestion`) được lập trình viên nhấn nút chấp nhận trực tiếp trên GitHub.
* **Token Efficiency (Hiệu quả tối ưu chi phí):** Giảm tối thiểu $40\%$ lượng token đầu vào nhờ kỹ thuật lọc file rác và trích xuất ngữ cảnh chọn lọc bằng AST thay vì gửi toàn bộ file.
* **End-to-End Latency:** Độ trễ trung bình từ lúc mở PR đến khi nhận được review hoàn chỉnh dưới $25$ giây.

---

## 9. Rủi ro kỹ thuật & Phương án giảm thiểu (Risks & Mitigations)

| Rủi ro | Mức độ ảnh hưởng | Giải pháp giảm thiểu |
| :--- | :--- | :--- |
| **Lệch số dòng (Line Number Mismatch)** khiến GitHub API từ chối đăng comment (Lỗi `422`). | Cao | Xây dựng bộ parser chuyên biệt cho định dạng Unified Diff (`@@ -a,b +c,d @@`) và viết bộ Unit Test bao phủ các trường hợp thêm/xóa/sửa nhiều dòng liên tiếp. |
| **Vượt quá giới hạn ngữ cảnh (Context Window Limit)** đối với các PR lớn (> 50 files). | Trung bình | Áp dụng chiến lược Map-Reduce: chia nhỏ PR theo từng module/file độc lập để phân tích song song, sau đó tổng hợp kết quả ở bước cuối. |
| **Chi phí gọi LLM API tăng cao** khi người dùng đẩy commit liên tục lên PR. | Trung bình | Triển khai cơ chế Debounce (chờ 30 giây sau commit cuối cùng mới chạy review) và Cache kết quả phân tích cho những file có mã băm (SHA) không thay đổi. |