"""Danh tính: gom các cách ghi tên khác nhau (NTFS 'TT\\TIEN THANH', author trong Office, tác giả Git, email...)
về một người. Không bao giờ tự đoán gộp hai người khác tên; gộp thủ công bằng merge()."""
import re
import sqlite3
from datetime import datetime

from .vn import plain

# Tài khoản hệ thống / phần mềm: không phải con người => không tính đóng góp
SYSTEM_ACCOUNTS = {"system", "nt authority\\system", "trustedinstaller", "administrators", "builtin\\administrators",
                   "users", "everyone", "authenticated users", "nt service\\trustedinstaller", "unknown",
                   "microsoft", "microsoft corporation", "admin", "administrator", "user", "owner", "author",
                   "(anonymous)", "anonymous", "openpyxl", "python-docx", "python-pptx", "pypdf", "root"}


def clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.lower().startswith("admin ("):  # phiên cục bộ trên chính máy chủ = người đang ngồi máy (tài khoản Windows)
        import os
        s = os.environ.get("USERNAME", "") or "admin"
    if s.lower() in SYSTEM_ACCOUNTS:
        return None
    if "\\" in s:  # DOMAIN\user  =>  user
        s = s.rsplit("\\", 1)[1]
    if "@" in s and " " not in s:  # email => phần trước @
        s = s.split("@", 1)[0]
    s = re.sub(r"[._]+", " ", s).strip()
    if not s or s.lower() in SYSTEM_ACCOUNTS or len(s) < 2:
        return None
    return s


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", plain(name)).strip()


def resolve(conn: sqlite3.Connection, raw: str | None) -> int | None:
    """person_id cho một cách ghi tên (tạo mới nếu chưa có). None nếu là tài khoản hệ thống."""
    name = clean_name(raw)
    if not name:
        return None
    key = name_key(name)
    r = conn.execute("SELECT person_id FROM person_aliases WHERE alias_key=?", (key,)).fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT person_id FROM people WHERE key=?", (key,)).fetchone()
    if r:
        pid = r[0]
    else:
        pid = conn.execute("INSERT INTO people (name, key, created_at) VALUES (?,?,?)",
                           (name, key, datetime.now().isoformat(timespec="seconds"))).lastrowid
    conn.execute("INSERT OR IGNORE INTO person_aliases (alias_key, person_id) VALUES (?,?)", (key, pid))
    return pid


def add_alias(conn: sqlite3.Connection, person_id: int, alias: str):
    conn.execute("INSERT OR REPLACE INTO person_aliases (alias_key, person_id) VALUES (?,?)",
                 (name_key(clean_name(alias) or alias), person_id))


def merge(conn: sqlite3.Connection, keep_id: int, drop_id: int):
    """Gộp hai hồ sơ là cùng một người (quyết định của con người). Mọi đóng góp và alias chuyển sang keep_id."""
    if keep_id == drop_id:
        return
    conn.execute("UPDATE contributions SET person_id=? WHERE person_id=?", (keep_id, drop_id))
    conn.execute("UPDATE person_aliases SET person_id=? WHERE person_id=?", (keep_id, drop_id))
    d = conn.execute("SELECT key FROM people WHERE person_id=?", (drop_id,)).fetchone()
    if d:
        conn.execute("INSERT OR REPLACE INTO person_aliases (alias_key, person_id) VALUES (?,?)", (d[0], keep_id))
    conn.execute("DELETE FROM people WHERE person_id=?", (drop_id,))
    conn.execute("INSERT INTO audit (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), "admin", "person_merge", str(keep_id),
                  f"gộp #{drop_id} vào #{keep_id}"))
