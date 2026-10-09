"""Event bus BỀN cho học realtime: watcher (ReadDirectoryChangesW) -> bảng `events` -> worker -> catalog + index.

Bảo đảm:
- Bền: sự kiện nằm trong DB; crash/restart không mất. Sự kiện đang xử lý dở được đặt lại 'pending' khi khởi động.
- Idempotent + khử trùng lặp: tối đa 1 sự kiện 'pending' mỗi object (chỉ mục duy nhất từng phần) => lưu/copy file
  bắn hàng chục sự kiện vẫn chỉ xử lý 1 lần; xử lý lại cũng không sinh dữ liệu trùng (so size/mtime/sha256).
- Debounce: chỉ xử lý khi file "yên" DEBOUNCE_SEC giây.
- Thử lại có lùi dần (backoff); quá MAX_ATTEMPTS => 'dead' (dead-letter, xem/replay được).
- Backpressure: mỗi vòng chỉ nhận BATCH sự kiện; việc nặng (embed) chỉ làm ngay khi ít file thay đổi."""
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import obs
from .catalog import connect
from .config import DEBOUNCE_SEC, REALTIME_INDEX_MAX, WATCH_IGNORE
from .scanner import _now, is_candidate, mark_deleted, process_file, size_ok

MAX_ATTEMPTS = 5
BATCH = 200
state = {"watching": [], "processed": 0, "queued": 0, "last_event": None, "last_error": None, "dead": 0}

_buf: dict[str, tuple[str, str | None, float]] = {}
_blk = threading.Lock()
_stop = threading.Event()


# ---------------------------------------------------------------- phát sự kiện
def emit(event_type: str, object_id: str, moved_from: str | None = None, actor: str | None = None,
         source: str = "filesystem", conn=None) -> None:
    """Ghi sự kiện vào bus (bền). Gộp với sự kiện 'pending' cũ của cùng object."""
    own = conn is None
    conn = conn or connect()
    now = time.time()
    conn.execute("""INSERT INTO events (event_id, event_type, actor_id, source_system, object_id, ts, last_at,
                    correlation_id, moved_from, processing_status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'pending',?)
                    ON CONFLICT(object_id) WHERE processing_status='pending' DO UPDATE SET
                      last_at=excluded.last_at,
                      event_type=CASE WHEN events.event_type='FILE_CREATED' AND excluded.event_type='FILE_MODIFIED'
                                      THEN 'FILE_CREATED' ELSE excluded.event_type END,
                      moved_from=COALESCE(excluded.moved_from, events.moved_from)""",
                 (str(uuid.uuid4()), event_type, actor, source, object_id, now, now, str(uuid.uuid4())[:8],
                  moved_from, datetime.now().isoformat(timespec="seconds")))
    if own:
        conn.commit()
        conn.close()
    state["last_event"] = f"{event_type} {object_id}"


def _ignored(path: str) -> bool:
    """Thư mục cache/tạm ghi liên tục: bỏ qua ở realtime (quét định kỳ vẫn đọc)."""
    low = path.lower().replace("\\", "/")
    return any(k in low for k in WATCH_IGNORE)


class _Handler(FileSystemEventHandler):
    def on_any_event(self, event):
        if event.is_directory or _ignored(event.src_path):
            return
        try:
            src = Path(event.src_path)
            t = event.event_type
            if t == "moved":
                dest = Path(event.dest_path)
                if is_candidate(src):
                    self._buffer("FILE_DELETED", str(src))
                if is_candidate(dest):
                    self._buffer("FILE_MOVED", str(dest), moved_from=str(src))
            elif t == "deleted":
                if is_candidate(src):
                    self._buffer("FILE_DELETED", str(src))
            elif t == "created":
                if is_candidate(src):
                    self._buffer("FILE_CREATED", str(src))
            elif t in ("modified", "closed"):
                # Windows báo "modified" cả khi file chỉ bị ĐỌC (đổi giờ truy cập) — kể cả lúc chính bot quét.
                # Chỉ nhận khi nội dung vừa được ghi gần đây.
                if is_candidate(src):
                    try:
                        recent = time.time() - src.stat().st_mtime < 120
                    except OSError:
                        recent = False
                    if recent:
                        self._buffer("FILE_MODIFIED", str(src))
        except Exception as e:  # watcher không bao giờ được chết vì 1 event lỗi
            state["last_error"] = f"{type(e).__name__}: {e}"

    @staticmethod
    def _buffer(kind: str, path: str, moved_from: str | None = None):
        with _blk:
            _buf[path] = (kind, moved_from, time.time())


