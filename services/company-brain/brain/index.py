"""CONTENT INDEX: catalog -> đoạn có vị trí (chunks + FTS) -> vector (Qdrant) + phiên bản/khác biệt.
Tìm kiếm LAI: vector + toàn văn (BM25) hợp nhất bằng RRF, lọc quyền ở CẢ HAI nhánh, có xếp hạng lại nhẹ."""
import atexit
import difflib
import gzip
import os
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import requests
from qdrant_client import QdrantClient, models

from . import obs
from .catalog import connect
from .config import (ADMIN_GROUP, FORCE_WORKERS, CHUNK_CHARS, CHUNK_OVERLAP, COLLECTION, DATA_DIR, EMBED_KEEP_ALIVE, EMBED_MODEL,
                     MAX_CHUNKS_PER_FILE, MAX_TEXT_CHARS, OLLAMA_URL, QDRANT_DIR, QDRANT_EXE, QDRANT_MODE,
                     QDRANT_URL)
from .extract import chunk_segments, extract_segments
from .server_acl import acl_sql

_client: QdrantClient | None = None
_lock = threading.RLock()
_qproc: subprocess.Popen | None = None
VERSION_DIR = DATA_DIR / "versions"
RRF_K = 60


# ---------------------------------------------------------------- Qdrant
def _qdrant_alive() -> bool:
    try:
        return requests.get(f"{QDRANT_URL}/healthz", timeout=1).ok
    except requests.RequestException:
        return False


def ensure_qdrant_server() -> bool:
    """Bật Qdrant server cục bộ nếu chưa chạy. True nếu sẵn sàng."""
    global _qproc
    if _qdrant_alive():
        return True
    if not QDRANT_EXE.exists():
        return False
    env = dict(os.environ, QDRANT__SERVICE__HOST="127.0.0.1", QDRANT__TELEMETRY_DISABLED="true",
               QDRANT__STORAGE__STORAGE_PATH=str(QDRANT_EXE.parent / "storage"),
               QDRANT__STORAGE__SNAPSHOTS_PATH=str(QDRANT_EXE.parent / "snapshots"))
    log = open(QDRANT_EXE.parent / "qdrant.log", "a", encoding="utf-8")
    _qproc = subprocess.Popen([str(QDRANT_EXE)], cwd=str(QDRANT_EXE.parent), env=env, stdout=log, stderr=log,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(40):
        time.sleep(0.5)
        if _qdrant_alive():
            return True
    return False


def client() -> QdrantClient:
    global _client
    with _lock:
        if _client is None:
            if QDRANT_MODE == "memory":
                _client = QdrantClient(":memory:")
            elif QDRANT_MODE == "server" and ensure_qdrant_server():
                _client = QdrantClient(url=QDRANT_URL, timeout=60)
            else:
                if QDRANT_MODE == "server":
                    obs.metric("error.qdrant_server_unavailable", 1)
                QDRANT_DIR.mkdir(parents=True, exist_ok=True)
                _client = QdrantClient(path=str(QDRANT_DIR))  # lùi về chế độ nhúng
                atexit.register(_client.close)
        return _client


def _ensure_collection(dim: int):
    c = client()
    if not c.collection_exists(COLLECTION):
        c.create_collection(COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
        for field in ("acl", "tier", "project", "department"):
            try:
                c.create_payload_index(COLLECTION, field, models.PayloadSchemaType.KEYWORD)
            except Exception:
                pass  # chế độ nhúng/memory không hỗ trợ index payload


def _delete_points(ids: list[int]):
    if ids and client().collection_exists(COLLECTION):
        client().delete(COLLECTION, points_selector=models.PointIdsList(points=ids))


def embed(texts: list[str], query: bool = False) -> list[list[float]]:
    if query:  # Qwen3-Embedding khuyến nghị instruction cho truy vấn
        texts = [f"Instruct: Given a question, retrieve company documents that answer it\nQuery: {t}"
                 for t in texts]
    out, t0 = [], time.time()
    for i in range(0, len(texts), 32):
        # num_ctx nhỏ: đoạn văn tối đa ~1.200 ký tự; để mặc định Ollama cấp ngữ cảnh khổng lồ => model 0,6B chiếm ~5,8 GB VRAM
        r = requests.post(f"{OLLAMA_URL}/api/embed", timeout=600,
                          json={"model": EMBED_MODEL, "input": texts[i:i + 32], "keep_alive": EMBED_KEEP_ALIVE,
                                "truncate": True, "options": {"num_ctx": 2048}})
        r.raise_for_status()
        out.extend(r.json()["embeddings"])
    if texts:
        obs.metric("embed_latency_s", (time.time() - t0) / len(texts) * 1000, unit="ms_per_text")
    return out


# ---------------------------------------------------------------- phiên bản + khác biệt
def save_snapshot(sha: str, text: str) -> str | None:
    if not sha or len(text) > 3_000_000:
        return None
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    path = VERSION_DIR / f"{sha}.txt.gz"
    if not path.exists():
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
    return str(path)


def read_snapshot(path: str | None) -> str | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    except (OSError, TypeError):
        return None


def diff_text(old: str, new: str, max_lines: int = 40) -> tuple[int, int, str]:
    """(dòng thêm, dòng bớt, tóm tắt các dòng đổi)."""
    a, b = old.splitlines(), new.splitlines()
    added = removed = 0
    shown: list[str] = []
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        if len(shown) < max_lines:
            shown.append(line[:240])
    return added, removed, "\n".join(shown)


def record_version_content(conn, f, flat_text: str):
    """Lưu snapshot nội dung của phiên bản hiện tại và so với phiên bản liền trước."""
    snap = save_snapshot(f["sha256"], flat_text)
    vno = f["version_no"] or 1
    row = conn.execute("SELECT version_id, snapshot FROM file_versions WHERE file_id=? AND version_no=?",
                       (f["file_id"], vno)).fetchone()
    if row is None:
        conn.execute("""INSERT INTO file_versions (file_id, version_no, sha256, size, fs_modified_at, editor,
                        observed_at) VALUES (?,?,?,?,?,?,?)""",
                     (f["file_id"], vno, f["sha256"], f["size"], f["fs_modified_at"],
                      f["doc_last_editor"] or f["doc_author"] or f["fs_owner"],
                      datetime.now().isoformat(timespec="seconds")))
    prev = conn.execute("""SELECT snapshot FROM file_versions WHERE file_id=? AND version_no<? AND snapshot IS NOT NULL
                           ORDER BY version_no DESC LIMIT 1""", (f["file_id"], vno)).fetchone()
    added = removed = summary = None
    if prev:
        old = read_snapshot(prev["snapshot"])
        if old is not None:
            added, removed, summary = diff_text(old, flat_text)
    conn.execute("""UPDATE file_versions SET snapshot=?, diff_added=?, diff_removed=?, diff_summary=?
                    WHERE file_id=? AND version_no=?""", (snap, added, removed, summary, f["file_id"], vno))


# ---------------------------------------------------------------- lập chỉ mục
def purge_file(conn, file_id: int):
    """Xóa đoạn + vector của một file (idempotent)."""
    ids = [r[0] for r in conn.execute("SELECT chunk_id FROM chunks WHERE file_id=?", (file_id,))]
    _delete_points(ids)
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))


