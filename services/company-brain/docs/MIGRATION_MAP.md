# MIGRATION_MAP — Company Brain: Rust core + Python AI

Phase 0 (audit + baseline) — 2026-10-07. Chưa sửa code sản phẩm. Baseline: `docs/bench/baseline-20261007-*.json`
(lệnh: `python scripts/bench_baseline.py`, chỉ đọc dữ liệu thật; event bus đo trên DB tạm).

## 0. Điều kiện tiên quyết — ĐÃ CÓ (2026-10-07): rustc 1.99 stable-msvc, VS Build Tools 2022 (C++), maturin, PyO3 0.27 abi3-py310 (chạy được với Python 3.14). Build: `scripts\build_rust.ps1`
| Cần | Trạng thái | Ghi chú |
|---|---|---|
| Rust toolchain (rustup, stable-x86_64-pc-windows-msvc) | **chưa cài** | `cargo`, `rustc` không có |
| MSVC linker (VS Build Tools 2022 + Windows SDK, "Desktop development with C++") | **chưa có** (`link.exe` không thấy) | ~6–8 GB |
| maturin (pip) | chưa cài | build wheel PyO3 |
| PyO3 hỗ trợ Python 3.14 | cần PyO3 ≥ 0.25 (abi3 hoặc build riêng 3.14) | kiểm tra lúc Phase 1 |

## 1. Baseline đo thực tế (máy thật, 768.540 file trong catalog)
| Chỉ số | Giá trị | Ý nghĩa cho migration |
|---|---|---|
| `norm()` | 220.944 lần/s (768k tên = 3,5 s) | Không còn là nút cổ chai sau khi có cột `name_key`. Rust có lợi nhưng nhỏ. |
| `telex_variants()` | 8.400 lần/s | Gọi ít (khớp tên app/kỹ năng). Lợi nhỏ. |
| Duyệt + stat `C:\Users\TIEN THANH` | 1.243.492 file / 222.494 thư mục trong 78,9 s (15.758 file/s) | **Lợi lớn**: Rust duyệt song song (jwalk) dự kiến nhanh hơn nhiều lần — cần đo lại. |
| Kiểm tra "file có đổi không" | 10.584 SELECT/s ⇒ ~73 s cho 770k file | **Lợi lớn**: nạp sẵn (path→size,mtime) một lần vào HashMap thay cho 770k SELECT. Làm được cả bằng Python. |
| SHA-256 (file ngẫu nhiên 0,1–50 MB, đĩa nguội) | 23,4 MB/s | Giới hạn bởi **I/O đĩa**, không phải CPU (hashlib dùng C). Rust chỉ lợi nếu đọc song song trên SSD. |
| Event bus emit | 60.185 sự kiện/s | **Không phải nút cổ chai.** |
| Event bus claim+finish | 18.792 sự kiện/s, p50 0,02 ms | **Không phải nút cổ chai.** |

**Kết luận baseline:** nút cổ chai thật của hệ thống ingest là **duyệt thư mục + kiểm tra thay đổi từng file** (~2,5 phút mỗi
vòng quét lại cho riêng thư mục người dùng) và **I/O băm**. Event bus và `norm` đã đủ nhanh. Theo quy tắc "benchmark trước,
không đoán", đề xuất **đổi thứ tự** so với prompt: scanner (Phase 3) mang lại lợi ích lớn nhất; watcher/event bus chuyển vì
độ bền/độ trễ realtime (không phải throughput) và có thể để sau; `norm` vẫn làm đầu tiên vì là bài tập parity an toàn nhất
để dựng hạ tầng PyO3/maturin/test chéo.

## 2. Bản đồ module
Trạng thái: ⬜ chưa bắt đầu · 🔶 đang làm · ✅ xong (parity + test + benchmark) · ⏸ giữ Python.

