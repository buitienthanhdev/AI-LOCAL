"""Corporate Catalog: mỗi file là một record xác định, kèm lịch sử thay đổi (event ledger)."""
import sqlite3
from .config import CATALOG_DB, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id        INTEGER PRIMARY KEY,
    path           TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    ext            TEXT,
    size           INTEGER,
    sha256         TEXT,
    fs_created_at  TEXT,
    fs_modified_at TEXT,
    fs_owner       TEXT,      -- chủ sở hữu NTFS (không phải "người tạo" thật)
    doc_author     TEXT,      -- author khai trong metadata tài liệu
    doc_last_editor TEXT,     -- lastModifiedBy trong metadata
    doc_created_at TEXT,
    title          TEXT,
    department     TEXT,      -- suy từ thư mục cấp 1
    project        TEXT,      -- suy từ thư mục cấp 2
    acl            TEXT,      -- danh sách nhóm được đọc, phân tách bằng dấu phẩy
    status         TEXT DEFAULT 'active',   -- active | deleted
    indexed_sha256 TEXT,      -- hash của lần đã embed gần nhất
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    extract_error  TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256);

-- Event ledger: ai/cái gì/khi nào. MVP ghi các sự kiện quan sát được khi quét.
CREATE TABLE IF NOT EXISTS file_events (
    event_id   INTEGER PRIMARY KEY,
    file_id    INTEGER REFERENCES files(file_id),
    event      TEXT NOT NULL,   -- discovered | modified | deleted | restored
    actor      TEXT,            -- người liên quan theo bằng chứng tốt nhất hiện có
    at         TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_file ON file_events(file_id);

-- Hàng đợi sự kiện từ watcher. UNIQUE(path) => nhiều event liên tiếp của 1 file gộp thành 1 (debounce).
CREATE TABLE IF NOT EXISTS ingest_queue (
    path        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,      -- upsert | delete
    moved_from  TEXT,
    first_at    REAL NOT NULL,
    last_at     REAL NOT NULL,      -- chỉ xử lý khi file "yên" vài giây
    attempts    INTEGER DEFAULT 0,
    error       TEXT                -- quá số lần thử => nằm lại đây (dead-letter)
);

-- Nhật ký hỏi đáp + phản hồi: nguồn dữ liệu để đánh giá và fine-tune sau này.
CREATE TABLE IF NOT EXISTS qa_log (
    qa_id      INTEGER PRIMARY KEY,
    user       TEXT,
    groups     TEXT,
    question   TEXT NOT NULL,
    answer     TEXT,
    sources    TEXT,               -- JSON
    steps      TEXT,               -- JSON
    model      TEXT,
    created_at TEXT NOT NULL,
    latency_s  REAL,
    rating     INTEGER,            -- 1 đúng, -1 sai, NULL chưa đánh giá
    comment    TEXT,
    rated_at   TEXT
);

-- Fact Store: tri thức có nguồn, thời gian hiệu lực, độ tin cậy, trạng thái.
CREATE TABLE IF NOT EXISTS facts (
    fact_id        INTEGER PRIMARY KEY,
    subject        TEXT NOT NULL,
    predicate      TEXT NOT NULL,
    value          TEXT NOT NULL,
    unit           TEXT,
    subject_key    TEXT NOT NULL,   -- dạng chuẩn hóa để so khớp
    predicate_key  TEXT NOT NULL,
    value_key      TEXT NOT NULL,
    evidence       TEXT,            -- trích nguyên văn đoạn chứa fact
    source_file_id INTEGER REFERENCES files(file_id),
    source_sha256  TEXT,
    author         TEXT,
    valid_from     TEXT,
    valid_to       TEXT,            -- NULL = còn hiệu lực
    base_confidence REAL,           -- theo loại nguồn
    confidence     REAL,            -- sau khi cộng đồng thuận / trừ mâu thuẫn
    status         TEXT NOT NULL,   -- candidate | usable | trusted | conflicted | superseded | approved | rejected
    note           TEXT,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_facts_key ON facts(subject_key, predicate_key);
CREATE INDEX IF NOT EXISTS ix_facts_file ON facts(source_file_id);
"""

SCHEMA2 = """
-- Đoạn nội dung (nguồn thật cho trích dẫn) + tìm toàn văn FTS5 (bỏ dấu tiếng Việt)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, idx INTEGER, loc TEXT, text TEXT NOT NULL,
    file_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text); END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text); END;

