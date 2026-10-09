# Company Brain — Mô tả codebase chi tiết (phục vụ chuyển sang Rust + C#)

> **Cập nhật 2026-10-09 — đọc [README.md](../README.md) §1 trước.** Company Brain nay là *hệ con tri thức* của agent hợp nhất
> `local-gemini-agent` (gọi qua HTTP :8765). `Company Brain.bat` chỉ chạy `serve`; nút nổi/panel Python đã khai tử khỏi luồng
> dùng chính. Các phần mô tả agent/computer/gui/widget/panel dưới đây là **legacy**: vẫn chạy, có test, nhưng không phát triển thêm.
> Kế hoạch chuyển C# ở §12 đã được thay bằng cửa sổ chat Rust của local-gemini-agent; lõi Rust `brain_core` đã làm một phần (§14).
> Số dòng/bản ghi ở §0, §3, §4 là ảnh chụp 2026-10-07 trừ khi ghi khác; số liệu mới nhất ở §14.
>
> Trạng thái gốc tại 2026-10-07. Mọi con số (dòng code, số bản ghi, độ trễ) đo trực tiếp trên máy này.
> Mục tiêu tài liệu: đủ để viết lại toàn bộ hệ thống mà **giữ nguyên hành vi** và **dùng lại được dữ liệu đã học**
> (`data/catalog.db` 762 MB, Qdrant ~2 GB). Phần cuối (§12) là hướng dẫn chuyển đổi.

---

## 0. Tóm tắt nhanh

| Hạng mục | Giá trị |
|---|---|
| Ngôn ngữ hiện tại | Python 3.14 (Windows 11), HTML/JS thuần cho giao diện |
| Mã nguồn | 45 module `brain/*.py` ≈ 11.900 dòng · `web/panel.html` 862 · `web/index.html` 434 · 12 file test ≈ 1.970 dòng |
| Tiến trình khi chạy | 3 tiến trình Python (`widget`, `panel`, `serve`) + Ollama + Qdrant server |
| Dữ liệu | SQLite `catalog.db` (768.541 file, 130.733 đoạn, 773.587 sự kiện file…) + Qdrant collection `chunks` + snapshot gz |
| Model | `gemma4:12b` (chat/vision/audio/tool calling, Q4_K_M, ~7,8 GB VRAM) · `qwen3-embedding:0.6b` (1024 chiều) |
| Test | 113 unit/acceptance pass + 6 live (Ollama thật) + `agent-eval` 36 câu: **33/36 = 92 %**, 17,4 s/câu |
| Nguyên tắc cốt lõi | LLM chỉ suy luận; **sự thật nằm trong SQLite + file gốc**; mọi truy vấn lọc quyền (ACL); hành động thay đổi máy phải người dùng bấm xác nhận |

---

## 1. Kiến trúc tổng thể

```
┌─────────────────────────── Máy Windows của người dùng ────────────────────────────┐
│                                                                                    │
│  [widget.py]  Tk, nút tròn "bể nước" nổi trên desktop, hotkey Ctrl+Shift+Space      │
│      │  stdin/stdout (lệnh văn bản 1 dòng)                                          │
│      ▼                                                                             │
│  [panel.py]   pywebview = WebView2, cửa sổ không viền, nạp http://127.0.0.1:8765/panel │
│      │  JS bridge (window.pywebview.api.*)      │ HTTP JSON (fetch)                 │
│      ▼                                          ▼                                   │
│  [server.py]  ThreadingHTTPServer :8765 (0.0.0.0 — LAN dùng web/index.html)         │
│     ├─ agent.ask  ── llm.chat ──► Ollama :11434 (/api/chat, /api/embed, /api/ps)     │
│     ├─ index.search ─────────────► Qdrant server :6333 (data/qdrant-server/qdrant.exe)│
│     ├─ luồng nền: jobs (quét+học) · watcher (sự kiện file) · maintenance (1 giờ)     │
│     │             tasks (hẹn giờ, 15 s/lần) · warm-up                                │
│     └─ SQLite data/catalog.db (WAL)                                                 │
│  Công cụ ngoài: PowerShell, ffmpeg (imageio-ffmpeg), nvidia-smi, git, explorer.exe  │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Khởi động (cũ, tới 2026-10-07)**: `Company Brain.bat` → `pythonw -m brain widget`. **Hiện nay**: `Company Brain.bat` → `pythonw -m brain serve` (không giao diện); chat qua `local-gemini-agent`. Luồng cũ: widget tự bật `server` (nếu `/api/health` không trả lời) và
`panel` (tiến trình con, điều khiển qua stdin). `Kill Company Brain.bat` dừng mọi tiến trình `python -m brain …`, Qdrant
trong thư mục dự án, và `ollama stop` các model.

**Luồng của server** (`server.serve`):
- `warm_up` (luồng riêng): `registry.bootstrap()` → bật Qdrant → `watcher.start()` → `jobs.start_background()`.
- `tasks.start()`: bộ hẹn giờ.
- Mỗi request HTTP một luồng; `_llm_slots` (semaphore) giới hạn số câu hỏi chạy LLM đồng thời.
- Tiến trình server tự hạ ưu tiên CPU (BELOW_NORMAL) và tự gắn vào **Job Object** chặn cứng CPU ở 60 %.

---

## 2. Các luồng nghiệp vụ chính

### 2.1 Học (ingest) — từ file trên đĩa tới tri thức

```
quét định kỳ (jobs.run_once, 6 giờ/lần, mọi ổ cố định)      watcher (ReadDirectoryChangesW qua watchdog)
            │                                                        │ debounce 5 s, bỏ WATCH_IGNORE
            ▼                                                        ▼
   scanner.process_file ◄──────────────────── bảng events (event bus BỀN: pending/processing/done/dead)
            │  stat → (size+mtime không đổi ⇒ bỏ qua, KHÔNG ghi DB) → sha256 → files + file_versions + file_events
            │  tier: 1 tài liệu/media · 2 mã nguồn (≤200 KB) · 3 thư viện/phần mềm (chỉ lập danh mục)
            │  acl: chỉ SHARED_ROOTS mới chia sẻ ('all' hoặc nhóm theo ACL_RULES), còn lại 'private' (chỉ admin)
            ▼
   index.index_file (chỉ tier 1-2):
       extract.extract_segments  → [(vị trí, văn bản)]  (docx theo heading, xlsx 25 dòng/khối kèm header,
                                   pptx theo slide, pdf theo trang, md theo mục, csv theo khối, txt/code theo khối dòng,
                                   ảnh/audio/video → media.read_media qua gemma4)
       chunk_segments (1200 ký tự, chồng 200, tối đa 60 đoạn/file) → bảng chunks (+FTS5 qua trigger)
       embed (qwen3-embedding, lô 32) → Qdrant point id = chunk_id, payload {file_id, acl, department, project, ext, tier}
       record_version_content → snapshot gz trong data/versions + diff so với bản trước (file_versions.diff_*)
            ▼
   facts.extract_facts_pending (chỉ FACT_ROOTS, FACT_EXT) → LLM trích fact JSON → resolve_group (supersede/conflict)
   contrib.record_file_event (sổ đóng góp) · graph.rebuild (thực thể/quan hệ) · gaps.detect (lỗ hổng tri thức)
