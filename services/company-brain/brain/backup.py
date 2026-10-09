"""Sao lưu + kiểm tra toàn vẹn dữ liệu.

- catalog.db: bản sao NHẤT QUÁN bằng SQLite online backup API (chạy được khi bot đang ghi, WAL), mỗi ngày một bản, giữ 7.
  Lưu sang Ổ KHÁC nếu có (mặc định D:\\CompanyBrainBackups) — hỏng ổ C vẫn còn dữ liệu.
- versions/ (snapshot văn bản để so phiên bản): chép thêm file mới (tăng dần).
- Qdrant KHÔNG sao lưu: vector dựng lại được từ catalog (`python -m brain index`).
- Kiểm tra toàn vẹn: PRAGMA quick_check + số đoạn trong Qdrant so với bảng chunks.
Khôi phục: tắt bot, chép file catalog-YYYYMMDD.db đè lên data/catalog.db (xoá catalog.db-wal / -shm), bật lại."""
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .catalog import connect
from .config import CATALOG_DB, DATA_DIR

KEEP = 7
EVERY_H = 20


def backup_dir() -> Path:
    env = os.environ.get("BRAIN_BACKUP_DIR")
    if env:
        return Path(env)
    if Path("D:/").exists() and str(DATA_DIR).upper()[:1] != "D":
        return Path("D:/CompanyBrainBackups")
    return DATA_DIR / "backups" / "db"


def _setting(conn, key: str, value=None):
    if value is None:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def status() -> dict:
    c = connect()
    s = {"backup": _setting(c, "last_backup"), "integrity": _setting(c, "last_integrity")}
    c.close()
    return s


def run_backup(force: bool = False) -> dict:
    c = connect()
    last = _setting(c, "last_backup")
    if not force and last and time.time() - last.get("ts", 0) < EVERY_H * 3600:
        c.close()
        return {"skipped": "đã sao lưu trong 20 giờ qua", **last}
    dest_dir = backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    t = time.time()
    tmp = dest_dir / f"catalog-{datetime.now():%Y%m%d-%H%M}.db.part"
    dst = sqlite3.connect(tmp)
    src = sqlite3.connect(CATALOG_DB, timeout=90)
    src.backup(dst, pages=4096, sleep=0.01)  # từng phần nhỏ => không giữ khóa đọc lâu, bot vẫn ghi được
    src.close()
    ok = dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    dst.close()
    final = tmp.with_suffix("")
    tmp.replace(final)
    olds = sorted(dest_dir.glob("catalog-*.db"))
    for o in olds[:-KEEP]:
        o.unlink(missing_ok=True)
    vdir, vcopied = DATA_DIR / "versions", 0  # snapshot phiên bản: chép tăng dần
    if vdir.exists():
        vdst = dest_dir / "versions"
        vdst.mkdir(exist_ok=True)
        for f in vdir.iterdir():
            if f.is_file() and not (vdst / f.name).exists():
                shutil.copy2(f, vdst / f.name)
                vcopied += 1
    info = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds"), "file": str(final),
            "mb": round(final.stat().st_size / 2**20), "seconds": round(time.time() - t, 1), "check_ok": ok,
            "versions_copied": vcopied, "kept": min(len(olds), KEEP)}
    _setting(c, "last_backup", info)
    c.close()
    from . import obs
    obs.audit("system", "backup", info["file"], f"{info['mb']} MB, kiểm tra {'ok' if ok else 'LỖI'}")
    return info


def run_integrity() -> dict:
    t = time.time()
    c = connect()
    qc = c.execute("PRAGMA quick_check").fetchone()[0]
    chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    points = None
    try:
        from .config import COLLECTION
        from .index import client
        points = client().count(COLLECTION, exact=True).count
    except Exception:
        pass
    info = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds"), "quick_check": qc,
            "chunks": chunks, "vector_points": points, "seconds": round(time.time() - t, 1),
            "ok": qc == "ok" and (points is None or abs(points - chunks) <= max(50, chunks // 100))}
    _setting(c, "last_integrity", info)
    c.close()
    return info