-- Event bus bền (sống sót qua restart): 1 dòng pending / object => gom (debounce) bền vững
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, actor_id TEXT, source_system TEXT NOT NULL DEFAULT 'filesystem',
    object_id TEXT NOT NULL, ts REAL NOT NULL, last_at REAL NOT NULL, correlation_id TEXT, payload_hash TEXT,
    previous_version INTEGER, new_version INTEGER, moved_from TEXT,
    processing_status TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | done | failed | dead
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, processed_at TEXT, claimed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_pending ON events(object_id) WHERE processing_status='pending';
CREATE INDEX IF NOT EXISTS ix_events_status ON events(processing_status, last_at);

-- Lịch sử phiên bản nội dung của file
CREATE TABLE IF NOT EXISTS file_versions (
    version_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, version_no INTEGER NOT NULL, sha256 TEXT, size INTEGER,
    fs_modified_at TEXT, editor TEXT, observed_at TEXT NOT NULL, snapshot TEXT,
    diff_added INTEGER, diff_removed INTEGER, diff_summary TEXT, UNIQUE(file_id, version_no)
);

-- Git
CREATE TABLE IF NOT EXISTS repos (repo_id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, last_sync TEXT);
CREATE TABLE IF NOT EXISTS git_commits (
    sha TEXT PRIMARY KEY, repo_id INTEGER NOT NULL, author_name TEXT, author_email TEXT, authored_at TEXT,
    subject TEXT, files_changed INTEGER, insertions INTEGER, deletions INTEGER
);