```

Thứ tự học "rẻ trước": văn bản/code → audio/video → ảnh; trong cùng nhóm theo tier rồi file mới sửa trước.
Nhịp học do `governor` quyết định (§2.4). Tiến độ % chỉ tính tier 1-2 (`progress.compute`).

### 2.2 Trả lời câu hỏi (`agent.ask`)

```
ask(question, groups, history, images, user)
 └─ llm.foreground()                       # trong lúc này việc học nền nhường GPU (§2.4)
    1. chọn công cụ: TOOLS cơ bản (6) + ask_choice; admin thêm file/app/launcher/computer/batch;
       _optional_tools(): hẹn giờ+kỹ năng và chuột/phím CHỈ khi câu hỏi có dấu hiệu cần (bớt ~1.800 token)
    2. prefetch (≈0,5 s): từ khóa (bỏ dấu + mở rộng tiếng lóng) → file theo tên (files.name_key LIKE),
       fact khớp, tìm lai nội dung (6 đoạn, score≥8) + file liên quan qua đồ thị, app đã cài (nếu là yêu cầu mở),
       quy tắc mở file, kỹ năng khớp, file vừa mở
    3. convo.compact(history): 8 tin gần giữ nguyên văn, phần cũ tóm tắt theo khối 10 tin (cache theo hash)
    4. bối cảnh hệ thống: "Bây giờ: Thứ … YYYY-MM-DD HH:MM" + KẾT QUẢ TÌM SƠ BỘ (JSON ≤10.000 ký tự)
       + kinh nghiệm (memory.recall) + việc người dùng vừa bấm Thực hiện (actions.recently_done)
    5. nhận diện ý định (_intent, regex trên câu không dấu) → gợi ý trước; câu "X liên quan tới ai" tra đồ thị sẵn
    6. vòng công cụ ≤ max_steps (admin 20): llm.chat(tools) → call_tool → kết quả JSON (≤12.000 ký tự) vào hội thoại;
       ảnh (screenshot/read_file ảnh/gui_look) chèn như tin user có images; ask_choice dừng vòng để hiện nút
       nếu model trả lời suông mà ý định cần công cụ ⇒ nhắc 1 lần (nudge)
       hết bước ⇒ gọi model tóm tắt đã làm gì/còn dở gì
    7. tự kiểm chứng (_needs_verification): JSON {verdict, unsupported_claims} so câu trả lời với BẰNG CHỨNG;
       unsupported ⇒ viết lại 1 lần ⇒ vẫn sai thì trả "chưa đủ bằng chứng" + liệt kê bằng chứng liên quan
    8. trả {answer, sources, steps, actions(pending), choices, confidence(high|medium|unknown|conflicted|n/a),
            verification, model, evidence[≤8]}
 server sau đó: ghi qa_log, metric ask_latency_s, memory.learn_async (tự học sửa sai/dặn dò, chạy nền)
