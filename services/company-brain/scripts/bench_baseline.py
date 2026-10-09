"""Baseline hiệu năng TRƯỚC khi chuyển sang Rust (Phase 0/19). CHỈ ĐỌC dữ liệu thật; phần event bus chạy trên DB tạm.

    python scripts/bench_baseline.py [--walk-root C:\\Users\\TIEN THANH] [--hash-mb 1024]

Kết quả in ra + ghi docs/bench/baseline-<ngày>.json để so sánh sau migration (cùng máy, cùng lệnh)."""
import argparse
import hashlib
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def bench_norm(names):
    from brain.facts import norm
    from brain.vn import telex_variants
    t = time.perf_counter()
    for n in names:
        norm(n)
    dt = time.perf_counter() - t
    sample = names[:5000]
    t = time.perf_counter()
    for n in sample:
        telex_variants(n[:40])
    dt2 = time.perf_counter() - t
    return {"norm_calls": len(names), "norm_s": round(dt, 2), "norm_per_s": int(len(names) / dt),
            "telex_calls": len(sample), "telex_s": round(dt2, 2), "telex_per_s": int(len(sample) / dt2)}


def bench_walk(root: Path):
    """Duyệt + stat giống scanner.scan (cùng bộ lọc thư mục/đuôi), KHÔNG ghi DB, không băm."""
    from brain.config import SUPPORTED_EXT
    from brain.scanner import _dir_excluded
    t = time.perf_counter()
    dirs = files = cand = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirs += 1
        dirnames[:] = [d for d in dirnames if d != ".git" and not _dir_excluded(d, os.path.join(dirpath, d))]
        for fn in filenames:
            files += 1
            if os.path.splitext(fn)[1].lower() in SUPPORTED_EXT and not fn.startswith("~$"):
                try:
                    os.stat(os.path.join(dirpath, fn))
                    cand += 1
                except OSError:
                    pass
    dt = time.perf_counter() - t
    return {"root": str(root), "dirs": dirs, "files_seen": files, "candidates_stat": cand, "walk_s": round(dt, 1),
            "files_per_s": int(files / dt) if dt else None}


def bench_unchanged_check(db: Path, n=20000):
    """Chi phí 'file có đổi không' của scanner: 1 SELECT theo path / file (đường nóng khi quét lại 770k file)."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    paths = [r[0] for r in c.execute("SELECT path FROM files WHERE status='active' ORDER BY random() LIMIT ?", (n,))]
    t = time.perf_counter()
    for p in paths:
        c.execute("SELECT file_id, sha256, size, fs_modified_at, status, extract_error, acl, tier, version_no "
                  "FROM files WHERE path=?", (p,)).fetchone()
    dt = time.perf_counter() - t
    c.close()
    return {"lookups": len(paths), "lookup_s": round(dt, 2), "lookups_per_s": int(len(paths) / dt),
            "projected_770k_s": round(770000 / (len(paths) / dt), 1)}


def bench_hash(db: Path, budget_mb: int):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("SELECT path, size FROM files WHERE status='active' AND size BETWEEN 100000 AND 50000000 "
                     "ORDER BY random() LIMIT 3000").fetchall()
    c.close()
    total = files = 0
    t = time.perf_counter()
    for path, size in rows:
        if total > budget_mb * 2**20:
            break
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            total += size
            files += 1
        except OSError:
            continue
    dt = time.perf_counter() - t
    return {"files": files, "mb": round(total / 2**20), "hash_s": round(dt, 1),
            "mb_per_s": round(total / 2**20 / dt, 1) if dt else None, "note": "có thể bị ảnh hưởng bởi cache đĩa"}


def bench_events(n=2000):
    """Vòng đời event bus trên DB TẠM: emit (gộp pending) -> claim -> finish."""
    tmp = Path(tempfile.mkdtemp(prefix="cb_evbench_"))
    os.environ["BRAIN_DATA_DIR"] = str(tmp)
    import importlib
    from brain import catalog, config
    importlib.reload(config)
    importlib.reload(catalog)
    from brain import watcher
    importlib.reload(watcher)
    conn = catalog.connect()
    t = time.perf_counter()
    for i in range(n):
        watcher.emit("FILE_MODIFIED", f"C:/bench/f{i % (n // 2)}.txt", conn=conn)  # 1/2 là trùng => gộp
    conn.commit()
    t_emit = time.perf_counter() - t
    conn.execute("UPDATE events SET last_at = last_at - 3600")  # bỏ debounce cho đo
    conn.commit()
    t = time.perf_counter()
    claimed = 0
    lat = []
    while True:
        rows = watcher._claim(conn)
        if not rows:
            break
        for ev in rows:
            t0 = time.perf_counter()
            watcher._finish(conn, ev, True)
            lat.append(time.perf_counter() - t0)
        claimed += len(rows)
    t_proc = time.perf_counter() - t
    conn.close()
    return {"emitted": n, "emit_per_s": int(n / t_emit), "claimed_unique": claimed,
            "claim_finish_per_s": int(claimed / t_proc) if t_proc else None,
            "finish_ms_p50": round(statistics.median(lat) * 1000, 2) if lat else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--walk-root", default=str(Path.home()))
    ap.add_argument("--hash-mb", type=int, default=1024)
    a = ap.parse_args()
    db = ROOT / "data" / "catalog.db"
    out = {"at": datetime.now().isoformat(timespec="seconds"), "python": sys.version.split()[0]}
    tracemalloc.start()
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    names = [r[0] for r in c.execute("SELECT name FROM files")]
    out["catalog_files"] = len(names)
    c.close()
    for key, fn in (("norm", lambda: bench_norm(names)), ("unchanged_check", lambda: bench_unchanged_check(db)),
                    ("walk", lambda: bench_walk(Path(a.walk_root))), ("hash", lambda: bench_hash(db, a.hash_mb)),
                    ("events", bench_events)):
        print(f"… {key}", flush=True)
        out[key] = fn()
        print(json.dumps(out[key], ensure_ascii=False), flush=True)
    out["peak_python_mb"] = round(tracemalloc.get_traced_memory()[1] / 2**20)
    d = ROOT / "docs" / "bench"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"baseline-{datetime.now():%Y%m%d-%H%M}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                                     encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