-- Con người + sổ đóng góp
CREATE TABLE IF NOT EXISTS people (
    person_id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_aliases (alias_key TEXT PRIMARY KEY, person_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS contributions (
    contrib_id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, kind TEXT NOT NULL, weight REAL NOT NULL,
    file_id INTEGER, project TEXT, department TEXT, at TEXT NOT NULL, evidence TEXT, ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_contrib_person ON contributions(person_id, at);
CREATE INDEX IF NOT EXISTS ix_contrib_project ON contributions(project, at);

-- Đồ thị tri thức
CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, key TEXT NOT NULL, UNIQUE(type, key)
);
CREATE TABLE IF NOT EXISTS relations (
    rel_id INTEGER PRIMARY KEY, src INTEGER NOT NULL, rel TEXT NOT NULL, dst INTEGER NOT NULL, source_file_id INTEGER,
    evidence TEXT, valid_from TEXT, valid_to TEXT, confidence REAL DEFAULT 0.8, ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_rel_src ON relations(src);
CREATE INDEX IF NOT EXISTS ix_rel_dst ON relations(dst);

-- Lỗ hổng tri thức + việc nghiên cứu tự sinh
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    gap_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT, detail TEXT, evidence TEXT,
    status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT, fingerprint TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS research_tasks (
    task_id INTEGER PRIMARY KEY, gap_id INTEGER, status TEXT NOT NULL DEFAULT 'open', candidates TEXT,
    created_at TEXT NOT NULL, finished_at TEXT
);

-- Model registry / benchmark / triển khai
CREATE TABLE IF NOT EXISTS models (
    model_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, role TEXT NOT NULL DEFAULT 'chat', base_model TEXT,
    quant TEXT, status TEXT NOT NULL DEFAULT 'candidate',   -- candidate|benchmarked|canary|production|retired
    canary_percent INTEGER DEFAULT 0, score REAL, parent TEXT, dataset_version TEXT, notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id INTEGER PRIMARY KEY, model TEXT NOT NULL, suite TEXT NOT NULL, score REAL, detail TEXT,
    latency_p50 REAL, tokens_per_s REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    dep_id INTEGER PRIMARY KEY, role TEXT NOT NULL, action TEXT NOT NULL, from_model TEXT, to_model TEXT,
    reason TEXT, at TEXT NOT NULL
);

-- Workflow bền (resume sau restart), số liệu quan sát, nhật ký kiểm toán
CREATE TABLE IF NOT EXISTS workflows (
    wf_id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS metrics (ts REAL NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, tags TEXT);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(name, ts);
CREATE TABLE IF NOT EXISTS audit (
    audit_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, detail TEXT
);
"""

MIGRATIONS = [
    "ALTER TABLE files ADD COLUMN facts_sha256 TEXT",   # hash của lần trích fact gần nhất
    "ALTER TABLE files ADD COLUMN tier INTEGER",        # 1 tài liệu/media · 2 mã nguồn · 3 chỉ lập danh mục
    "ALTER TABLE files ADD COLUMN version_no INTEGER DEFAULT 1",
    "CREATE INDEX IF NOT EXISTS ix_files_tier ON files(tier, status)",
    # tên + tiêu đề đã bỏ dấu, lưu sẵn: tìm theo tên trên ~770.000 file không phải gọi hàm Python cho từng dòng (7 s -> <1 s)
    "ALTER TABLE files ADD COLUMN name_key TEXT",
    """CREATE TRIGGER IF NOT EXISTS tr_files_name_key_ins AFTER INSERT ON files BEGIN
         UPDATE files SET name_key = norm_key(NEW.name || ' ' || ifnull(NEW.title, '')) WHERE file_id = NEW.file_id; END""",
    """CREATE TRIGGER IF NOT EXISTS tr_files_name_key_upd AFTER UPDATE OF name, title ON files BEGIN
         UPDATE files SET name_key = norm_key(NEW.name || ' ' || ifnull(NEW.title, '')) WHERE file_id = NEW.file_id; END""",
]
_ready: set[str] = set()


def _norm_key(s):
    from .facts import norm  # import muộn: facts phụ thuộc catalog
    return norm(s) if s is not None else None


def _backfill_name_key(conn: sqlite3.Connection):
    """Điền name_key cho các dòng cũ (một lần; theo lô để không giữ khóa ghi quá lâu)."""
    last = 0
    while True:
        ids = [r[0] for r in conn.execute("SELECT file_id FROM files WHERE name_key IS NULL AND file_id > ? "
                                          "ORDER BY file_id LIMIT 20000", (last,))]
        if not ids:
            break
        conn.execute(f"UPDATE files SET name_key = norm_key(name || ' ' || ifnull(title, '')) "
                     f"WHERE file_id BETWEEN ? AND ? AND name_key IS NULL", (ids[0], ids[-1]))
        conn.commit()
        last = ids[-1]


def connect(readonly: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(CATALOG_DB, timeout=90)
        conn.execute("PRAGMA busy_timeout=90000")
        conn.create_function("norm_key", 1, _norm_key, deterministic=True)  # trigger name_key cần trên MỌI kết nối ghi
        if str(CATALOG_DB) not in _ready:  # tạo/migrate schema một lần mỗi tiến trình
            conn.execute("PRAGMA journal_mode=WAL")  # watcher, job nền và web ghi/đọc đồng thời
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.executescript(SCHEMA2)
            for m in MIGRATIONS:
                try:
                    conn.execute(m)
                except sqlite3.OperationalError:
                    pass  # đã áp dụng
            conn.commit()
            _backfill_name_key(conn)
            _ready.add(str(CATALOG_DB))
    conn.row_factory = sqlite3.Row
    return conn
