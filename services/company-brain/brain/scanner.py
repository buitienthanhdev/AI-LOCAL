"""SCAN: quét thư mục -> catalog + phiên bản + sổ sự kiện/đóng góp. Không cần LLM.
process_file() dùng chung cho lần quét định kỳ (lưới an toàn) và worker sự kiện realtime."""
import hashlib
import os
import time
from datetime import datetime
from pathlib import Path

from . import contrib
from .catalog import connect
from .config import (ACL_RULES, CODE_EXT, DATA_DIR, EXCLUDE_DIRS, EXCLUDE_PATHS, LIB_MARKERS, MAX_CODE_KB,
                     MAX_FILE_MB, MAX_MEDIA_MB, MIN_IMAGE_KB, SHARED_ROOTS, SUPPORTED_EXT)
from .extract import extract

_DATA_DIR = str(DATA_DIR).lower()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _ntfs_owner(path: Path) -> str | None:
    try:
        import win32security
        sd = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
        name, domain, _ = win32security.LookupAccountSid(None, sd.GetSecurityDescriptorOwner())
        return f"{domain}\\{name}" if domain else name
    except Exception:
        return None


def compute_tier(path: str, ext: str, size: int) -> int:
    """1: tài liệu/media người dùng · 2: mã nguồn người dùng · 3: thư viện/phần mềm (chỉ lập danh mục)."""
    low = path.lower().replace("\\", "/")
    if any(m in low for m in LIB_MARKERS):
        return 3
    if ext in CODE_EXT:
        return 2 if size <= MAX_CODE_KB * 1024 else 3
    return 1


def _acl_for(path: Path) -> str:
    """Chỉ file nằm trong SHARED_ROOTS mới được chia sẻ; mọi file khác của máy là 'private' (chỉ admin)."""
    for shared in SHARED_ROOTS:
        try:
            rel = path.relative_to(Path(shared).resolve()).parts[:-1]
        except ValueError:
            continue
        if rel and rel[0] in ACL_RULES:
            return ",".join(ACL_RULES[rel[0]])
        return "all"
    return "private"


def _rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    """Thư mục cha tương đối (dùng suy phòng ban/dự án): ưu tiên tính từ thư mục chia sẻ."""
    for shared in SHARED_ROOTS:
        try:
            return path.relative_to(Path(shared).resolve()).parts[:-1]
        except ValueError:
            pass
    try:
        return path.relative_to(root).parts[:-1]
    except ValueError:
        return path.parts[1:-1]


def size_ok(p: Path, size: int) -> bool:
    from .media import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
    ext = p.suffix.lower()
    if ext in AUDIO_EXT or ext in VIDEO_EXT:
        return size <= MAX_MEDIA_MB * 1024 * 1024
    if ext in IMAGE_EXT:
        return MIN_IMAGE_KB * 1024 <= size <= MAX_FILE_MB * 1024 * 1024
    return size <= MAX_FILE_MB * 1024 * 1024


def _dir_excluded(name: str, full: str) -> bool:
    low = full.lower()
    return name.lower() in EXCLUDE_DIRS or low == _DATA_DIR or low in EXCLUDE_PATHS


def rust_policy() -> tuple[list[str], list[str], list[str]]:
    """Bộ lọc của scan() truyền xuống brain_core.walk: (đuôi file, tên thư mục loại, đường dẫn thư mục loại) — đều
    chữ thường, cùng nguồn config với bản Python để không lệch."""
    return sorted(SUPPORTED_EXT), sorted(d.lower() for d in EXCLUDE_DIRS), sorted({_DATA_DIR, *EXCLUDE_PATHS})


def is_candidate(path: Path) -> bool:
    """Watcher dùng: file có thuộc phạm vi theo dõi không (đuôi hỗ trợ, không nằm trong thư mục bị loại)."""
    if path.suffix.lower() not in SUPPORTED_EXT or path.name.startswith("~$"):
        return False
    cur = path.anchor
    for part in path.parts[1:-1]:
        cur = os.path.join(cur, part)
        if _dir_excluded(part, cur):
            return False
    return True


