"""Kiểm tra parity Rust (brain_core) == Python trên DỮ LIỆU THẬT (chỉ đọc). Bắt buộc 0 khác biệt trước khi bật cờ USE_RUST_*.

    python scripts/parity_check.py"""
import hashlib
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import brain_core  # noqa: E402

from brain.facts import _py_norm as py_norm  # noqa: E402
from brain.vn import _py_plain as py_plain, _syllable_variants  # noqa: E402

db = sqlite3.connect(f"file:{ROOT / 'data' / 'catalog.db'}?mode=ro", uri=True)
corpus = [r[0] for r in db.execute("SELECT name FROM files")]
for sql in ("SELECT ifnull(title,'') FROM files WHERE title IS NOT NULL", "SELECT path FROM files LIMIT 200000",
            "SELECT subject FROM facts", "SELECT value FROM facts", "SELECT trigger FROM memories",
            "SELECT content FROM memories", "SELECT name FROM entities", "SELECT name FROM people"):
    try:
        corpus += [r[0] for r in db.execute(sql) if r[0] is not None]
    except sqlite3.Error:
        pass
# chuỗi khó: Unicode đặc biệt
corpus += ["İstanbul", "ΣΊΣΥΦΟΣ", "ß straße", "Ａｂｃ１２３", "ﬁle", "①②", "٣٤", "Ǆ", "ẞ", "x̣́", "Ð", "đĐ",
           "Ƀ", "ⅷ", "㎏", " nbsp", "tab\tnew\nline", "", "   ", "🎉 emoji", "Ω", "K (kelvin)", "ﬀ"]
print("chuỗi kiểm tra:", len(corpus))

bad = 0
t = time.perf_counter()
py = [py_norm(s) for s in corpus]
tp = time.perf_counter() - t
t = time.perf_counter()
rs = brain_core.norm_many(corpus)
tr = time.perf_counter() - t
for s, a, b in zip(corpus, py, rs):
    if a != b:
        bad += 1
        if bad <= 15:
            print("  KHÁC norm:", repr(s[:80]), "| py:", repr(a[:60]), "| rs:", repr(b[:60]))
print(f"norm: {bad} khác / {len(corpus)} · Python {tp:.2f}s · Rust {tr:.2f}s ({tp / tr:.1f}x)")

bad_p = sum(py_plain(s) != brain_core.plain(s) for s in corpus)
print(f"plain: {bad_p} khác")

# name_key đã lưu phải khớp norm Rust
rows = db.execute("SELECT name, title, name_key FROM files WHERE name_key IS NOT NULL").fetchall()
bad_k = sum(brain_core.norm(n + " " + (ti or "")) != k for n, ti, k in rows)
print(f"name_key đã lưu: {bad_k} khác / {len(rows)}")

# telex: so tập biến thể từng âm tiết (không phụ thuộc thứ tự set)
words = sorted({w for s in corpus[:200000] for w in s.split()[:4] if 0 < len(w) < 12})[:60000] + ["ửod", "ẽcel", "ưỏd"]
bad_t = 0
for w in words:
    if set(_syllable_variants(w)) != set(brain_core.syllable_variants(w)):
        bad_t += 1
        if bad_t <= 5:
            print("  KHÁC telex:", repr(w))
print(f"telex (âm tiết): {bad_t} khác / {len(words)}")

# hash: mẫu file thật
paths = [r[0] for r in db.execute("SELECT path FROM files WHERE status='active' AND size < 20000000 ORDER BY random() "
                                  "LIMIT 300")]
bad_h = n_h = 0
for p in paths:
    try:
        h = hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        continue
    n_h += 1
    bad_h += brain_core.hash_file(p) != h
print(f"sha256: {bad_h} khác / {n_h} file")
ok = bad == bad_p == bad_k == bad_t == bad_h == 0
print("PARITY:", "ĐẠT" if ok else "KHÔNG ĐẠT")
sys.exit(0 if ok else 1)
