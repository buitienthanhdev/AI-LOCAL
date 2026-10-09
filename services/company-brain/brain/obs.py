"""Quan sát (observability) + kiểm toán. Số liệu ghi xuống DB để xem lại và dựng dashboard; không bao giờ làm hỏng
luồng chính (mọi lỗi ghi số liệu bị nuốt)."""
import json
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from .catalog import connect
from .config import CATALOG_DB


_q: queue.SimpleQueue = queue.SimpleQueue()
_started = False
_slock = threading.Lock()


def _write_batch(rows: list) -> None:
    conn = sqlite3.connect(CATALOG_DB, timeout=5)  # ngắn: số liệu chỉ là phụ trợ, không được chặn ai
    try:
        conn.executemany("INSERT INTO metrics (ts, name, value, tags) VALUES (?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def flush() -> None:
    """Ghi ngay các số liệu đang chờ (dùng trước khi đọc tổng hợp / trong test)."""
    rows = []
    while True:
        try:
            rows.append(_q.get_nowait())
        except queue.Empty:
            break
    if rows:
        try:
            connect().close()  # đảm bảo schema đã có
            _write_batch(rows)
        except sqlite3.Error:
            pass  # số liệu là best-effort


def _flusher():
    while True:
        time.sleep(2)
        flush()


def metric(name: str, value: float, **tags):
    """Ghi số liệu KHÔNG BAO GIỜ chặn nơi gọi: đưa vào hàng đợi, luồng nền ghi theo lô."""
    global _started
    _q.put((time.time(), name, float(value), json.dumps(tags, ensure_ascii=False) if tags else None))
    if not _started:
        with _slock:
            if not _started:
                threading.Thread(target=_flusher, daemon=True, name="metrics-flusher").start()
                _started = True


@contextmanager
def timed(name: str, **tags):
    t = time.time()
    try:
        yield
    except Exception:
        metric("error." + name, 1, **tags)
        raise
    finally:
        metric(name, time.time() - t, **tags)


def audit(actor: str | None, action: str, target: str | None = None, detail: str | None = None):
    try:
        conn = connect()
        conn.execute("INSERT INTO audit (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), actor, action, target, (detail or "")[:1000]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def summary(window_s: int = 3600) -> dict:
    """Tổng hợp số liệu trong cửa sổ thời gian: số lượng, trung bình, p95 theo từng loại."""
    flush()
    conn = connect()
    since = time.time() - window_s
    out: dict = {}
    for name in [r[0] for r in conn.execute("SELECT DISTINCT name FROM metrics WHERE ts>=?", (since,))]:
        vals = sorted(r[0] for r in conn.execute("SELECT value FROM metrics WHERE name=? AND ts>=?", (name, since)))
        out[name] = {"n": len(vals), "avg": round(sum(vals) / len(vals), 3), "p95": round(vals[int(0.95 * (len(vals) - 1))], 3),
                     "max": round(vals[-1], 3)}
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out["_queue"] = {
        "pending": q("SELECT COUNT(*) FROM events WHERE processing_status='pending'"),
        "processing": q("SELECT COUNT(*) FROM events WHERE processing_status='processing'"),
        "dead": q("SELECT COUNT(*) FROM events WHERE processing_status='dead'"),
    }
    conn.close()
    return out


def prune(days: int = 14):
    """Giữ số liệu 14 ngày."""
    conn = connect()
    conn.execute("DELETE FROM metrics WHERE ts<?", (time.time() - days * 86400,))
    conn.commit()
    conn.close()