| Python module | Rust target | Lý do | API tương thích (giữ nguyên chữ ký Python) | Test hiện có | Rủi ro | Trạng thái |
|---|---|---|---|---|---|---|
| `facts.norm`, `vn.plain` | `brain_core::text::norm` | hàm thuần, hạ tầng PyO3 đầu tiên | `norm(s: str) -> str` — Python gọi `brain_core.norm` nếu `USE_RUST_NORM=1`, lỗi import ⇒ bản Python | gián tiếp (units, acceptance, `name_key`) — **cần thêm test parity trên 768k tên thật** | **Trung bình**: semantics phải y hệt (xem §4) vì `name_key`/`subject_key`/`trigger` đã lưu sẵn | ✅ 2026-10-07: parity 0/985.153 chuỗi thật, name_key 0/768.546, nhanh 3,9x; cờ `USE_RUST_NORM` (mặc định 1) |
| `vn.telex_variants`, `_syllable_variants` | `brain_core::text::telex` | thuần, lặp tổ hợp | `telex_variants(text, limit=64) -> set[str]` | `test_vietnamese_input_variants` | Thấp. Khác biệt có chủ đích: Python cắt 12 biến thể theo thứ tự băm NGẪU NHIÊN của set (đổi mỗi lần chạy); Rust sắp (độ dài, chuỗi) => xác định. Từng âm tiết: parity 0/18.860 | ✅ có trong brain_core; Python chưa chuyển sang gọi (lợi ích nhỏ) |
| `scanner.scan` (walk + lọc + stat + quyết định đổi/không) | `brain_core::scan` | nút cổ chai đo được (78,9 s + 73 s) | `scan(root, progress) -> stats` giữ nguyên; Rust trả danh sách **ứng viên đã đổi** + đường dẫn biến mất; Python xử lý phần còn lại | acceptance (scan_shared…), quality | **Cao**: xem §3 — `process_file` gọi `extract` (metadata docx…) và `contrib.record_file_event` bằng Python | ✅ 2026-10-07: `brain_core.walk` + `scanner._scan_rust` (nạp catalog 1 lần, băm song song theo lô 256, ghi DB qua `process_file` cũ). Parity duyệt: 0 khác trên 665.839 file (C:\Users) + 76.655 (D:\). Parity quét đầu-cuối (files, file_events, file_versions, contributions, repos, ACL HR): `test_rust_scanner_identical_to_python`. Quét lại thật: **167,2 s → 20,3 s (8,2x)**. Cờ `USE_RUST_SCANNER` (mặc định 1), `RUST_SCAN_THREADS`=4 |
| `scanner._sha256` | `brain_core::hash_file` (sha2, streaming 1 MiB) | song song I/O | `hex sha256` y hệt hashlib | gián tiếp | ✅ `brain_core.hash_file/hash_files` (nhả GIL, song song) — parity 0/300 file thật; sẽ dùng trong Phase 3. Thấp (sha256 chuẩn; **không đổi sang blake3** vì `sha256`, `indexed_sha256`, `facts_sha256`, `file_versions.sha256`, tên snapshot đều dùng sha256) | ⬜ |
| `scanner.compute_tier`, `_acl_for`, `_dir_excluded`, `is_candidate`, `size_ok` | `brain_core::policy` | gọi cho mọi file khi quét | đọc cấu hình từ Python truyền xuống (LIB_MARKERS, EXCLUDE_DIRS, ACL_RULES, SHARED_ROOTS, MAX_FILE_MB) — **một nguồn sự thật là config.py** | units (tier), acceptance (ACL) | **Cao về ACL**: lệch một quy tắc = rò rỉ dữ liệu ⇒ test parity trên toàn bộ path thật | ⬜ |
| `scanner.mark_deleted`, phần dọn "đã xóa" | `brain_core::scan` (tập path đã thấy) | 770k path trong RAM | giữ ghi `file_events` 'deleted' như cũ | acceptance | Trung bình | ⬜ |
| `watcher._Handler` + `_flusher` (watchdog) | `brain_core::watch` (notify, ReadDirectoryChangesW) | độ trễ/độ bền realtime, bỏ watchdog | `start(roots, on_changed)`, `stop()`, ghi bảng `events` y hệt `emit` | acceptance (watcher), workflow resume | **Cao**: quy tắc "modified chỉ nhận nếu mtime < 120 s", WATCH_IGNORE, gộp FILE_CREATED+MODIFIED, moved_from | ⬜ (sau scanner) |
| `watcher.emit/_claim/_finish/recover/replay/prune` | `brain_core::event_bus` | throughput đã đủ (18–60k/s) — chuyển chỉ để Rust watcher ghi trực tiếp | **SQL giữ nguyên từng câu** (unique partial index, backoff 2^n, MAX_ATTEMPTS 5, superseded) | acceptance | Trung bình | ⬜ (cuối) |
| `watcher.process_pending` | ⏸ Python (gọi process_file → extract/contrib) | phụ thuộc Python | — | — | — | ⏸ |
| `governor` (cpu_load, gpu_load, ram_load, idle_seconds, Job Object, duty) | `brain_core::governor` | bỏ subprocess `nvidia-smi` mỗi lần đo (NVML qua `nvml-wrapper`), GetSystemTimes native | `target_duty()`, `rest()`, `duty()`, `set_force()`, `mark_background()`, `state` dict giữ nguyên | `test_governor.py` (12 test) | Trung bình: `rest()` phụ thuộc luồng Python (`threading.local`) ⇒ giữ `rest/mark_background` bằng Python, chỉ chuyển **phép đo** + Job Object | ⬜ |
| `jobs.run_once` (điều phối) | ⏸ Python, gọi `brain_core.scan` | điều phối nhẹ | — | acceptance | — | ⏸ |
| `jobs._purge_outside` | `brain_core` (lọc prefix 770k dòng) | 770k dòng/lần chạy | giữ kết quả | — | Thấp | ⬜ |
| `index.py` (embed, Qdrant, FTS5, RRF) | ⏸ Python | tìm kiếm ~0,2 s, phụ thuộc Ollama/Qdrant client | — | acceptance, quality | — | ⏸ (đánh giá lại sau) |
| `index.diff_text`, snapshot gz | có thể `brain_core::diff` | chỉ khi đo thấy chậm | — | units | Thấp | ⏸ |
| `extract.py` (docx/xlsx/pptx/pdf) | ⏸ Python | prompt: không chuyển ngay; benchmark từng loại sau | — | units | — | ⏸ |
| `agent, llm, facts(LLM), graph, memory, convo, learning, train*, bench, agent_eval, registry, workflow, media, gui, launcher, apps, computer, skills, tasks, batch, actions, server, UI` | ⏸ Python | AI / thay đổi nhanh / Windows UI | — | 113 test | — | ⏸ |