def index_file(conn, f) -> int:
    """Mở file, tách đoạn có vị trí, embed, ghi chunks + vector. Trả số đoạn."""
    segs, _ = extract_segments(Path(f["path"]))
    flat = "\n\n".join(f"[{loc}]\n{t}" for loc, t in segs)[:MAX_TEXT_CHARS]
    record_version_content(conn, f, flat)
    pieces = chunk_segments([(loc, t[:MAX_TEXT_CHARS]) for loc, t in segs], CHUNK_CHARS, CHUNK_OVERLAP,
                            MAX_CHUNKS_PER_FILE)
    header = f"[{f['name']}] {f['title'] or ''}".strip()
    vecs = embed([f"{header} · {loc}\n{t}" for loc, t in pieces]) if pieces else []  # chậm: chưa giữ khóa DB
    conn.commit()
    purge_file(conn, f["file_id"])
    points = []
    for i, ((loc, text), vec) in enumerate(zip(pieces, vecs)):
        cid = conn.execute("INSERT INTO chunks (file_id, idx, loc, text, file_sha256) VALUES (?,?,?,?,?)",
                           (f["file_id"], i, loc, text, f["sha256"])).lastrowid
        points.append(models.PointStruct(id=cid, vector=vec, payload={
            "file_id": f["file_id"], "acl": (f["acl"] or "private").split(","), "tier": f["tier"],
            "project": f["project"], "department": f["department"]}))
    conn.commit()
    if points:
        _ensure_collection(len(vecs[0]))
        client().upsert(COLLECTION, points)
    return len(points)