```

**Bằng chứng (`Evidence`)** gom từ mọi kết quả công cụ: đoạn văn (hits), hàng SQL, fact (kèm status), cạnh đồ thị,
xếp hạng đóng góp, phiên bản, nội dung file/web, dòng grep, output lệnh, tình trạng máy, file tìm thấy, và **lời người
dùng trong hội thoại**. Bộ kiểm chứng chỉ được dùng các bằng chứng này.

### 2.3 Hành động & xác nhận

- Công cụ chỉ-đọc chạy ngay (đọc/tìm file, lệnh PowerShell chỉ-đọc, web, chụp màn hình, âm lượng, mở app/file/URL).
- Công cụ thay đổi máy tạo **pending action** (`actions.add_pending`): `{id, action, summary, detail(xem trước), plan}`,
  hết hạn 30 phút (việc nền: 24 giờ). Panel hiện nút ✔ Thực hiện / Hủy → `POST /api/action` → `actions.execute`.
- Loại pending: `copy|move|rename|delete(Thùng rác)|mkdir` (file_action), `write_file|edit_file` (sao lưu bản cũ vào
  data/backups, từ chối nếu file đổi sau khi đề xuất), `run_command` (kể cả chạy nền), `batch` / `batch_undo`,
  `schedule_command`, `gui_grant` (cấp quyền chuột/phím 10 phút, rồi làm luôn thao tác đang chờ).
- Sau khi thực hiện: audit log + `_done_log[user]` ⇒ lượt chat sau được báo "việc X đã xong" (không làm lại).

### 2.4 Điều tiết tài nguyên (`governor` + cổng GPU `llm`)

- **duty** (tỉ lệ thời gian được bận) tính lại liên tục:
  `duty = clamp((headroom − reserve)/100, TRICKLE 0.03, cap)`, `headroom = 100 − tải GPU/CPU của app KHÁC`
  (đo lúc bot đang nghỉ: nvidia-smi + GetSystemTimes). Máy rảnh ≥3 phút: cap 0,6, reserve 5; đang dùng: cap 0,35, reserve 25.
  "Học ngay" (force): duty 1, 3 file song song, gỡ cap CPU, RAM ngưỡng dừng 97 % (thường 90 %).
- `governor.rest()` gọi giữa các file: ngủ để tỉ lệ bận ≈ duty, dậy sớm nếu điều kiện đổi (lát 0,5 s).
- **Cổng GPU** (`llm.foreground`/`_background_turn`): role `vision`/`extract` là việc nền ⇒ chờ khi người dùng đang hỏi,
  tối đa 1 yêu cầu nền trong Ollama. **Trần token sinh** `MAX_PREDICT` chat/extract 2048, vision 1536 (+repeat_penalty 1.15).
  Tất cả lời gọi gemma4 dùng cùng `num_ctx=16384` (đổi num_ctx = Ollama nạp lại ~10 s).

---

## 3. Danh mục module

Cột "Đích" là đề xuất khi chuyển: **R** = Rust core/server, **C#** = ứng dụng Windows (UI + tích hợp OS), **R/C#** = tùy.

| Module | Dòng | Vai trò | Hàm/lớp chính | Đích |
|---|---:|---|---|---|
| `config.py` | 138 | Mọi hằng số cấu hình (§8) | — | R (TOML) |
| `catalog.py` | 254 | Schema SQLite + migration + `connect()` (WAL, busy 90 s, UDF `norm_key`, backfill `name_key`) | `connect` | R |
| `scanner.py` | 263 | Quét ổ đĩa → files/versions/events; tier, ACL, NTFS owner, metadata Office | `process_file`, `scan`, `compute_tier`, `mark_deleted`, `fixed_drives` | R |
| `extract.py` | 225 | Trích văn bản theo vị trí từ docx/xlsx/pptx/pdf/md/csv/txt/code; chia đoạn | `extract_segments`, `chunk_segments` | R |
| `media.py` | 143 | Ảnh/audio/video → văn bản qua gemma4 (ffmpeg cắt khung/đoạn 30 s) | `read_image`, `transcribe`, `read_video`, `prepare_attachment` | R |
| `index.py` | 387 | Chunks+FTS5, embedding, Qdrant, snapshot/diff phiên bản, **tìm lai** | `index_file`, `index_all`, `embed`, `search`, `ensure_qdrant_server` | R |
| `watcher.py` | 282 | Event bus bền + watcher realtime + worker | `emit`, `process_pending`, `recover`, `replay`, `start` | R |
| `jobs.py` | 139 | Vòng quét+học nền, maintenance mỗi giờ, "Học ngay" | `run_once`, `start_background`, `trigger` | R |
| `governor.py` | 231 | Điều tiết thích ứng, Job Object CPU cap, đo idle/CPU/GPU/RAM | `target_duty`, `rest`, `set_force`, `set_cpu_cap` | R (+Win API) |
| `llm.py` | 117 | Cổng Ollama: router model, retry, fallback production, metric, cổng GPU, trần token | `chat`, `foreground` | R |
| `agent.py` | 678 | Bộ não hỏi-đáp (§2.2): TOOLS, SYSTEM prompt, prefetch, intent, vòng công cụ, kiểm chứng | `ask`, `call_tool`, `prefetch`, `verify`, `Evidence` | R |
| `sqlbox.py` | 98 | Hộp cát SQL cho AI: authorizer chỉ cho đọc view `visible_*` đã lọc quyền | `run_sql` | R |
| `server_acl.py` | 15 | `can_read(acl, groups)`, `acl_sql()` | — | R |
| `server.py` | 435 | HTTP API (§5), phục vụ panel/web, phiên đăng nhập | `Handler`, `serve` | R (axum) |
| `api_ext.py` | 184 | API mở rộng: đóng góp, đồ thị, lỗ hổng, model, số liệu, sự kiện, workflow, audit | `get`, `post` | R |
| `auth.py` | 58 | Tài khoản LAN (data/users.json, PBKDF2), phiên cookie 12 giờ; truy cập từ 127.0.0.1 = admin | `login`, `session` | R |
| `facts.py` | 331 | Fact Store: trích bằng LLM, chuẩn hóa, độ tin cậy, supersede/conflict, duyệt | `llm_extract`, `extract_facts_pending`, `resolve_group`, `review`, `norm` | R |
| `graph.py` | 196 | Đồ thị thực thể/quan hệ trong SQLite, BFS, đường đi ngắn nhất | `rebuild`, `neighbors`, `path`, `related_files` | R |
| `contrib.py` | 138 | Sổ đóng góp có trọng số + bằng chứng | `record_file_event`, `record_commit`, `report`, `backfill` | R |
| `people.py` | 75 | Hợp nhất danh tính (NTFS, Office author, Git, email) | `resolve`, `merge`, `name_key` | R |
| `gitsync.py` | 85 | Đọc `git log` các repo tìm thấy → commit + đóng góp | `sync_all` | R |
| `gaps.py` | 165 | 8 bộ phát hiện lỗ hổng tri thức + việc nghiên cứu | `detect`, `run` | R |
| `workflow.py` / `workflows_def.py` | 90 / 85 | Workflow bền (lưu trạng thái sau mỗi bước, resume) — MAINTENANCE, DEPLOY_MODEL, BUILD_TRAINING_DATASET | `start`, `run`, `resume_all` | R |
| `registry.py` | 236 | Registry model + router + canary/promote/rollback | `pick`, `production`, `promote`, `rollback` | R |
| `bench.py` | 246 | Benchmark nội bộ (31 case, chấm tự động) | `run_suite` | R |
| `agent_eval.py` | 171 | Chấm điểm đầu-cuối 36 câu tiếng Việt, dry-run hành động | `run` | R |
| `learning.py` / `train.py` / `train_worker.py` | 160/68/36 | Sinh dataset từ tri thức đã kiểm chứng; LoRA (chưa chạy được: thiếu torch/peft) | `build_dataset`, `preflight` | R (giữ Python riêng nếu cần train) |
| `memory.py` | 196 | Kinh nghiệm: lesson/example/alias/preference, embedding, tự học từ hội thoại/👍👎 | `recall`, `add`, `learn_from_turn` | R |
| `convo.py` | 77 | Tóm tắt hội thoại dài theo khối | `compact` | R |
| `obs.py` | 110 | Metric (hàng đợi bất đồng bộ → bảng metrics), audit, summary p95 | `metric`, `audit`, `summary` | R |
| `progress.py` | 56 | % đã học theo loại tệp | `compute` | R |
| `doctor.py` | 100 | Tự kiểm tra: Ollama trùng, model trên GPU, VRAM, Qdrant, độ trễ, ổ đĩa | `run` | R |
| `vn.py` | 83 | Bỏ dấu, biến thể Telex gõ nhầm, tiếng lóng (gg, ppt, utb…), so khớp mờ | `plain`, `telex_variants`, `expand_slang`, `best_similarity` | R |
| `actions.py` | 240 | Thao tác file + cơ chế pending/execute dùng chung, vùng bảo vệ, Thùng rác | `propose`, `add_pending`, `execute` | R (+C# shell) |
| `batch.py` | 236 | Thao tác file hàng loạt theo loại/ngày/mẫu tên + hoàn tác | `propose`, `run`, `propose_undo` | R |
| `apps.py` | 221 | Danh sách app (Get-StartApps + đích .lnk), tìm theo tên gọi tiếng Việt, mở app | `find`, `open_app`, `refresh` | **C#** |
| `launcher.py` | 533 | Mở URL/YouTube theo trình duyệt, mở file bằng app chỉ định (cả app Store), quy tắc loại-file→app, đọc registry | `open_url`, `open_file`, `set_rules`, `default_handler` | **C#** |
| `computer.py` | 531 | Đọc/tìm/grep/ghi/sửa file, PowerShell (phân loại chỉ-đọc), web search/fetch, chụp màn hình, phím media, tình trạng máy | 11 công cụ | R + C# |
| `gui.py` | 559 | Điều khiển chuột/phím: UI Automation đánh số phần tử, PrintWindow, SendInput, clipboard, quyền 10 phút | `look`, `call`, `grant` | **C#** |
| `tasks.py` | 385 | Hẹn giờ/lặp, việc nền, thông báo (DB + toast Windows) | `create`, `tick`, `run_task`, `notify` | R (+C# toast) |
| `skills.py` | 239 | Kỹ năng do người dùng dạy (bước cụ thể có tham số, hoặc hướng dẫn) | `save`, `run`, `matching` | R |
| `widget.py` / `widget_art.py` | 497/133 | Nút nổi Tk + vẽ "bể nước" (Pillow), ghi âm (sounddevice), đọc to (SAPI), hotkey | `App`, `BubbleArt`, `Recorder`, `Speaker` | **C#** (WPF/WinUI) |
| `panel.py` | 189 | Cửa sổ WebView2 cho panel + JS bridge | `Bridge`, `main` | **C#** (WebView2) |
| `web/panel.html` | 862 | Giao diện chính: tab Chat · Học · Việc · Nhớ · Tri thức · Phân tích · ⚙ | — | giữ nguyên (HTML/JS) |
| `web/index.html` | 434 | Web LAN cho người khác | — | giữ nguyên |
| `__main__.py` | 240 | CLI 30 lệnh (§9) | `main` | R (clap) |

---

## 4. Mô hình dữ liệu

### 4.1 SQLite `data/catalog.db` (WAL, `synchronous=NORMAL`, busy_timeout 90 s)

| Bảng | Bản ghi | Cột (ý nghĩa) |
|---|---:|---|
| `files` | 768.541 | file_id PK, **path UNIQUE**, name, ext, size, sha256, fs_created_at, fs_modified_at, fs_owner (NTFS), doc_author, doc_last_editor, doc_created_at, title, department, project, **acl** ('all' / 'hr,board' / 'private'), status (active/deleted), indexed_sha256 (đã index bản nào), first_seen_at, last_seen_at, extract_error, facts_sha256, **tier** (1/2/3), version_no, **name_key** (tên+tiêu đề không dấu, trigger) |
| `file_events` | 773.587 | sổ sự kiện file: discovered/modified/moved/deleted/restored/ai_* (actor, at, detail) |
| `file_versions` | 16.024 | version_no, sha256, size, editor, observed_at, snapshot (đường dẫn gz), diff_added/removed/summary |
| `chunks` | 130.733 | chunk_id PK (= id điểm Qdrant), file_id, idx, **loc** (vị trí: "trang 3", "Sheet1 dòng 26-50"…), text, file_sha256 |
| `chunks_fts` | FTS5 | `text`, tokenizer `unicode61 remove_diacritics 2`, đồng bộ bằng trigger `chunks_ai`/`chunks_ad` |
| `events` | 15.858 | event bus bền: event_type, object_id, processing_status (pending/processing/done/dead), attempts, error, claimed_at… Index duy nhất một-pending-mỗi-đối-tượng (`ux_events_pending`) |
| `ingest_queue` | 8 | hàng đợi debounce của watcher |
| `facts` | 91 | subject/predicate/value/unit (+ *_key chuẩn hóa), evidence, source_file_id, source_sha256, author, valid_from/to, base_confidence, confidence, **status** (candidate/usable/trusted/approved/conflicted/superseded/rejected), note, reviewed_by/at |
| `entities` / `relations` | 15.054 / 28.864 | đồ thị: type, name, key (tên chuẩn hóa) / src, rel, dst, source_file_id, evidence, valid_from/to, confidence |
| `people` / `person_aliases` | 1.070 / 1.070 | danh tính + bí danh |
| `contributions` | 9.187 | person_id, kind, weight, file_id, project, department, at, evidence, ref (khóa chống trùng) |
| `repos` / `git_commits` | 65 / 7.368 | repo Git tìm thấy + commit |
| `knowledge_gaps` / `research_tasks` | 152 / 45 | lỗ hổng tri thức (fingerprint chống trùng) + việc nghiên cứu |
| `models` / `bench_runs` / `deployments` | 2 / 3 / 1 | registry model, kết quả benchmark, lịch sử triển khai |
| `workflows` | 43 | type, status, step, state (JSON), attempts, error |
| `qa_log` | 130 | lịch sử hỏi-đáp + rating/comment (👍👎) |
| `memories` | 14 | kind (lesson/example/alias/preference), trigger, content, vector (JSON 1024 float), owner, scope, uses, active |
| `convo_summaries` | 3 | key = sha256 khối tin nhắn → summary |
| `open_rules` | 22 | ext → app (quy tắc "loại file mở bằng app") |
| `tasks` / `notifications` | 3 / 3 | hẹn giờ (kind, payload, repeat, interval_min, at_time, weekdays, next_run…) / thông báo (data JSON) |
| `skills` | 1 | name, name_key, description, triggers, params JSON, steps JSON, instructions, uses |
| `metrics` / `audit` / `settings` | 44.943 / 25 / 1 | số liệu (ts, name, value, tags) / kiểm toán / cài đặt |

Index & trigger: `ix_files_sha`, `ix_files_tier(tier,status)`, `ix_events_file`, `ix_events_status`, `ux_events_pending`,
`ix_facts_key`, `ix_facts_file`, `ix_chunks_file`, `ix_contrib_person`, `ix_contrib_project`, `ix_rel_src`, `ix_rel_dst`,
`ix_metrics`, trigger `chunks_ai`/`chunks_ad` (FTS), `tr_files_name_key_ins`/`_upd` (gọi UDF **`norm_key`** — kết nối ghi
nào cũng phải đăng ký hàm này, kể cả bản Rust).

### 4.2 Qdrant (server cục bộ :6333, collection `chunks`)
- Vector 1024 chiều (qwen3-embedding:0.6b), cosine. Point id = `chunks.chunk_id`.
- Payload: `file_id`, `acl` (mảng/chuỗi nhóm — lọc bằng `MatchAny(groups ∪ {all})` khi không phải admin), `department`,
  `project`, `ext`, `tier`. Truy vấn có tiền tố instruction:
  `"Instruct: Given a question, retrieve company documents that answer it\nQuery: …"`.
- `data/qdrant` (1,9 GB) là kho chế độ local cũ — **không còn dùng** (đã chuyển sang server).

### 4.3 File trên đĩa (`data/`)
`catalog.db` (+ -wal/-shm) · `qdrant-server/` (qdrant.exe + storage) · `versions/*.txt.gz` (snapshot nội dung theo sha)
· `media_cache/` · `backups/` (bản cũ trước khi ghi/sửa) · `batches/*.json` (sổ hoàn tác chuyển hàng loạt)
· `agent_eval/*.json` · `datasets/` · `apps.json` (cache danh sách app) · `users.json` · `widget.json` (vị trí nút, tùy chọn)
· `activate_packaged.ps1` (sinh tự động) · `webview/` (profile WebView2) · `*.log`.

---

## 5. HTTP API (server :8765)

Xác thực: cookie `cb_session` (đăng nhập LAN) **hoặc** truy cập từ 127.0.0.1 ⇒ phiên admin tự động.
Mọi endpoint dữ liệu lọc theo `groups` của phiên. Cột "Q" = chỉ admin.

| Endpoint | Q | Mục đích |
|---|:-:|---|
| `GET /`, `/panel` | | trang web LAN / panel (panel chỉ phục vụ localhost) |
| `GET /api/health` · `/api/me` · `/api/status` | | sống không · phiên · trạng thái job/catalog/watcher |
| `POST /api/login` · `/api/logout` | | phiên LAN |
| `POST /api/ask` | | `{question, history[≤120], attachments[≤4, base64, ≤15 MB]}` → kết quả `agent.ask` + `qa_id` |
| `POST /api/action` | ✔ | `{id, confirm}` → thực hiện/hủy pending → `{ok, message, output?}` |
| `POST /api/feedback` | | `{qa_id, rating ±1, comment}` → học kinh nghiệm |
| `POST /api/transcribe` | | ghi âm (wav) → chữ (gemma4 audio) |
| `GET /api/search` · `/api/file/<id>` · `/api/versions` | | tìm lai · tải file (lọc quyền) · phiên bản |
| `GET /api/progress` | | % học, loại tệp, nhịp học (governor), **unread**, **gui** (quyền chuột/phím) |
| `POST /api/reindex` · `/api/learn/normal` | ✔ | Học ngay (force) · dừng force |
| `GET /api/facts` · `POST /api/facts/review` | ✔(review) | tri thức · duyệt/từ chối fact |
| `GET /api/memories` · `POST /api/memories/toggle` | | kinh nghiệm đã học |
| `GET /api/open_rules` · `POST /api/open_rules/remove` | ✔ | quy tắc mở file |
| `GET /api/tasks` · `POST /api/tasks/set` · `/api/notifications/read` · `/api/skills/delete` | ✔ | tab Việc |
| `POST /api/gui/stop` | | thu hồi quyền chuột/phím |
| `GET /api/doctor` | ✔ | tự kiểm tra sức khỏe |
| `GET /api/contributions` · `/api/people` · `POST /api/people/merge` | | sổ đóng góp, danh tính |
| `GET /api/graph` · `/api/gaps` · `POST /api/gaps/resolve` | | đồ thị, lỗ hổng tri thức |
| `GET /api/models` · `POST /api/models/{register,benchmark,canary,deploy,promote,rollback}` | ✔ | vòng đời model |
| `GET /api/metrics` · `/api/events` · `POST /api/events/replay` · `/api/workflows` · `/api/audit` · `/api/maintenance(/run)` | ✔ | vận hành |
| `GET /api/datasets` · `/api/dataset` · `POST /api/dataset/build` · `/api/train` | ✔ | dataset/huấn luyện |

---

## 6. Tích hợp LLM

### 6.1 Ollama
- `POST /api/chat` `{model, messages, stream:false, think:false, keep_alive, tools?, format?(JSON schema),
  options:{temperature, num_ctx:16384, num_predict, draft_num_predict:0, repeat_penalty?}}`. `draft_num_predict:0` là
  bắt buộc với gemma4 trên bản Ollama này.
- `POST /api/embed` `{model, input[≤32], keep_alive:"30s", truncate:true, options:{num_ctx:2048}}`.
- `GET /api/ps` (doctor), `/api/tags` (registry). Ảnh/audio gửi base64 trong `messages[].images`.
- keep_alive: chat 5m, media 2m, embed 30s. Retry 1 lần với 5xx/lỗi mạng; model canary lỗi ⇒ lùi về production.
- Vai trò (role): `chat`, `vision`, `extract` (router `registry.pick(role)`; hiện mọi role → gemma4).

### 6.2 Prompt quan trọng (giữ nguyên nội dung khi chuyển — đã được chỉnh qua nhiều vòng thử thật)
`agent.SYSTEM` (quy tắc trả lời, chọn công cụ, thực thi, an toàn) · `agent.VERIFY_PROMPT` + `VERIFY_SCHEMA` ·
`facts.PROMPT` + `SCHEMA` · `memory.DETECT_PROMPT` + `DETECT_SCHEMA` · `convo.PROMPT` · `media` (tả ảnh/chép lời) ·
lời nhắc ý định trong `agent._INTENTS` / `_KNOWLEDGE_INTENTS`.

### 6.3 Công cụ (function calling) — 37 công cụ

| Nhóm | Công cụ | Ai dùng | Ghi chú |
|---|---|---|---|
| Tri thức | `sql_catalog`, `semantic_search`, `contribution_report`, `graph_query`, `graph_path`, `file_history` | mọi người | qua view đã lọc quyền |
| Hỏi lại | `ask_choice` | mọi người | dừng vòng, hiện nút |
| File | `file_action` | admin | open/open_folder chạy ngay; còn lại pending |
| Hàng loạt | `batch_files`, `undo_batch` | admin | pending, có xem trước + hoàn tác |
| App | `open_app`, `list_apps` | admin | |
| Mở theo ý định | `open_url`, `open_file_with`, `set_open_rule`, `manage_open_rules`, `file_type_info` | admin | |
| Máy tính | `read_file`, `find_files`, `search_in_files`, `write_file`, `edit_file`, `run_command`, `web_search`, `fetch_url`, `screenshot`, `media_control`, `system_status` | admin | ghi/sửa/lệnh thay đổi ⇒ pending |
| Hẹn giờ | `schedule_task`, `manage_tasks` | admin (tùy chọn) | command ⇒ pending 1 lần |
| Kỹ năng | `save_skill`, `run_skill`, `manage_skills` | admin (tùy chọn) | |
| Chuột/phím | `gui_look`, `gui_click`, `gui_type`, `gui_key`, `gui_scroll`, `gui_window` | admin (tùy chọn) | cần quyền 10 phút |

Định dạng: JSON Schema kiểu OpenAI `{"type":"function","function":{name, description, parameters}}`; mô tả viết tiếng
Việt và chứa nhiều ví dụ cách người dùng nói — **đây là một phần "trí tuệ" của hệ thống, cần chép nguyên**.

---

## 7. Mô hình bảo mật

- **ACL file**: `files.acl` — 'all', danh sách nhóm ('hr,board'), hoặc 'private' (chỉ admin). Chỉ thư mục trong
  `SHARED_ROOTS` được chia sẻ; thư mục con trùng tên trong `ACL_RULES` thu hẹp nhóm.
- **Lọc ở mọi nhánh**: Qdrant (payload filter), FTS (SQL), view `visible_*` (sqlbox), đồ thị, đóng góp, phiên bản, tải file.
- **sqlbox**: SQLite authorizer chặn đọc bảng gốc; AI chỉ được SELECT trên view tạm `visible_files/events/versions/
  contributions/relations/facts/gaps`.
- **Admin** = người dùng ngồi tại máy chủ (127.0.0.1). Người trong LAN không có công cụ thao tác máy.
- **Vùng bảo vệ**: không sửa/xóa/ghi trong `C:\Windows`, `Program Files(x86)`, `ProgramData`, `Recovery`,
  `$Recycle.Bin`, `System Volume Information`. Xóa = vào Thùng rác.
- **Lệnh PowerShell**: chỉ chạy ngay nếu `is_read_only` (danh sách lệnh/cmdlet chỉ-đọc, cấm `; & > $( :: .Method(` và
  các động từ Set/Remove/New/Stop/Invoke…). Còn lại pending.
- **Chuột/phím**: quyền có thời hạn (10 phút, ≤200 thao tác), dừng khẩn cấp khi chuột ở góc trên-trái, không gõ vào ô
  mật khẩu (UIA IsPassword), audit từng thao tác.
- **Prompt injection**: SYSTEM dặn coi nội dung web/file/output là dữ liệu, không phải mệnh lệnh (chưa có cơ chế kỹ thuật
  cách ly — điểm cần làm tốt hơn khi viết lại).
- Mật khẩu LAN: PBKDF2 trong `users.json`.

---

## 8. Cấu hình (`config.py`) — giá trị hiện tại

| Nhóm | Hằng số |
|---|---|
| Đường dẫn | `ROOT`, `DATA_DIR` (env `BRAIN_DATA_DIR`), `CATALOG_DB`, `QDRANT_DIR`, `QDRANT_EXE`, `USERS_FILE` |
| Phạm vi quét | `SCAN_ALL_DRIVES=True`, `SCAN_ROOTS` (Desktop/Documents/Downloads khi không quét hết), `SHARED_ROOTS=[sample_data]`, `FACT_ROOTS=[sample_data]`, `EXCLUDE_DIRS`, `WATCH_IGNORE`, `LIB_MARKERS` (site-packages, node_modules, .venv, anaconda3, .nuget…), `SCAN_EVERY_HOURS=6` |
| Loại file | `SUPPORTED_EXT` (văn phòng, ảnh, audio, video, code), `CODE_EXT`, `FACT_EXT`, `MAX_FILE_MB=50`, `MAX_CODE_KB=200`, `MAX_MEDIA_MB=500`, `MIN_IMAGE_KB=30`, `MAX_TEXT_CHARS=300000` |
| Chia đoạn | `CHUNK_CHARS=1200`, `CHUNK_OVERLAP=200`, `MAX_CHUNKS_PER_FILE=60` |
| Media | `AUDIO_CHUNK_SEC=30`, `AUDIO_MAX_SEC=1200`, `VIDEO_FRAMES=6`, `MAX_ATTACH_MB=15` |
| Model | `CHAT_MODEL=MEDIA_MODEL=gemma4:12b`, `EMBED_MODEL=qwen3-embedding:0.6b`, `LLM_OPTIONS={draft_num_predict:0}`, keep_alive chat 5m / media 2m / embed 30s, `OLLAMA_URL=http://localhost:11434` |
| Vector | `QDRANT_MODE=server`, `QDRANT_URL=http://127.0.0.1:6333`, `COLLECTION=chunks` |
| Điều tiết | `RESOURCE_LIMIT=0.6`, `ACTIVE_MAX_DUTY=0.35`, `ACTIVE_RESERVE=25`, `IDLE_RESERVE=5`, `TRICKLE_DUTY=0.03`, `IDLE_MINUTES=3`, `MAX_REST_S=120`, `RAM_PAUSE_AT=90`, `FORCE_RAM_PAUSE_AT=97`, `FORCE_WORKERS=3`, `PAUSE_BETWEEN_FILES=0.3` |
| Watcher | `DEBOUNCE_SEC=5`, `REALTIME_INDEX_MAX=3` |
| Fact | `FACT_MAX_CHARS=12000`, `SOURCE_WEIGHTS` (spec/master 0,95 · formula 0,9 · lab/report…), `DEFAULT_SOURCE_WEIGHT=0.5`, `USABLE_AT=0.5`, `TRUSTED_AT=0.8` |
| Quyền | `ADMIN_GROUP='admin'`, `ACL_RULES={HR:[hr,board], Finance:[finance,board]}`, `SESSION_HOURS=12` |
| Web | `WEB_HOST=0.0.0.0`, `WEB_PORT=8765` |
| Kiểm chứng | `VERIFY_ANSWERS=True`, `VERIFY_MAX_EVIDENCE_CHARS=7000` |

---

## 9. CLI (`python -m brain <lệnh>`)

`ask` · `search` · `scan` · `index` · `stats` · `serve` · `widget` · `panel` · `adduser` · `users` · `facts` · `dupes` ·
`contrib` · `graph` · `gaps` · `history` · `maintenance` · `events` · `metrics` · `models` · `bench` · `doctor` ·
`agent-eval` · `deploy` · `promote` · `rollback` · `canary-eval` · `dataset` · `export-dataset` · `train`.
Mật khẩu LAN: `pbkdf2_hmac("sha256", password, salt, _ITER)` (xem `auth.py`). Qdrant: khoảng cách **cosine**.

---

## 10. Thuật toán & quy tắc chi tiết (phải giữ đúng khi viết lại)

**Chuẩn hóa tiếng Việt (`vn.plain` / `facts.norm`)**: NFKD, bỏ dấu kết hợp, `đ→d`, chữ thường, gộp khoảng trắng.
Dùng ở `name_key`, từ khóa prefetch, khớp app, fact key. FTS5 dùng `remove_diacritics 2` tương đương.
`telex_variants`: sinh các chuỗi phím Telex có thể đã gõ ra một từ (để "ửod" khớp "word"). `SLANG`: gg→google,
yt/utb/ytb→youtube, ppt→powerpoint, xl→excel, ko/k→khong…

**Tìm lai (`index.search`)**: nhánh vector (30 điểm, lọc ACL) + nhánh BM25 FTS5 (30 đoạn, chỉ tier 1-2, lọc ACL) →
**RRF** `Σ 1/(60 + rank)` → nhân `(1 + 0,15 × số từ khóa ≥3 ký tự có trong tên file/vị trí + 0,2 nếu cả hai nhánh cùng chọn)`
→ tối đa 2 đoạn mỗi file → `score = s × 1000`. Prefetch chỉ lấy đoạn có score ≥ 8.

**Tier**: chứa `LIB_MARKERS` ⇒ 3; đuôi code ⇒ 2 (nếu ≤200 KB, không thì 3); còn lại 1.

**Fact**: độ tin cậy = trọng số nguồn theo tên file (`SOURCE_WEIGHTS`) × chất lượng trích; status
`trusted ≥0,8`, `usable ≥0,5`, còn lại `candidate`; cùng (subject_key, predicate_key): bản đã duyệt thắng ⇒ bản khác giá trị
thành `conflicted`; bản mới hơn cùng nguồn ⇒ bản cũ `superseded` (valid_to = valid_from của bản mới); các nguồn hiện hành
khác giá trị ⇒ tất cả `conflicted` (không tự chọn).

**Đóng góp**: `created_doc 3,0 · modified_doc 1,0 · created_code 2,0 · modified_code 0,7 · git_commit 2,0 (+ theo quy mô,
có trần)`; chỉ biết chủ NTFS (không có tác giả khai) ⇒ ×0,5; >25 file cùng người/dự án/phút ⇒ coi là sao chép hàng loạt,
bỏ; tài khoản hệ thống bị loại; chỉ tài liệu văn phòng (không tính ảnh/video).

**Event bus**: `emit` chèn pending (unique một-pending-mỗi-đối-tượng ⇒ gộp sự kiện dồn dập); worker `claim` →
processing → done; lỗi ⇒ tăng attempts, quá ngưỡng ⇒ `dead`; `recover` đưa processing treo quá lâu về pending;
`replay` chạy lại dead.

**Ý định (`agent._intent`)**: regex trên câu không dấu; thứ tự ưu tiên: hẹn giờ · thao tác giao diện · kỹ năng ·
nhiều file (gom/dọn/chuyển tất cả) → yêu cầu thực thi (mở/bật/phát/nghe/đóng) → tri thức (đóng góp/phiên bản/liên quan)
→ câu hỏi về máy (ip/ram/gpu…) → thông tin mới (hôm nay/hiện nay/tin tức…).

**Hẹn giờ**: `parse_when` nhận `HH:MM`, `17h`, `9h30`, `YYYY-MM-DD HH:MM`, `dd/mm[/yyyy] HH:MM`, `+30m/+2h/+1d`;
lặp `once|minutes|hourly|daily|weekdays|weekly`; `tick` 15 s dời lịch TRƯỚC khi chạy (không chạy trùng); máy chủ tắt lúc
tới giờ ⇒ chạy bù và ghi "trễ".

**Kỹ năng**: lưu các bước của lượt trước (`_last_calls[user]`, bỏ công cụ quản lý); giá trị mẫu → `{tham_so}`; bước có
`file_id/element/x/y` ⇒ không chạy nguyên xi, trả hướng dẫn + bước mẫu để model tự làm.

---

## 11. Tích hợp đặc thù Windows (điểm nóng khi chuyển)

| Chức năng | Hiện dùng | Nơi |
|---|---|---|
| Danh sách app | PowerShell `Get-StartApps` + đọc đích `.lnk` (WScript.Shell) | apps |
| Mở app Store / mở file bằng app Store | `explorer shell:AppsFolder\<AUMID>`, `IApplicationActivationManager.ActivateForFile` (C# nhúng trong PowerShell) | apps, launcher |
| App mặc định theo đuôi file | Registry `HKCU\…\FileExts\<ext>\UserChoice`, `HKCR\<ProgId>\shell\open\command`, `Application\AppUserModelID`, `App Paths` | launcher |
| Xóa vào Thùng rác | `SHFileOperation(FO_DELETE, FOF_ALLOWUNDO)` | actions |
| Chủ sở hữu NTFS | pywin32 security API | scanner |
| Theo dõi file | watchdog (ReadDirectoryChangesW) | watcher |
| Chặn CPU | Job Object `JobObjectCpuRateControlInformation` HARD_CAP | governor |
| Tải CPU/GPU/RAM/idle | `GetSystemTimes`, `GlobalMemoryStatusEx`, `GetLastInputInfo`, `nvidia-smi` | governor |
| Nút nổi | Tk `overrideredirect` + `-transparentcolor`, vẽ bằng Pillow | widget, widget_art |
| Panel | pywebview (WebView2), DWM bo góc, JS bridge | panel |
| Hotkey toàn cục | `RegisterHotKey(Ctrl+Shift+Space)` | widget |
| Ghi âm / đọc to | sounddevice 16 kHz mono / SAPI `SpVoice` giọng OneCore tiếng Việt | widget |
| Chuột/phím | UI Automation (comtypes: `FindAllBuildCache`, `CurrentIsPassword`), `SendInput` (KEYEVENTF_UNICODE), `SetCursorPos`, `PrintWindow(PW_RENDERFULLCONTENT)`, `DwmGetWindowAttribute(DWMWA_CLOAKED)`, clipboard (win32clipboard), `SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)` | gui |
| Chụp màn hình | Pillow `ImageGrab` (all_screens) | computer |
| Phím media | `keybd_event` VK_VOLUME_*/VK_MEDIA_* | computer |
| Thông báo | WinRT `ToastNotificationManager` qua PowerShell (AUMID PowerShell) | tasks |
| Lệnh hệ thống | PowerShell `-NoProfile -NonInteractive`, UTF-8 output | computer, apps |