## 3. Hợp đồng quan trọng nhất: ranh giới scanner Rust ↔ Python
`process_file` hiện làm TẤT CẢ trong một hàm: stat → so size+mtime với DB → băm → so sha → **extract metadata (Python libs)**
→ ghi `files` / `file_events` / `file_versions` → **`contrib.record_file_event` (Python, logic đóng góp)**.
Đề xuất tách:
1. **Rust `scan_changes(roots, policy) -> ChangeSet`**: duyệt song song; nạp một lần `SELECT path,size,fs_modified_at,status,
   extract_error,acl,tier,sha256 FROM files` vào HashMap; với file không đổi (size+mtime) chỉ cập nhật acl/tier nếu khác (đúng
   như cũ, **không ghi gì nếu không đổi**); file nghi đổi ⇒ băm streaming song song có giới hạn (governor) ⇒ nếu sha trùng chỉ
   cập nhật mtime/acl/tier (như cũ); còn lại trả về danh sách (path, size, mtime, ctime, sha, acl, tier, owner?) cho Python.
   Đường dẫn trong DB dưới root mà không thấy ⇒ danh sách "deleted".
2. **Python** nhận ChangeSet (danh sách nhỏ — chỉ file đổi) và chạy phần còn lại của `process_file` + `mark_deleted` y như cũ
   (extract metadata, insert/update, file_events, file_versions, contrib). Behavior không đổi, ACL/provenance giữ nguyên.
3. Truyền dữ liệu: PyO3 trả `list[tuple]` (không JSON). Ghi DB phần (1) bằng `rusqlite` cùng file `catalog.db`, WAL,
   busy_timeout 90 s, giao dịch ngắn (< 0,5 s như hiện tại) để không khóa watcher/web.
4. `_ntfs_owner` (GetSecurityInfo) chỉ gọi cho file đổi — giữ Python lúc đầu.

## 4. Đặc tả `norm()` phải giữ y hệt (facts.py:73)
```
s = str(s).replace("đ","d").replace("Đ","D")
s = NFKD(s); bỏ mọi ký tự có unicodedata.combining(c) != 0; s = s.lower()   # lower SAU khi bỏ dấu
s = re.sub(r"[^a-z0-9.%/]+", " ", s).strip()                                  # ASCII thường, số, . % / ; còn lại -> 1 dấu cách
```
Chú ý khi viết Rust: NFKD (crate `unicode-normalization`, cùng phiên bản Unicode với Python 3.14 — Unicode 16), "combining"
= canonical combining class ≠ 0 (không phải `is_mark`), `lower` theo Python (`str.lower` có quy tắc đặc biệt như 'İ'→'i̇'
— sau đó bị thay bằng dấu cách nên kết quả cuối thường trùng, nhưng phải test), ký tự full-width (NFKD đổi 'Ａ'→'A'),
chữ số không ASCII ('٣' không thuộc [0-9] ⇒ cách). Test parity bắt buộc: toàn bộ 768.540 tên file + mọi `subject_key`,
`trigger`, `name_key` hiện có ⇒ 0 khác biệt.

## 5. Rollback / feature flag (config.py, đọc env)
`USE_RUST_NORM`, `USE_RUST_SCANNER`, `USE_RUST_WATCHER`, `USE_RUST_GOVERNOR` — mặc định **0** cho tới khi parity + acceptance
pass; import `brain_core` lỗi ⇒ tự dùng Python và ghi metric `error.rust_fallback`. Python cũ chỉ bị xóa sau giai đoạn ổn định
(Phase 28).