def _flusher():
    """Ghi sự kiện đệm xuống DB theo lô mỗi giây (một giao dịch ngắn, không ghi theo từng sự kiện)."""
    while not _stop.is_set():
        time.sleep(1)
        with _blk:
            items = list(_buf.items())
            _buf.clear()
        if not items:
            continue
        try:
            conn = connect()
            for path, (kind, moved_from, _) in items:
                emit(kind, path, moved_from, conn=conn)
            conn.commit()
            conn.close()
        except Exception as e:
            state["last_error"] = f"flush: {type(e).__name__}: {e}"
            with _blk:  # không mất sự kiện: trả lại bộ đệm để thử lần sau
                for path, v in items:
                    _buf.setdefault(path, v)
            time.sleep(3)


# ---------------------------------------------------------------- xử lý sự kiện
def recover():
    """Khởi động lại: sự kiện đang 'processing' dở (crash giữa chừng) quay về 'pending' để xử lý lại an toàn."""
    conn = connect()
    n = conn.execute("UPDATE events SET processing_status='pending', claimed_at=NULL "
                     "WHERE processing_status='processing'").rowcount
    conn.commit()
    conn.close()
    return n


def _claim(conn, limit=BATCH) -> list:
    cutoff = time.time() - DEBOUNCE_SEC
    rows = conn.execute("""UPDATE events SET processing_status='processing', claimed_at=?
                           WHERE event_id IN (SELECT event_id FROM events WHERE processing_status='pending'
                                              AND last_at<=? ORDER BY last_at LIMIT ?) RETURNING *""",
                        (time.time(), cutoff, limit)).fetchall()
    conn.commit()
    return rows


def _finish(conn, ev, ok: bool, err: str | None = None, **upd):
    now = datetime.now().isoformat(timespec="seconds")
    if ok:
        conn.execute("""UPDATE events SET processing_status='done', processed_at=?, error=NULL,
                        payload_hash=COALESCE(?, payload_hash), previous_version=?, new_version=? WHERE event_id=?""",
                     (now, upd.get("sha"), upd.get("prev"), upd.get("new"), ev["event_id"]))
    else:
        attempts = ev["attempts"] + 1
        status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
        try:
            conn.execute("""UPDATE events SET processing_status=?, attempts=?, error=?, claimed_at=NULL,
                            last_at=? WHERE event_id=?""",
                         (status, attempts, err, time.time() + 2 ** attempts, ev["event_id"]))
        except Exception:  # đã có sự kiện pending mới hơn cho object này => sự kiện này bị thay thế
            conn.execute("UPDATE events SET processing_status='done', processed_at=?, error='superseded' "
                         "WHERE event_id=?", (now, ev["event_id"]))
    conn.commit()