def process_file(conn, p: Path, root: Path, now: str, stats: dict, moved_from: str | None = None,
                 pre_hash: tuple[int, float, str] | None = None) -> int | None:
    """Đưa 1 file vào catalog. Trả về file_id nếu file mới/đổi (cần index), None nếu không đổi/bỏ qua.
    pre_hash = (size, mtime, sha256) đã băm sẵn (Rust, song song) — chỉ dùng nếu stat hiện tại vẫn khớp."""
    try:
        st = p.stat()
        if not size_ok(p, st.st_size):
            stats["skipped"] += 1
            return None
        key = str(p)
        acl = _acl_for(p)
        ext = p.suffix.lower()
        tier = compute_tier(key, ext, st.st_size)
        row = conn.execute("SELECT file_id, sha256, size, fs_modified_at, status, extract_error, acl, tier, "
                           "version_no FROM files WHERE path=?", (key,)).fetchone()
        healthy = row is not None and row["status"] == "active" and row["extract_error"] is None
        # Kích thước + mtime không đổi => coi như không đổi, khỏi đọc lại file (và KHÔNG ghi gì xuống DB)
        if healthy and row["size"] == st.st_size and row["fs_modified_at"] == _iso(st.st_mtime):
            if row["acl"] != acl or row["tier"] != tier:  # quyền/bậc đổi => cập nhật + buộc index lại payload
                conn.execute("UPDATE files SET acl=?, tier=?, indexed_sha256=NULL WHERE file_id=?",
                             (acl, tier, row["file_id"]))
            stats["unchanged"] += 1
            return None
        if pre_hash and pre_hash[2] and pre_hash[0] == st.st_size and abs(pre_hash[1] - st.st_mtime) < 1e-5:
            sha = pre_hash[2]
        else:
            if conn.in_transaction:  # sắp băm/đọc file (có thể lâu) => nhả khóa ghi trước
                conn.commit()
            sha = _sha256(p)
    except OSError:  # file bị khóa / đang ghi / không có quyền
        stats["skipped"] += 1
        return None

    if healthy and row["sha256"] == sha:  # chỉ mtime đổi (vd. bấm Save không sửa gì)
        conn.execute("UPDATE files SET fs_modified_at=?, acl=?, tier=? WHERE file_id=?",
                     (_iso(st.st_mtime), acl, tier, row["file_id"]))
        stats["unchanged"] += 1
        return None

    meta, err = {}, None
    if tier <= 2:  # file thư viện không mở ra lấy metadata (tốn mà vô ích)
        try:
            _, meta = extract(p, light=True)
        except Exception as e:  # file hỏng/mã hóa vẫn được ghi vào catalog
            err = f"{type(e).__name__}: {e}"
            stats["errors"] += 1

    rel = _rel_parts(p, root)
    rec = {
        "path": key, "name": p.name, "ext": ext, "size": st.st_size, "sha256": sha,
        "fs_created_at": _iso(st.st_ctime), "fs_modified_at": _iso(st.st_mtime),
        "fs_owner": _ntfs_owner(p),
        "doc_author": meta.get("doc_author"), "doc_last_editor": meta.get("doc_last_editor"),
        "doc_created_at": meta.get("doc_created_at"), "title": meta.get("title"),
        "department": rel[0] if len(rel) > 0 else None,
        "project": rel[1] if len(rel) > 1 else None,
        "acl": acl, "tier": tier, "status": "active", "last_seen_at": now, "extract_error": err,
    }
    actor = rec["doc_last_editor"] or rec["doc_author"] or rec["fs_owner"]

    if row is None:
        rec["first_seen_at"] = now
        rec["version_no"] = 1
        cur = conn.execute(f"INSERT INTO files ({','.join(rec)}) VALUES ({','.join('?' * len(rec))})",
                           tuple(rec.values()))
        fid, version_no = cur.lastrowid, 1
        event, detail = ("moved", f"from={moved_from}") if moved_from else ("discovered", None)
        stats["new"] += 1
    else:
        fid = row["file_id"]
        restored = row["status"] == "deleted"
        version_no = (row["version_no"] or 1) + (0 if restored and row["sha256"] == sha else 1)
        rec["version_no"] = version_no
        event, detail = ("restored" if restored else "modified"), None
        conn.execute(f"UPDATE files SET {','.join(f'{k}=?' for k in rec)} WHERE file_id=?", (*rec.values(), fid))
        stats["modified"] += 1
    conn.execute("INSERT INTO file_events (file_id, event, actor, at, detail) VALUES (?,?,?,?,?)",
                 (fid, event, actor, rec["fs_modified_at"], detail or f"sha256={sha[:12]}"))
    conn.execute("""INSERT OR IGNORE INTO file_versions (file_id, version_no, sha256, size, fs_modified_at, editor,
                    observed_at) VALUES (?,?,?,?,?,?,?)""",
                 (fid, version_no, sha, st.st_size, rec["fs_modified_at"], actor, now))
    f = conn.execute("SELECT * FROM files WHERE file_id=?", (fid,)).fetchone()
    if event in ("discovered", "moved"):
        creator = rec["doc_author"] or rec["fs_owner"]
        contrib.record_file_event(conn, f, "created", creator, rec["doc_created_at"] or rec["fs_created_at"], 1,
                                  declared=bool(rec["doc_author"]))
    elif event == "modified":
        contrib.record_file_event(conn, f, "modified", actor, rec["fs_modified_at"], version_no,
                                  declared=bool(rec["doc_last_editor"] or rec["doc_author"]))
    return fid