## 6. Bất biến phải kiểm ở mỗi phase (test có sẵn)
ACL mọi nhánh — `test_acceptance.py` · tier/không embed bậc 3 — `test_units.py` · không ghi DB khi file không đổi, rò rỉ ACL
dữ liệu thật — `test_quality.py` · event recovery/resume — acceptance (workflow resume) · governor — `test_governor.py` ·
pending action / batch / gui — `test_computer.py`, `test_batch.py`, `test_gui.py` · GPU arbitration — `test_llm_gate.py`.

## 7. Thứ tự đề xuất (sau khi cài toolchain)
1. Phase 1 — workspace `rust/brain_core` + `rust/brain_core_py` (PyO3, maturin, release profile `debug = 1`), import thử.
2. Phase 2 — `norm` + `telex_variants` + test parity 768k tên ⇒ cờ `USE_RUST_NORM`.
3. Phase 3/5 — `scan_changes` (walk + policy + HashMap + hash song song + ghi lô) ⇒ benchmark lại cùng lệnh ⇒ cờ.
4. Phase 6 — đo lường governor native (NVML, GetSystemTimes, Job Object); giữ `rest()` Python.
5. Phase 4 — watcher `notify` + event bus SQL y hệt (lợi về độ bền/độ trễ, không phải throughput).
Mỗi bước: `cargo test` + test parity + 113 pytest + benchmark + cờ rollback.

## 8. Rủi ro đã thấy trong audit
- `process_file` trộn I/O với logic Python (extract, contrib) ⇒ không thể "chuyển nguyên scanner sang Rust" mà giữ behavior;
  phải tách như §3.
- Một vài nơi ngoài scanner cũng ghi bảng `files` (watcher, actions/batch qua scan lại, index cập nhật `indexed_sha256`,
  facts cập nhật `facts_sha256`, trigger `name_key` cần hàm `norm_key` đăng ký trên **mọi** kết nối ghi — Rust (`rusqlite`)
  phải đăng ký hàm này bằng chính `norm` Rust, nếu không trigger lỗi khi Rust INSERT/UPDATE `files`).
- Hai tiến trình (Python server + Rust thread trong cùng tiến trình qua PyO3) cùng ghi SQLite: giữ giao dịch ngắn, busy_timeout.
- Python 3.14 mới — kiểm tra PyO3/maturin hỗ trợ trước khi cam kết.


## 9. Nhật ký phase
- 2026-10-07 Phase 1–2 ✅ (norm/plain/hash). Phase 3 ✅ (scanner: duyệt + so thay đổi + băm song song).
- Phát hiện khi chạy thật: cài Rust thêm ~60.000 file (`.rustup` tài liệu HTML/JS, `.cargo/registry`) bị xếp bậc 2 (code người
  dùng) => thêm vào `LIB_MARKERS` (bậc 3, không embed). 119 đoạn đã lỡ index sẽ được `index_all` tự xóa (dọn tier=3).
- Vận hành: đổi `config.py` phải khởi động lại bot — vòng quét nền của server đang chạy dùng cấu hình cũ.
- Một lần pytest báo 1 error không tái hiện được (6 lần chạy lại đều pass) — nghi khóa thư mục tạm của Windows khi copytree
  trong test parity quét; theo dõi tiếp.
- Tiếp theo: Phase 6 (đo lường governor native: NVML + GetSystemTimes), rồi Phase 4 (watcher `notify`).

- 2026-10-07 Phase 6 (governor) ✅ phần đo GPU: `brain_core.gpu_stat` (NVML, nvml-wrapper) thay cho chạy `nvidia-smi` mỗi giây
  khi bot nghỉ. Số đọc y hệt nvidia-smi (8/8 mẫu: % GPU + VRAM); chi phí **0,04 ms thay vì 62 ms/lần** (~1.500x), không tạo tiến
  trình. Cờ `USE_RUST_GOVERNOR` (mặc định 1), lỗi/không có GPU ⇒ lùi về nvidia-smi. Đo CPU vẫn ở Python vì đã dùng API native
  (GetSystemTimes qua ctypes); `rest()`/`mark_background()` giữ Python (phụ thuộc luồng Python).
- Phase 4 (watcher) — **HOÃN có lý do đo được**: watchdog trên Windows dùng chính ReadDirectoryChangesW như crate `notify`;
  event bus đã đạt 18.000–60.000 sự kiện/s (baseline). Không có nút cổ chai để gỡ ⇒ theo quy tắc "benchmark trước, không viết
  lại theo cảm tính", chưa chuyển. Xem lại nếu xuất hiện mất sự kiện khi đổi hàng loạt file (theo dõi `events.dead`).
