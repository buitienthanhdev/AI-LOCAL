# Company Brain — kiến trúc, đối chiếu đặc tả, và những gì CHƯA làm

> **Cập nhật 2026-10-09:** Company Brain là *hệ con tri thức* của agent hợp nhất `local-gemini-agent` (HTTP :8765); xem [README.md](../README.md) §1.
> Phần chat/agent trong sơ đồ dưới đây là legacy (vẫn chạy, ngừng phát triển); `Company Brain.bat` chỉ chạy `serve`.

Tài liệu này đối chiếu hệ thống thực tế với bản đặc tả "Corporate AI Local". Mọi dòng đánh dấu ✅ đều có test tự động
(`python -m pytest tests -q`; test chạy model thật: `python -m pytest tests/test_live.py --live -s`).

## Luồng dữ liệu

```
Ổ đĩa ──watchdog──▶ events (bus bền, SQLite) ──worker──▶ files + file_versions + file_events + contributions
   │  (và quét định kỳ làm lưới an toàn)                         │
   │                                                             ▼
   └─ Git repo ──gitsync──▶ git_commits + contributions     chunks (+FTS5)  ──▶ Qdrant (vector, ACL trong payload)
                                                                 │
                         facts (nguồn, hiệu lực, độ tin cậy) ◀───┘  trích bằng LLM (chỉ tài liệu công ty)
                                  │
                  graph (entities/relations) · gaps · people/contributions
                                  │
   Agent (gemma4 qua Ollama) ◀── công cụ: sql_catalog (view lọc quyền) · semantic_search (lai) · contribution_report ·
        │                              graph_query/path · file_history · file_action · open_app
        └─▶ bản nháp → TỰ KIỂM CHỨNG với bằng chứng → câu trả lời + độ tin cậy + bằng chứng có vị trí
```

Nguồn sự thật = SQLite (WAL) `data/catalog.db`; vector = Qdrant server cục bộ (`data/qdrant-server`); bằng chứng gốc = chính
file trên ổ đĩa + snapshot văn bản từng phiên bản (`data/versions/*.txt.gz`). **LLM không lưu sự thật nào.**

## Bậc học (tier) — vì sao không "học" 764.000 file
Mọi file được lập DANH MỤC (tên, nơi, ai tạo, khi nào). Chỉ file bậc 1–2 được MỞ RA ĐỌC NỘI DUNG (tốn GPU):
1 = tài liệu/ảnh/audio/video của người dùng · 2 = mã nguồn của người dùng · 3 = thư viện/phần mềm cài sẵn (chỉ danh mục).
Quy tắc ở `config.LIB_MARKERS`; tiến độ (%) chỉ tính bậc 1–2.

## Đối chiếu đặc tả

| Mục đặc tả | Trạng thái | Ghi chú / bằng chứng |
|---|---|---|
| Event bus bền, idempotent, dedup, retry, dead-letter, replay, phục hồi sau restart | ✅ | `watcher.py`, `events`; test case 5 |
| Backpressure | ✅ một phần | nhận theo lô 200; việc nặng (embed) chỉ làm ngay khi ≤ 3 file đổi, còn lại hàng đợi nền |
| Catalog + provenance (creator/owner/editor/sha/version) | ✅ | `files`, `file_versions`, `file_events` |
| Phiên bản nội dung + biết nội dung nào đổi | ✅ | snapshot + diff; test case 2 |
| Document intelligence: vị trí (trang/mục/sheet+dòng/slide), bảng | ✅ | `extract.py`; OCR ảnh/PDF scan qua Gemma 4 (ảnh), PDF scan CHƯA |
| Chunk có `document→version→file` | ✅ | `chunks.file_sha256`, loc hiển thị trong câu trả lời |
| Fact store có nguồn, hiệu lực thời gian, độ tin cậy, trạng thái | ✅ | `facts.py` (candidate→usable→trusted→approved; conflicted/superseded/rejected) |
| Conflict engine, không ghi đè, có người duyệt + audit | ✅ | test case 3 |
| Knowledge graph (+ duyệt nhiều bước, lọc quyền) | ✅ SQLite | `graph.py`; chưa dùng Neo4j (chưa cần ở quy mô này) |
| Tìm lai: lọc metadata + vector + toàn văn + mở rộng đồ thị + xếp hạng lại | ✅ | `index.search`; "reranker" là RRF + thưởng khớp tên/vị trí, KHÔNG phải cross-encoder |
| Tự kiểm chứng câu trả lời, UNKNOWN/CONFLICTED | ✅ | `agent.verify`; test_units (3 kịch bản) + live |
| Phân quyền: AI không dùng nội dung file không có quyền | ✅ | lọc ở vector, BM25, SQL view, đồ thị, đóng góp, phiên bản; test case 4 |
| Sổ đóng góp có loại/trọng số/bằng chứng, phân biệt creator/editor/committer | ✅ | `contrib.py`; test case 7 |
| Git connector | ✅ | `gitsync.py` (chỉ đọc) |
| Lỗ hổng tri thức + việc nghiên cứu tự sinh | ✅ | `gaps.py`; 8 bộ dò; test case 8 |
| Workflow bền, resume sau restart | ✅ | `workflow.py`; test case 5b |
| Model registry, router, benchmark nội bộ, canary, rollback | ✅ | `registry.py`, `bench.py`; test case 6 |
| Sinh dataset huấn luyện từ tri thức đã kiểm chứng + cổng chất lượng | ✅ | `learning.py` |
| Huấn luyện LoRA | ⛔ CHƯA | `train.py` kiểm tra điều kiện và nói thật cái gì thiếu (PyTorch/PEFT/model nền HF). `train_worker.py` chưa được chạy thử |
| Shadow deployment | ⛔ | chỉ canary theo tỉ lệ lưu lượng (shadow tốn gấp đôi GPU) |
| Nhiều model song song (fast/reasoning/coding/vision) | ◐ | router theo vai trò có sẵn; 16 GB VRAM nên mỗi lúc chỉ nạp ~1 model lớn |
| vLLM / SGLang | ⛔ | không chạy native trên Windows; giữ Ollama/llama.cpp |
| PostgreSQL / MinIO / NATS | ⛔ có chủ đích | 1 máy, 1 bên ghi chính → SQLite WAL đủ; không thêm công nghệ không có lý do |
| Multi-tenant / SSO / AD | ⛔ | đăng nhập cục bộ băm PBKDF2; web chạy HTTP trong LAN |
| Sao lưu + toàn vẹn dữ liệu | ✅ | `backup.py`: catalog.db online mỗi ngày → ổ khác, giữ 7 bản; `quick_check` + đối chiếu Qdrant; Qdrant dựng lại bằng `index` |
| Chống DNS rebinding / POST chéo trang / dò mật khẩu LAN | ✅ | `tests/test_security.py`; **HTTPS chưa bật** |
| Lõi Rust (`brain_core`): norm, quét, băm, đo GPU | ✅ | parity trên dữ liệu thật, quét 8,2x nhanh; cờ `USE_RUST_*`; còn lại policy/ACL, watcher: `docs/MIGRATION_MAP.md` |
| Hợp nhất với agent thao tác máy (`local-gemini-agent`) | ✅ | `knowledge.*` qua HTTP; `local-gemini-agent/docs/unification.md` |
| Observability | ✅ | `metrics`, `/api/metrics`, tab Phân tích › Sức khỏe |
| Audit trail | ✅ | bảng `audit` (duyệt tri thức, thao tác file, model, gộp người) |
| Tự sửa code production | ⛔ có chủ đích | không làm |