---

## 12. Hướng dẫn chuyển sang Rust + C#

### 12.1 Phân chia đề xuất

```
┌──────────────── C# (.NET 8, WinUI 3 hoặc WPF) — "brain-desktop.exe" ───────────────┐
│ Nút nổi (thay widget Tk) · Panel = WebView2 nạp lại nguyên panel.html               │
│ Bridge JS↔C# (thay pywebview) · Hotkey · Ghi âm (NAudio) · TTS (System.Speech/WinRT) │
│ Tích hợp Windows: app/Start menu, ActivateForFile, registry, UI Automation (FlaUI),  │
│ SendInput, PrintWindow, clipboard, toast (Microsoft.Toolkit.Uwp.Notifications)      │
│ → Phơi các khả năng này cho core qua named pipe / HTTP localhost (JSON-RPC)          │
└──────────────────────────────────────────────────────────────────────────────────────┘
┌──────────────── Rust — "brain-core.exe" (service) ───────────────────────────────────┐
│ HTTP API (axum) giữ nguyên đường dẫn §5 · SQLite (rusqlite, bundled, FTS5) · Qdrant   │
│ (qdrant-client) · Ollama (reqwest) · scanner/watcher/index/facts/graph/contrib/gaps  │
│ agent + tools + sqlbox + governor + tasks + skills + memory + convo + doctor + eval  │
└──────────────────────────────────────────────────────────────────────────────────────┘
```
Lý do: phần nặng IO/CPU, nhiều luồng nền và quy tắc nghiệp vụ hợp với Rust (một file exe, ít RAM, không GIL); phần chạm
sâu vào Windows UI/COM/WinRT dễ và ổn định hơn nhiều trong C#.