def process_pending() -> list[int]:
    """Nhận và xử lý các sự kiện đã 'yên'. Trả về file_id mới/đổi cần index. (Gọi thẳng được trong test.)"""
    conn = connect()
    changed, stats = [], {"new": 0, "modified": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    now = _now()
    for ev in _claim(conn):
        t0 = time.time()
        p = Path(ev["object_id"])
        try:
            prev = conn.execute("SELECT version_no FROM files WHERE path=?", (ev["object_id"],)).fetchone()
            sha = None
            if ev["event_type"] == "FILE_DELETED" or not p.exists():
                mark_deleted(conn, ev["object_id"], now, detail="watcher")
            else:
                before = stats["skipped"]
                fid = process_file(conn, p, Path(p.anchor), now, stats, moved_from=ev["moved_from"])
                if stats["skipped"] > before and p.exists() and size_ok(p, p.stat().st_size):
                    raise OSError("file đang bị khóa")  # thử lại sau (backoff)
                if fid:
                    changed.append(fid)
                    r = conn.execute("SELECT sha256 FROM files WHERE file_id=?", (fid,)).fetchone()
                    sha = r[0] if r else None
            new = conn.execute("SELECT version_no FROM files WHERE path=?", (ev["object_id"],)).fetchone()
            conn.commit()
            _finish(conn, ev, True, sha=sha, prev=prev[0] if prev else None, new=new[0] if new else None)
            state["processed"] += 1
            obs.metric("ingest_latency_s", time.time() - ev["ts"])
        except Exception as e:
            conn.rollback()
            err = f"{type(e).__name__}: {e}"
            state["last_error"] = err
            _finish(conn, ev, False, err)
            obs.metric("error.ingest", 1)
    q = conn.execute("""SELECT SUM(processing_status='pending'), SUM(processing_status='dead')
                        FROM events WHERE processing_status IN ('pending','dead')""").fetchone()
    state["queued"], state["dead"] = q[0] or 0, q[1] or 0
    conn.close()
    return changed


def replay(statuses=("dead", "failed")) -> int:
    """Đưa lại các sự kiện lỗi vào hàng đợi (sau khi đã sửa nguyên nhân)."""
    conn = connect()
    n = 0
    for ev in conn.execute(f"SELECT event_id, object_id FROM events WHERE processing_status IN "
                           f"({','.join('?' * len(statuses))})", statuses).fetchall():
        try:
            conn.execute("UPDATE events SET processing_status='pending', attempts=0, error=NULL, last_at=? "
                         "WHERE event_id=?", (time.time(), ev["event_id"]))
            n += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return n


def prune(days: int = 7):
    conn = connect()
    conn.execute("DELETE FROM events WHERE processing_status='done' AND ts<?", (time.time() - days * 86400,))
    conn.commit()
    conn.close()


def _worker(on_changed):
    last_prune = 0.0
    while not _stop.is_set():
        try:
            changed = process_pending()
            if changed and on_changed:
                on_changed(changed)
            if time.time() - last_prune > 3600:
                prune()
                last_prune = time.time()
        except Exception as e:
            state["last_error"] = f"{type(e).__name__}: {e}"
        time.sleep(2)


def start(roots: list[Path], on_changed=None) -> Observer:
    """on_changed(file_ids): gọi sau khi catalog đã cập nhật các file mới/đổi."""
    _stop.clear()
    n = recover()
    if n:
        state["last_event"] = f"khôi phục {n} sự kiện đang xử lý dở"
    observer = Observer()
    for r in roots:
        try:
            observer.schedule(_Handler(), str(r), recursive=True)
            state["watching"].append(str(r))
        except OSError as e:
            state["last_error"] = f"Không theo dõi được {r}: {e}"
    observer.daemon = True
    observer.start()
    threading.Thread(target=_flusher, daemon=True, name="event-flusher").start()
    threading.Thread(target=_worker, args=(on_changed,), daemon=True, name="ingest-worker").start()
    return observer


def stop(observer: Observer | None = None):
    """Dừng watcher + các luồng nền (dùng khi tắt máy chủ / trong test)."""
    _stop.set()
    state["watching"].clear()
    if observer is not None:
        observer.stop()
        observer.join(timeout=5)
    time.sleep(2.2)  # chờ các luồng nền thoát vòng lặp


def realtime_index(file_ids: list[int]):
    """Ít file => embed + trích fact ngay (vài giây GPU). Nhiều => để job nền chạy khi máy rảnh."""
    if len(file_ids) > REALTIME_INDEX_MAX:
        return
    from . import governor
    from .facts import extract_facts_for
    from .index import index_all
    governor.mark_background()  # học ngầm => cũng chỉ dùng tối đa RESOURCE_LIMIT
    index_all(file_ids=file_ids, throttle=governor.rest)
    governor.rest()
    extract_facts_for(file_ids)