def _migrate_store(conn):
    """Lần đầu dùng kho vector mới: các file từng 'đã index' ở kho cũ phải index lại."""
    if conn.execute("SELECT value FROM settings WHERE key='vector_store'").fetchone() is None:
        conn.execute("UPDATE files SET indexed_sha256=NULL")
        conn.execute("INSERT INTO settings (key, value) VALUES ('vector_store','v2-chunks')")
        conn.commit()


def index_all(progress=None, throttle=None, file_ids: list[int] | None = None) -> dict:
    conn = connect()
    stats = {"indexed": 0, "chunks": 0, "removed": 0, "empty": 0, "errors": 0}
    _migrate_store(conn)

    # File đã xóa hoặc thuộc bậc 3 (thư viện): gỡ khỏi chỉ mục
    for r in conn.execute("""SELECT file_id FROM files WHERE indexed_sha256 IS NOT NULL
                             AND (status='deleted' OR tier=3)""").fetchall():
        purge_file(conn, r["file_id"])
        conn.execute("UPDATE files SET indexed_sha256=NULL WHERE file_id=?", (r["file_id"],))
        stats["removed"] += 1
    conn.commit()

    sql = """SELECT * FROM files WHERE status='active' AND extract_error IS NULL AND tier IN (1,2)
             AND (indexed_sha256 IS NULL OR indexed_sha256 != sha256)"""
    if file_ids is not None:
        sql += f" AND file_id IN ({','.join(str(int(i)) for i in file_ids) or 'NULL'})"
    # Ưu tiên: tài liệu chữ + code trước (rẻ, giá trị cao), rồi audio/video, ảnh sau cùng; file mới sửa trước
    # (việc rẻ — chỉ embedding — xong hết trước; ảnh/video cần model thị giác nên làm sau cùng, bất kể bậc)
    sql += """ ORDER BY CASE WHEN ext IN ('.jpg','.jpeg','.png','.webp','.bmp','.gif','.tif','.tiff') THEN 2
                            WHEN ext IN ('.mp3','.wav','.m4a','.ogg','.flac','.aac','.wma','.opus',
                                         '.mp4','.mov','.mkv','.avi','.webm','.wmv','.m4v') THEN 1 ELSE 0 END,
                       tier, fs_modified_at DESC"""
    todo = conn.execute(sql).fetchall()

    def one(c, f) -> int | None:
        """Xử lý một file bằng kết nối c. Trả số đoạn, hoặc None nếu lỗi (đã ghi nhận)."""
        t0 = time.time()
        try:
            k = index_file(c, f)
        except Exception as e:
            c.rollback()
            c.execute("UPDATE files SET extract_error=? WHERE file_id=?", (f"{type(e).__name__}: {e}", f["file_id"]))
            c.commit()
            obs.metric("error.index_file", 1)
            return None
        c.execute("UPDATE files SET indexed_sha256=? WHERE file_id=?", (f["sha256"], f["file_id"]))
        c.commit()
        obs.metric("index_file_s", time.time() - t0)
        return k

    def tally(k):
        if k is None:
            stats["errors"] += 1
        else:
            stats["chunks" if k else "empty"] += k or 1
            stats["indexed"] += 1

    def own_conn(f):  # luồng phụ: kết nối riêng (SQLite không dùng chung kết nối giữa luồng)
        c = connect()
        try:
            return one(c, f)
        finally:
            c.close()

    from . import governor
    pool = ThreadPoolExecutor(max_workers=FORCE_WORKERS, thread_name_prefix="index")
    inflight: set = set()
    try:
        for n, f in enumerate(todo, 1):
            if throttle:
                throttle()
            if progress:
                progress(f"Index {n}/{len(todo)}: {f['name']}", f["path"])
            if governor.is_force():  # 'Học ngay': nhiều file song song để GPU/CPU không bao giờ rỗi
                inflight.add(pool.submit(own_conn, f))
                while len(inflight) >= FORCE_WORKERS:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for fu in done:
                        tally(fu.result())
            else:
                for fu in inflight:  # vừa thôi cưỡng chế: chờ các file đang dở xong rồi chạy tuần tự
                    tally(fu.result())
                inflight = set()
                tally(one(conn, f))
        for fu in inflight:
            tally(fu.result())
    finally:
        pool.shutdown(wait=True)
        conn.close()
    return stats


# ---------------------------------------------------------------- tìm kiếm lai
_FTS_STOP = {"la", "va", "cua", "co", "khong", "nao", "gi", "the", "nay", "do", "trong", "voi", "cho", "một", "những",
             "các", "và", "của", "là", "có", "không", "như", "thế", "nào", "bao", "nhiêu", "ai", "đã", "được"}