### 12.2 Bảng tương đương thư viện

| Python | Rust | C# |
|---|---|---|
| `sqlite3` (+UDF, authorizer, FTS5) | `rusqlite` (features `bundled`, `functions`, `hooks`) — `Connection::authorizer`, `create_scalar_function` | `Microsoft.Data.Sqlite` |
| `qdrant-client` | `qdrant-client` (gRPC :6334 hoặc REST) | `Qdrant.Client` |
| `requests` (Ollama, web) | `reqwest` + `serde_json` (`ollama-rs` tùy chọn) | `HttpClient` |
| `python-docx` / `openpyxl` / `python-pptx` | `docx-rs` hoặc `zip`+`quick-xml` tự đọc OOXML; `calamine` (xlsx) | `DocumentFormat.OpenXml` |
| `pypdf` | `pdf-extract` / `lopdf` (hoặc `pdfium-render` cho chất lượng tốt hơn) | `UglyToad.PdfPig` |
| `watchdog` | `notify` (+ `notify-debouncer-full`) | `FileSystemWatcher` |
| `numpy` (cosine trong memory) | thuần Rust / `ndarray` | — |
| `Pillow` (ảnh, vẽ số lên ảnh, thumbnail) | `image` + `imageproc` + `ab_glyph` | `System.Drawing`/`SkiaSharp` |
| `imageio-ffmpeg` | gọi `ffmpeg.exe` như cũ (`std::process::Command`) | `Process` |
| `pywin32` / `ctypes` | `windows` crate (windows-rs) | P/Invoke, `Microsoft.Windows.CsWin32` |
| `comtypes` UI Automation | `uiautomation` crate | `FlaUI.UIA3` (khuyến nghị) |
| `pywebview` | — | `Microsoft.Web.WebView2` |
| Tk widget | — | WPF/WinUI cửa sổ trong suốt `AllowsTransparency`, `Topmost` |
| `sounddevice` / SAPI | `cpal` | `NAudio` / `System.Speech` hoặc `Windows.Media.SpeechSynthesis` |
| `http.server` | `axum` + `tokio` | (ASP.NET nếu đặt server trong C#) |
| `pytest` | `cargo test` + `insta` (snapshot) | xUnit |

### 12.3 Phải giữ tương thích để dùng lại dữ liệu
1. **Schema SQLite giữ nguyên** (§4.1) ⇒ mở thẳng `catalog.db` hiện có. Bản Rust phải đăng ký UDF **`norm_key`** (trigger
   name_key gọi nó) và **`norm`** (sqlbox) cho mọi kết nối, với đúng thuật toán bỏ dấu của §10 — lệch một ký tự là
   `name_key`/fact key không khớp.
2. **Qdrant**: giữ collection `chunks`, id = chunk_id, payload §4.2, cùng model embedding + cùng chuỗi instruction truy vấn.
3. **HTTP API giữ đường dẫn và JSON** ⇒ `panel.html`/`index.html` chạy lại không cần sửa (chỉ thay `window.pywebview.api`
   bằng bridge WebView2 cùng tên hàm: `open_path, pick_files, rec_start, rec_stop, speak, stop_speak, resize, hide,
   restart_server, ensure_server, open_data_dir, quit`).
4. **Prompt + mô tả công cụ chép nguyên văn** (§6.2–6.3). Đổi chữ là đổi hành vi; chạy lại `agent-eval` để so.
5. Snapshot `data/versions/<sha>.txt.gz`, `batches/*.json`, `users.json` (PBKDF2: cùng thuật toán/vòng lặp).

### 12.4 Thứ tự chuyển đề xuất (mỗi bước chạy song song với bản Python, so kết quả)
1. Rust: `catalog` + `vn/norm` + `sqlbox` + `server_acl` → đọc DB hiện có, so `name_key`/kết quả SQL với Python.
2. Rust: `llm` (cổng GPU, trần token) + `index.search` + `embed` → so top-k tìm lai với Python trên 50 truy vấn.
3. Rust: `agent` + 6 công cụ tri thức + `verify` + HTTP `/api/ask` → chạy `agent-eval` (mục tiêu ≥ 92 %).
4. Rust: scanner + extract + media + watcher + jobs + governor (học nền) → so số file/đoạn/tier sau 1 lần quét thư mục mẫu.
5. C#: desktop (nút + panel WebView2 + bridge) gọi API Rust → bỏ widget/panel Python.
6. C#: dịch vụ tích hợp Windows (apps, launcher, gui, toast, thùng rác, media keys) phơi qua named pipe; Rust gọi khi
   model gọi công cụ tương ứng.
7. Rust: facts, graph, contrib, people, gitsync, gaps, workflow, registry, bench, tasks, skills, memory, convo, doctor.
8. Tắt hẳn bản Python; giữ `train.py` riêng nếu sau này huấn luyện LoRA (hệ sinh thái huấn luyện vẫn là Python).

### 12.5 Bẫy đã gặp — đừng lặp lại
- **Hàng đợi Ollama**: Ollama xử lý lần lượt; việc nền phải nhường câu hỏi (đo được 377 s chờ). Giữ cổng GPU.
- **Trần token**: thiếu `num_predict` ⇒ model lặp vô tận (13.700 token, ~6 phút chặn hàng đợi).
- **Cùng num_ctx** cho mọi lời gọi cùng model; **embedding num_ctx nhỏ** (mặc định chiếm 5,8 GB VRAM).
- **Hai `ollama serve`** (một nghe `::`, một `127.0.0.1`) ⇒ `localhost` và `127.0.0.1` vào hai bản khác nhau. `doctor` kiểm tra.
- **Không gọi UDF trên 770.000 dòng** mỗi câu hỏi (7 s) — dùng cột tính sẵn (`name_key`).
- **SQLite nhiều luồng ghi**: transaction ngắn, commit trước khi băm file, metric ghi bất đồng bộ (từng gây khóa 90 s).
- **Watcher**: sự kiện "modified" do chính bot đọc file sinh ra ⇒ chỉ nhận modified khi mtime đổi trong 120 s; bỏ thư mục cache.
- **%TEMP% nằm trong AppData** ⇒ bị xếp tier 3; test phải dùng thư mục riêng (`.test_tmp`).
- **Gõ phím ảo + bộ gõ tiếng Việt** (Unikey/EVKey) làm rơi/lặp chữ ⇒ nhập bằng clipboard + Ctrl+V, trả lại clipboard cũ.
- **Chụp cửa sổ bị che** ⇒ PrintWindow; thao tác chuột phải đưa cửa sổ lên trước; DPI aware theo luồng.
- **Model nhỏ hay trả lời suông** thay vì gọi công cụ ⇒ cơ chế nhận diện ý định + nhắc 1 lần là cần thiết, không phải tô vẽ.
- **pywebview**: thuộc tính không có tiền tố `_` bị phơi ra JS (từng gây đệ quy) — với WebView2/C# chỉ phơi đúng lớp bridge.

### 12.6 Nợ kỹ thuật hiện có (nên sửa luôn khi viết lại)
- `agent.py` quá lớn (678 dòng, prompt + intent + vòng lặp + bằng chứng chung một file) ⇒ tách module.
- Prompt injection chỉ được chống bằng lời dặn ⇒ nên cách ly nội dung không tin cậy (đánh dấu, tóm tắt qua bước riêng).
- Gate GPU chỉ có hiệu lực trong một tiến trình ⇒ bản mới nên có một "LLM broker" duy nhất cho mọi tiến trình.
- 3 câu `agent-eval` còn trượt: tìm file theo tên (model trả lời từ sơ bộ, không gọi công cụ), liệt kê việc đã hẹn
  (gọi nhầm sql_catalog), dạy kỹ năng (không gọi save_skill).
- `data/qdrant` 1,9 GB cũ không dùng; `train_worker.py` chưa từng chạy được.
- Đóng góp: `buitienthanhdev` và `TIEN THANH` đang là hai người (chưa hợp nhất).

---

## 13. Kiểm thử

| Bộ | Nội dung |
|---|---|
| `test_acceptance.py` | 8 kịch bản nghiệm thu đầu-cuối (quét → học → hỏi → quyền → phiên bản → đóng góp) + resume workflow |
| `test_units.py` | tier, ACL, tiếng Việt (Telex/tiếng lóng), bench, contrib, name_key… |
| `test_quality.py` | hồi quy trên dữ liệu thật (đóng góp phình, lỗ hổng giả, trích hàng loạt) |
| `test_governor.py` | nhịp học thích ứng |
| `test_launcher.py`, `test_computer.py`, `test_batch.py`, `test_gui.py` | công cụ hành động — mọi thao tác thật bị mock |
| `test_assistant.py` | hẹn giờ, việc nền, kỹ năng, trí nhớ hội thoại |
| `test_llm_gate.py` | cổng GPU, trần token |
| `test_live.py` (`--live`) | 6 bài với Ollama thật |
| `agent_eval` | 36 câu tiếng Việt tự nhiên, dry-run, chấm công cụ/tham số/câu trả lời |

Hạ tầng test: dữ liệu riêng `.test_tmp/run_*`, Qdrant in-memory, embedding giả xác định (túi từ băm 128 chiều), theo dõi và
đóng mọi kết nối SQLite sau mỗi test. Khi viết lại nên chuyển nguyên các kịch bản này thành test Rust/C# trước khi chuyển code.

---

## 14. Thay đổi sau ảnh chụp 2026-10-07 (cập nhật 2026-10-09)

**Số liệu hiện tại (đo 2026-10-09):** 50 module `brain/*.py` ≈ 11.060 dòng · `web/panel.html` 895 + `web/index.html` 434 ·
13 file test, **133 pass + 6 skip** (`python -m pytest tests -q`, 21 s) · `catalog.db`: 891.593 file, 215.070 đoạn, 91 fact,
26.490 thực thể, 51.624 quan hệ, 1.072 người, 11.858 đóng góp, 197.886 sự kiện bus.

**Hợp nhất agent:** `local-gemini-agent/` (repo git riêng; Rust core + cửa sổ chat + gateway Gemini + Python worker UIA/browser/OCR/AutoCAD)
là agent duy nhất. Brain phục vụ công cụ `knowledge.*` qua HTTP `http://127.0.0.1:8765` (adapter, không import chéo vì Python 3.14 ≠ 3.12 và
`brain_core` build theo từng interpreter). Bảng quyết định giữ/bỏ từng module: `local-gemini-agent/docs/unification.md`.
Giữ: index, sqlbox, graph, extract, scanner, facts, vn, Qdrant, catalog. Retire: agent, computer, gui, apps, launcher, actions, skills,
tasks, convo, memory, widget, panel (mã còn lại để tham chiếu và có test, không thêm tính năng).

**Lõi Rust `brain_core`** (`rust/brain_core` + `rust/brain_core_py`, PyO3 0.27 abi3-py310, maturin; build `scriptsuild_rust.ps1`):
`text.rs` (`norm`, `telex`) · `scan.rs` (`walk` song song, jwalk) · `hash.rs` (`hash_file(s)` sha256, nhả GIL) · `sysmon.rs` (NVML cho governor).
Cờ môi trường `USE_RUST_NORM` / `USE_RUST_SCANNER` / `USE_RUST_GOVERNOR` (mặc định 1; `0` = bản Python), `RUST_SCAN_THREADS=4`.
Parity: `norm` 0 khác trên 985.153 chuỗi thật, nhanh 3,9x; duyệt 0 khác trên 665.839 + 76.655 file; quét lại thư mục người dùng
167,2 s → 20,3 s (8,2x). Test: `tests/test_rust_parity.py` (bỏ qua nếu chưa build). Chưa chuyển: policy/ACL (rủi ro rò rỉ — cần parity toàn bộ path),
watcher/event bus. Chi tiết: `docs/MIGRATION_MAP.md`.

**Module mới/đổi so với §3:** `backup.py` (sao lưu `catalog.db` online mỗi ngày → `D:\CompanyBrainBackups`, giữ 7 bản, `versions/` tăng dần,
`PRAGMA quick_check` + đối chiếu số đoạn Qdrant; bước `backup` của workflow MAINTENANCE; lệnh `python -m brain backup`) ·
`jobs.py` · `server.py` thêm chống DNS rebinding (Host/Origin), POST chỉ `application/json`, khoá đăng nhập 15 phút sau 5 lần sai/IP (audit
`login_failed`), log lỗi `data/logs/server.log` · `llm.py` stream + ngắt việc nền khi có câu hỏi · CLI thêm `backup`, `autonomy on|off`.
HTTPS vẫn chưa bật.

**Hiệu năng:** câu hỏi 420 s → ~20 s (đo 2026-10-07, các nguyên nhân ở §2.4/§12.5 và README §9); danh sách công cụ admin cố định ⇒ cache prompt Ollama
(đọc prompt 3,7 s → 0,8 s); `/api/ask` hỗ trợ `stream:true` (NDJSON tiến trình).

**Còn mở:** 3 câu `agent-eval` trượt (§12.6, phần agent legacy); đóng góp `buitienthanhdev` và `TIEN THANH` chưa hợp nhất; LoRA chưa chạy được;
OCR PDF scan chưa có; `data/qdrant` cũ (1,9 GB) không dùng.
