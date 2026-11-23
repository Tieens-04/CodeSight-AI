# Các lỗi phát hiện khi review Sprint 2

Báo cáo ngày 23/11/2026 cho Sprint 2 của CodeSight-AI (CA-11 đến CA-16), tại commit `5d4c057` trên nhánh `feat/ca-16-sprint2-remediation`. Vòng review ghi nhận **7 vấn đề còn mở**, gồm **5 P1 cần xử lý trước Sprint 3** và **2 P2**.

## P1 — Cần xử lý trước Sprint 3

### S2-01 · CA-14 · Indexer tiếp tục ghi khi mất khóa Redis

Khi Redis báo lỗi lúc lấy khóa, `AsyncRepoIndexLock.hold()` vẫn cho tác vụ vào vùng ghi. Khi tác vụ mất quyền giữ khóa, heartbeat dừng nhưng công việc được bảo vệ vẫn chạy. Nếu hai push cập nhật cùng một file, tác vụ cũ có thể xóa points của tác vụ mới trong Qdrant. PostgreSQL có thể giữ SHA mới trong khi Qdrant còn dữ liệu từ SHA cũ. Kiểm tra giả lập đã xác nhận tác vụ đi vào vùng ghi sau lỗi kết nối Redis. [Lấy khóa](../src/codesight/rag/indexer.py#L746) · [Heartbeat](../src/codesight/rag/indexer.py#L782) · [Xóa points cũ](../src/codesight/rag/indexer.py#L2147)

**Hướng sửa.** Trả lỗi có thể retry khi lấy khóa thất bại. Chặn các lần ghi tiếp theo khi tác vụ mất quyền giữ khóa. Kiểm thử cả lỗi lúc lấy khóa và mất khóa trong lúc đồng bộ.

### S2-02 · CA-16 · CLI đánh giá cập nhật SHA dùng chung với worker

CLI đánh giá dùng collection Qdrant thử nghiệm và PostgreSQL từ cấu hình ứng dụng. Lượt index ghi `last_indexed_sha` vào PostgreSQL. Nếu worker sau đó nhận push cho đúng SHA đó, nó có thể trả `noop_already_indexed` dù collection chính chưa được cập nhật. Đường chạy này được xác nhận qua mã nguồn. [CLI đánh giá](../evals/run_m2_eval.py#L86) · [Ghi cursor](../src/codesight/cli/commands/index.py#L199) · [Worker bỏ qua SHA](../src/codesight/rag/indexer.py#L1908)

**Hướng sửa.** Dùng PostgreSQL cô lập cho CLI đánh giá như bài integration opt-in. Kiểm thử rằng chạy đánh giá giữ nguyên cursor của index chính.

### S2-03 · CA-16 · CLI đánh giá có thể xóa collection đã tồn tại

Nếu `--collection` trùng một collection có sẵn và người chạy bỏ qua `--keep-collection`, khối `finally` sẽ xóa collection đó khi cleanup. Việc xóa có thể xảy ra ngay cả khi phép đánh giá lỗi trước lúc index. Đường chạy này được xác nhận qua mã nguồn. [Tham số CLI](../evals/run_m2_eval.py#L51) · [Cleanup](../evals/run_m2_eval.py#L115)

**Hướng sửa.** Từ chối tên đã tồn tại trước khi bắt đầu. Chỉ xóa collection mà lượt chạy đã tạo thành công. Kiểm thử cả đường thành công và đường lỗi.

### S2-04 · CA-15 và CA-16 · Gate Milestone 2 có thể báo đạt khi truy xuất sai

Phép kiểm tra giả lập trên cả Python và TypeScript cho caller đúng, callee Recall@5 bằng 0 và truy vấn tài liệu ở trạng thái suy giảm. Validator vẫn trả danh sách vi phạm rỗng. Runner cũng xây dựng danh sách caller kỳ vọng từ `called_symbols` đã index, nên cạnh bị AST parser bỏ sót có thể biến mất khỏi đáp án. [Chọn caller kỳ vọng](../src/codesight/rag/live_eval.py#L525) · [Đo recall](../src/codesight/rag/live_eval.py#L939) · [Gate KPI](../src/codesight/rag/live_eval.py#L231)

**Hướng sửa.** Dùng tập đáp án được xác nhận độc lập cho caller, callee và tài liệu kiến trúc của từng repo. Kiểm tra riêng từng nhóm theo tiêu chí nghiệm thu.

### S2-05 · CA-13 và CA-16 · Kiểm toán NFR-3 có thể bỏ qua dòng mã đã lọt vào payload

Runner loại khỏi danh sách canary những dòng thân hàm đã xuất hiện trong payload rồi gọi auditor payload mà không truyền canary. Kiểm tra giả lập đưa một dòng thân hàm vào `imports` và xác nhận bộ chọn giữ lại 0 canary. Bằng chứng này xác nhận điểm mù của phép đo. Kết quả rò rỉ trên dữ liệu thật vẫn chưa được kiểm chứng. [Lọc canary](../src/codesight/rag/live_eval.py#L755) · [Audit payload](../src/codesight/rag/live_eval.py#L1298) · [Quét đĩa](../src/codesight/rag/live_eval.py#L1326)

**Hướng sửa.** Chọn marker thân hàm độc lập với payload. Dùng cùng tập marker cho cả audit payload và quét đĩa. Kiểm thử marker đã xuất hiện trong một trường metadata hợp lệ.

## P2 — Ảnh hưởng độ đầy đủ của chỉ mục và tín hiệu nợ kỹ thuật

### S2-06 · CA-14 · Indexer lưu SHA dù Git tree trả về bị cắt

Khi GitHub trả `truncated=true`, indexer ghi cảnh báo, xử lý phần cây nhận được và vẫn có thể lưu SHA. Những file bị thiếu từ lượt index đó có thể tiếp tục vắng khỏi RAG qua các lượt push nếu chúng không thay đổi. Đường chạy này được xác nhận qua mã nguồn. [Cây bị cắt](../src/codesight/rag/indexer.py#L1397) · [Lưu SHA](../src/codesight/rag/indexer.py#L1579)

**Hướng sửa.** Duyệt tiếp các cây con để hoàn thành lượt index, hoặc giữ nguyên cursor và báo rõ lượt index chưa hoàn tất.

### S2-07 · CA-11 · Tài liệu trong thư mục tests làm mất tín hiệu P_untested

Một diff giả lập thêm 51 dòng logic Python vào `src/service.py` và một dòng văn bản vào `tests/README.md`. Detector trả `added_test_logic_lines=1` và `is_untested=False`. File khớp glob `tests/**` được đếm trước khi lọc loại file, làm sai tín hiệu đầu vào cho điểm nợ kỹ thuật. [Đếm test đi kèm](../src/codesight/ast_engine/complexity.py#L979)

**Hướng sửa.** Chỉ đếm logic từ loại file test được hỗ trợ. Giữ ca hồi quy với README và thêm đối chứng bằng một file test Python.

## Kết quả kiểm tra tại 5d4c057

- Ruff lint và format đạt. Có 108 file đúng format.
- Mypy đạt trên 60 file nguồn.
- Toàn bộ pytest có **447 passed, 1 skipped**, coverage **90,87%**. Suite đã chạy integration trên PostgreSQL, Redis và Qdrant cục bộ của CodeSight.
- Self-test của auditor NFR-3 đạt.
- Bài đánh giá opt-in trên repo thật bị skip. Cấu hình lúc kiểm tra thiếu credential GitHub và khóa Gemini. Kết quả recall, thời gian truy xuất và tỷ lệ giảm token trên hai repo thật còn chờ xác minh. [Bài đánh giá opt-in](../tests/integration/test_week2_dual_lang_rag_e2e.py#L1743)