def fts_query(text: str) -> str | None:
    toks = [t for t in re.findall(r"\w+", text, re.UNICODE) if len(t) >= 2 and t.lower() not in _FTS_STOP]
    toks = list(dict.fromkeys(toks))[:12]
    return " OR ".join(f'"{t}"' for t in toks) if toks else None


def search(query: str, groups: list[str], limit: int = 8, department: str | None = None,
           project: str | None = None, ext: str | None = None, per_file: int = 2) -> list[dict]:
    """Tìm lai vector + BM25 -> RRF -> xếp hạng lại nhẹ. LUÔN lọc quyền ở cả hai nhánh."""
    admin = ADMIN_GROUP in groups
    t0 = time.time()
    ranks: dict[int, dict] = {}

    # nhánh 1: vector
    try:
        must = []
        if not admin:
            must.append(models.FieldCondition(key="acl", match=models.MatchAny(any=list({*groups, "all"}))))
        for key, val in (("department", department), ("project", project)):
            if val:
                must.append(models.FieldCondition(key=key, match=models.MatchValue(value=val)))
        c = client()
        if c.collection_exists(COLLECTION):
            vec = embed([query], query=True)[0]
            res = c.query_points(COLLECTION, query=vec, limit=30, with_payload=False,
                                 query_filter=models.Filter(must=must) if must else None)
            for rank, p in enumerate(res.points, 1):
                ranks.setdefault(int(p.id), {})["vec"] = rank
    except Exception:
        obs.metric("error.search_vector", 1)

    # nhánh 2: toàn văn BM25 (lọc quyền ngay trong SQL)
    conn = connect(readonly=True)
    q = fts_query(query)
    if q:
        cond, args = acl_sql(groups, "f.acl")
        extra, eargs = "", []
        if department:
            extra += " AND f.department=?"
            eargs.append(department)
        if project:
            extra += " AND f.project=?"
            eargs.append(project)
        try:
            for rank, r in enumerate(conn.execute(f"""SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.rowid
                    JOIN files f ON f.file_id=c.file_id WHERE chunks_fts MATCH ? AND f.status='active' AND f.tier IN (1,2)
                    AND {cond}{extra} ORDER BY bm25(chunks_fts) LIMIT 30""", [q, *args, *eargs]), 1):
                ranks.setdefault(r[0], {})["fts"] = rank
        except Exception:
            obs.metric("error.search_fts", 1)

    if not ranks:
        conn.close()
        return []
    ids = list(ranks)
    rows = {r["chunk_id"]: r for r in conn.execute(f"""SELECT c.chunk_id, c.loc, c.text, f.file_id, f.name, f.path, f.acl,
            f.doc_author, f.fs_owner, f.fs_modified_at, f.ext, f.project FROM chunks c JOIN files f ON f.file_id=c.file_id
            WHERE c.chunk_id IN ({','.join('?' * len(ids))}) AND f.status='active'""", ids)}
    conn.close()
    kws = {k.lower() for k in re.findall(r"\w+", query, re.UNICODE) if len(k) >= 3}
    scored = []
    for cid, why in ranks.items():
        r = rows.get(cid)
        if not r:
            continue
        s = sum(1 / (RRF_K + v) for v in why.values())
        if ext and r["ext"] != ext:
            continue
        # xếp hạng lại nhẹ: thưởng khi từ khóa xuất hiện trong tên file / vị trí; thưởng khi cả hai nhánh cùng chọn
        hay = f"{r['name']} {r['loc']}".lower()
        s *= 1 + 0.15 * sum(k in hay for k in kws) + (0.2 if len(why) == 2 else 0)
        scored.append((s, cid, why))
    scored.sort(key=lambda x: -x[0])
    out, per = [], {}
    for s, cid, why in scored:
        r = rows[cid]
        if per.get(r["file_id"], 0) >= per_file:  # đa dạng nguồn: tối đa vài đoạn mỗi file
            continue
        per[r["file_id"]] = per.get(r["file_id"], 0) + 1
        out.append({"score": round(s * 1000, 1), "chunk_id": cid, "file_id": r["file_id"], "name": r["name"],
                    "path": r["path"], "loc": r["loc"], "text": r["text"], "author": r["doc_author"] or r["fs_owner"],
                    "modified_at": r["fs_modified_at"], "project": r["project"],
                    "why": ("vector#%s" % why["vec"] if "vec" in why else "") +
                           (" + " if len(why) == 2 else "") + ("từ khóa#%s" % why["fts"] if "fts" in why else "")})
        if len(out) >= limit:
            break
    obs.metric("search_latency_s", time.time() - t0)
    return out