def mark_deleted(conn, path: str, now: str, detail: str | None = None) -> bool:
    """Đánh dấu deleted (không xóa record: giữ provenance)."""
    r = conn.execute("SELECT file_id FROM files WHERE path=? AND status='active'", (path,)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE files SET status='deleted' WHERE file_id=?", (r["file_id"],))
    conn.execute("INSERT INTO file_events (file_id, event, at, detail) VALUES (?, 'deleted', ?, ?)",
                 (r["file_id"], now, detail))
    return True


def backfill_tiers(conn, progress=print) -> int:
    """Gán bậc cho các dòng cũ chưa có (một lần)."""
    rows = conn.execute("SELECT file_id, path, ext, size FROM files WHERE tier IS NULL").fetchall()
    for i in range(0, len(rows), 20000):
        conn.executemany("UPDATE files SET tier=? WHERE file_id=?",
                         [(compute_tier(r["path"], r["ext"] or "", r["size"] or 0), r["file_id"])
                          for r in rows[i:i + 20000]])
        conn.commit()
        progress(f"Gán bậc học: {min(i + 20000, len(rows)):,}/{len(rows):,}")
    return len(rows)


def _register_repo(conn, repo_dir: str):
    conn.execute("INSERT OR IGNORE INTO repos (path) VALUES (?)", (repo_dir,))


def _rust():
    from .config import USE_RUST_SCANNER
    if not USE_RUST_SCANNER:
        return None
    try:
        import brain_core
        return brain_core if hasattr(brain_core, "walk") else None
    except ImportError:
        return None


def _suffix(path: str) -> str:
    """pathlib.Path.suffix (chữ thường) không cần tạo Path — dùng cho hàng trăm nghìn file."""
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    i = name.rfind(".")
    return name[i:].lower() if 0 < i < len(name) - 1 else ""


def _acl_fast(path: str, shared_low: list[str]) -> str:
    """Như _acl_for nhưng bỏ qua tạo Path khi file chắc chắn không nằm trong thư mục chia sẻ (đa số file).
    shared_low tính lại MỖI lần quét (SHARED_ROOTS có thể đổi) — không cache toàn cục: ACL là bất biến tuyệt đối."""
    low = path.lower()
    if not any(low.startswith(s) for s in shared_low):
        return "private"
    return _acl_for(Path(path))


def scan(root: Path, progress=print) -> dict:
    """Quét một thư mục gốc (đệ quy). Lưới an toàn cho những gì watcher bỏ sót.
    USE_RUST_SCANNER=1 (mặc định khi brain_core có sẵn): duyệt + stat bằng Rust song song, nạp catalog một lần thay vì
    một SELECT mỗi file, băm song song; quyết định & ghi DB vẫn y hệt bản Python (process_file)."""
    bc = _rust()
    if bc is not None:
        return _scan_rust(bc, root.resolve(), progress)
    return _scan_py(root, progress)


def _scan_rust(bc, root: Path, progress) -> dict:
    from .config import RUST_SCAN_THREADS
    conn = connect()
    now = _now()
    stats = {"new": 0, "modified": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": 0}
    progress(f"Quét {root} (Rust) ...")
    entries, repos = bc.walk(str(root), *rust_policy(), threads=RUST_SCAN_THREADS)
    for r in repos:
        _register_repo(conn, r)
    prefix = os.path.join(str(root), "")
    known = {r[0]: r[1:] for r in conn.execute(
        "SELECT path, file_id, size, fs_modified_at, status, extract_error, acl, tier FROM files WHERE path LIKE ?",
        (prefix + "%",))}
    from .media import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
    max_b, media_b, min_img = MAX_FILE_MB * 1048576, MAX_MEDIA_MB * 1048576, MIN_IMAGE_KB * 1024
    shared_low = [os.path.join(str(Path(s).resolve()), "").lower() for s in SHARED_ROOTS]
    seen: set[str] = set()
    todo: list[tuple[str, int, float]] = []
    last_commit = time.monotonic()
    for path, size, mtime in entries:
        seen.add(path)
        if size < 0:  # stat lỗi (như process_file: OSError => skipped)
            stats["skipped"] += 1
            continue
        ext = _suffix(path)
        lim_ok = (size <= media_b) if (ext in AUDIO_EXT or ext in VIDEO_EXT) else \
            (min_img <= size <= max_b) if ext in IMAGE_EXT else size <= max_b  # = size_ok
        if not lim_ok:
            stats["skipped"] += 1
            continue
        row = known.get(path)
        if row and row[3] == "active" and row[4] is None and row[1] == size and row[2] == _iso(mtime):
            acl, tier = _acl_fast(path, shared_low), compute_tier(path, ext, size)
            if row[5] != acl or row[6] != tier:  # như process_file: quyền/bậc đổi => cập nhật + index lại
                conn.execute("UPDATE files SET acl=?, tier=?, indexed_sha256=NULL WHERE file_id=?", (acl, tier, row[0]))
            stats["unchanged"] += 1
            if time.monotonic() - last_commit > 0.5:
                conn.commit()
                last_commit = time.monotonic()
            continue
        todo.append((path, size, mtime))
    known.clear()
    conn.commit()
    done = 0
    for i in range(0, len(todo), 256):  # băm song song theo lô (không giữ khóa DB trong lúc băm)
        chunk = todo[i:i + 256]
        shas = bc.hash_files([c[0] for c in chunk], RUST_SCAN_THREADS)
        for (path, size, mtime), sha in zip(chunk, shas):
            if process_file(conn, Path(path), root, now, stats, pre_hash=(size, mtime, sha) if sha else None):
                done += 1
                if done % 200 == 0:
                    progress(f"Quét: {done} file mới/đổi — {os.path.dirname(path)}")
            if time.monotonic() - last_commit > 0.5:
                conn.commit()
                last_commit = time.monotonic()
    for r in conn.execute("SELECT path FROM files WHERE status='active' AND path LIKE ?", (prefix + "%",)).fetchall():
        if r["path"] not in seen and mark_deleted(conn, r["path"], now):
            stats["deleted"] += 1
    conn.commit()
    conn.close()
    return stats


def _scan_py(root: Path, progress=print) -> dict:
    """Bản Python gốc (rollback: USE_RUST_SCANNER=0)."""
    root = root.resolve()
    conn = connect()
    now = _now()
    stats = {"new": 0, "modified": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": 0}
    seen: set[str] = set()
    last_commit = time.monotonic()
    done = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:  # kho Git: ghi nhận để đồng bộ commit, không đi vào .git
            _register_repo(conn, dirpath)
            dirnames.remove(".git")
        dirnames[:] = [d for d in dirnames if not _dir_excluded(d, os.path.join(dirpath, d))]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in SUPPORTED_EXT or fn.startswith("~$"):
                continue
            seen.add(str(p))
            if process_file(conn, p, root, now, stats):
                done += 1
                if done % 200 == 0:
                    progress(f"Quét: {done} file mới/đổi — {dirpath}")
            if time.monotonic() - last_commit > 0.5:
                conn.commit()  # giao dịch ngắn (<0,5 s) => watcher/web không bị "database is locked"
                last_commit = time.monotonic()

    prefix = os.path.join(str(root), "")  # "C:\" giữ nguyên, "D:\x" -> "D:\x\"
    for r in conn.execute("SELECT path FROM files WHERE status='active' AND path LIKE ?",
                          (prefix + "%",)).fetchall():
        if r["path"] not in seen and mark_deleted(conn, r["path"], now):
            stats["deleted"] += 1

    conn.commit()
    conn.close()
    return stats


def fixed_drives() -> list[Path]:
    import win32api
    import win32file
    drives = win32api.GetLogicalDriveStrings().split("\x00")
    return [Path(d) for d in drives if d and win32file.GetDriveType(d) == win32file.DRIVE_FIXED]