## Giới hạn đã biết (đừng tin quá mức)
- Trích tri thức do gemma4 12B (4-bit): đặt tên chủ thể chưa nhất quán, đôi khi trích thừa. Mâu thuẫn chỉ được ĐÁNH DẤU.
- Tự kiểm chứng dùng chính model đó chấm câu trả lời → giảm bịa chứ không loại bỏ hoàn toàn.
- Điểm đóng góp là thước đo thô (trọng số tự đặt), không dùng để đánh giá con người.
- OCR PDF scan chưa có; HTTPS chưa bật; huấn luyện LoRA chưa chạy được.
- Embedding/Qdrant/Ollama chạy trên cùng 1 GPU. Nhịp học THÍCH ỨNG (`governor.py`): Học ngay = tối đa (không nghỉ, 3 file song song,
  gỡ giới hạn CPU); máy rảnh ≥ 3 phút = tới 60%; đang dùng máy = tối đa ~35% khi tài nguyên còn trống, nhỏ giọt ~3% khi bận.
  Tải của app khác đo lúc bot đang nghỉ. Đo thực tế: thường ~32 file/phút, Học ngay ~114 file/phút (ngoài ra phụ thuộc VRAM
  còn trống: khi app khác chiếm gần hết VRAM, đọc một ảnh mất ~40 s).
- Việc rẻ (chỉ embedding) luôn được học trước ảnh/video; tồn đọng được đọc ngay khi khởi động, không chờ quét 750.000 file.

## Kết quả đo thực tế (máy này: RTX 5060 Ti 16 GB, Ollama)
- Benchmark nội bộ `core` (31 case: bám bằng chứng, từ chối khi không biết, mâu thuẫn, trích tri thức, chọn công cụ,
  tiếng Việt, chống lệnh tiêm nhiễm trong tài liệu): **gemma4:12b = 1.00** (p50 1,7 s, ~48 tok/s).
  **qwen3:14b = 0.94** trên bản 23 case trước đó (sai 1 câu mâu thuẫn theo thời gian, 1 câu viết tắt) => bị `start_canary` TỪ CHỐI.
- Bộ benchmark đã bão hòa với gemma4 (1.00): chỉ phát hiện được suy giảm, chưa phân biệt được model TỐT HƠN. Khi có model mạnh hơn
  cần thêm case khó hơn (nhiều tài liệu, suy luận dài) từ dữ liệu thật qua suite `facts`.
- Dữ liệu thật phơi bày 3 lỗi mà dữ liệu tổng hợp không bắt được (đã sửa + có test trong `tests/test_quality.py`):
  đóng góp bị thổi phồng bởi file code tải về/ảnh AI sinh ra/giải nén hàng loạt; lỗ hổng giả khi chỉ mục còn trống;
  tab tải chậm ghi đè tab khác.
- Cải thiện hiệu năng: máy chủ nhận kết nối sau ~2 s (trước đây ~60 s do nạp Qdrant nhúng 167.000 điểm);
  chỉ ~14.800 trong 764.000 file cần đọc nội dung (bậc 1–2).
