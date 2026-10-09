"""Parity duyệt thư mục: brain_core.walk == os.walk + bộ lọc của scanner.scan (CHỈ ĐỌC).

    python scripts/walk_parity.py [root]"""
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import brain_core  # noqa: E402

from brain import scanner  # noqa: E402
from brain.config import SUPPORTED_EXT  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home()).resolve()

t_start = time.time()
t = time.perf_counter()
py_files, py_repos = {}, set()
for dirpath, dirnames, filenames in os.walk(root):
    if ".git" in dirnames:
        py_repos.add(dirpath)
        dirnames.remove(".git")
    dirnames[:] = [d for d in dirnames if not scanner._dir_excluded(d, os.path.join(dirpath, d))]
    for fn in filenames:
        p = Path(dirpath) / fn
        if p.suffix.lower() not in SUPPORTED_EXT or fn.startswith("~$"):
            continue
        try:
            st = p.stat()
            py_files[str(p)] = (st.st_size, scanner._iso(st.st_mtime))
        except OSError:
            py_files[str(p)] = (-1, None)
tp = time.perf_counter() - t

t = time.perf_counter()
entries, repos = brain_core.walk(str(root), *scanner.rust_policy(), threads=8)
tr = time.perf_counter() - t
rs_files = {p: (s, scanner._iso(m) if s >= 0 and not math.isnan(m) else None) for p, s, m in entries}

def churn(p):  # file sinh ra / bị xóa / bị sửa TRONG LÚC đo (app khác ghi tạm) — không phải lỗi duyệt
    try:
        return os.stat(p).st_mtime >= t_start - 1
    except OSError:
        return True


only_py = {p for p in py_files.keys() - rs_files.keys() if not churn(p)}
only_rs = {p for p in rs_files.keys() - py_files.keys() if not churn(p)}
diff = [p for p in py_files.keys() & rs_files.keys() if py_files[p] != rs_files[p] and not churn(p)]
print(f"root {root}")
print(f"Python os.walk: {len(py_files):,} file trong {tp:.1f}s · Rust: {len(rs_files):,} file trong {tr:.1f}s ({tp / tr:.1f}x)")
print(f"chỉ Python thấy: {len(only_py)} · chỉ Rust thấy: {len(only_rs)} · khác size/mtime: {len(diff)}")
for p in list(only_py)[:8]:
    print("  py-only:", p)
for p in list(only_rs)[:8]:
    print("  rs-only:", p)
for p in diff[:8]:
    print("  diff:", p, py_files[p], rs_files[p])
print(f"repo .git: py {len(py_repos)} · rs {len(repos)} · khác {len(py_repos ^ set(repos))}")
ok = not only_py and not only_rs and not diff and py_repos == set(repos)
print("PARITY:", "ĐẠT" if ok else "KHÔNG ĐẠT")
sys.exit(0 if ok else 1)
