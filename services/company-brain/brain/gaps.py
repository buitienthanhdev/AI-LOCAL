"""Khám phá tri thức tự động: phát hiện lỗ hổng/vấn đề trong kho tri thức và tự tạo việc nghiên cứu.

Mọi phát hiện đều có BẰNG CHỨNG và `fingerprint` ổn định (chạy lại không sinh trùng). Gap không còn đúng nữa thì tự
đóng (resolved). AI chỉ ĐỀ XUẤT: việc nghiên cứu thu thập bằng chứng nội bộ để người duyệt, không tự phong 'authoritative'."""
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta

from . import people
from .server_acl import ADMIN_GROUP

MIN_INDEX_COVERAGE = 0.5   # chỉ dò "thiếu tài liệu" khi ≥ 50% file cần học đã được đọc
DOC_EXT = ("'.docx'", "'.xlsx'", "'.pdf'", "'.pptx'", "'.md'", "'.txt'", "'.csv'")
SPEC_WORDS = ("spec", "master", "tieu chuan", "tiêu chuẩn", "quy trinh", "quy trình", "sop", "dac ta", "đặc tả")
NOT_FOUND = re.compile(r"không tìm thấy|không có thông tin|chưa có thông tin|không thấy|không đủ (thông tin|bằng chứng)|"
                       r"không xác định", re.I)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fp(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def detect(conn) -> list[dict]:
    """Chạy mọi bộ dò, trả về danh sách gap hiện hành (chưa ghi DB)."""
    gaps: list[dict] = []
    # Bộ dò dựa vào nội dung đã index (mồ côi, chủ đề thiếu) chỉ đáng tin khi kho chỉ mục đã đủ đầy; nếu không sẽ báo
    # sai hàng loạt ("chưa có tài liệu nào nhắc tới X" chỉ vì X chưa được index).
    tot = conn.execute("SELECT COUNT(*) FROM files WHERE status='active' AND tier IN (1,2)").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM files WHERE status='active' AND tier IN (1,2) AND indexed_sha256=sha256").fetchone()[0]
    index_ready = tot > 0 and done / tot >= MIN_INDEX_COVERAGE

    def add(kind, subject, detail, evidence):
        gaps.append({"kind": kind, "subject": subject, "detail": detail, "evidence": evidence,
                     "fingerprint": _fp(kind, subject)})

    # 1) mâu thuẫn chưa giải quyết
    groups = conn.execute("""SELECT subject, predicate, GROUP_CONCAT(value || ' (' || f.name || ')', ' ≠ ') AS vals
                             FROM facts x JOIN files f ON f.file_id=x.source_file_id
                             WHERE x.status='conflicted' GROUP BY x.subject_key, x.predicate_key""").fetchall()
    for g in groups:
        add("conflict", f"{g['subject']} · {g['predicate']}", "Các nguồn hiện hành không thống nhất — cần người quyết định",
            g["vals"])

    # 2) tri thức đã duyệt nhưng file nguồn đã đổi sau đó => cần duyệt lại
    for r in conn.execute("""SELECT x.fact_id, x.subject, x.predicate, x.value, f.name FROM facts x
                             JOIN files f ON f.file_id=x.source_file_id
                             WHERE x.status='approved' AND x.source_sha256 != f.sha256 AND f.status='active'"""):
        add("stale_approved", f"fact#{r['fact_id']}", f"'{r['subject']} · {r['predicate']} = {r['value']}' đã duyệt "
            f"nhưng file nguồn {r['name']} đã thay đổi", r["name"])

    # 3) file trùng nội dung (tài liệu bậc 1)
    for r in conn.execute(f"""SELECT sha256, COUNT(*) n, GROUP_CONCAT(path, ' | ') paths FROM files
                              WHERE status='active' AND tier=1 AND ext IN ({','.join(DOC_EXT)}) AND size>0
                              GROUP BY sha256 HAVING n>1 LIMIT 100"""):
        add("duplicate_files", r["sha256"][:12], f"{r['n']} bản trùng nội dung", r["paths"])

    # 4) dự án chỉ có một người viết / không có đặc tả chuẩn
    for p in conn.execute("""SELECT project, department, COUNT(*) n FROM files WHERE status='active' AND tier=1
                             AND project IS NOT NULL GROUP BY project, department HAVING n>=3"""):
        rows = conn.execute("""SELECT name, doc_author, fs_owner FROM files WHERE status='active' AND tier=1
                               AND project=? AND department IS ?""", (p["project"], p["department"])).fetchall()
        authors = Counter(people.clean_name(r["doc_author"] or r["fs_owner"]) for r in rows)
        authors.pop(None, None)
        if authors:
            who, k = authors.most_common(1)[0]
            if k / len(rows) >= 0.9:
                add("single_person_dependency", p["project"], f"{k}/{len(rows)} tài liệu của dự án chỉ do một người viết: {who}",
                    who)
        if len(rows) >= 5 and not any(any(w in r["name"].lower() for w in SPEC_WORDS) for r in rows):
            add("no_canonical_spec", p["project"], f"{len(rows)} tài liệu nhưng không có đặc tả/tiêu chuẩn chuẩn "
                f"(tên chứa spec/master/quy trình/SOP)", ", ".join(r["name"] for r in rows[:6]))

    # 5) tài liệu mồ côi: không thuộc dự án, không có tri thức nào trích ra
    for r in () if not index_ready else conn.execute(f"""SELECT f.file_id, f.name, f.path FROM files f WHERE f.status='active' AND f.tier=1
                              AND f.ext IN ({','.join(DOC_EXT)}) AND f.project IS NULL AND f.indexed_sha256=f.sha256
                              AND NOT EXISTS (SELECT 1 FROM facts x WHERE x.source_file_id=f.file_id)
                              AND f.size>2000 ORDER BY f.fs_modified_at DESC LIMIT 25"""):
        add("orphan_document", r["name"], "Tài liệu không thuộc dự án nào và chưa trích được tri thức", r["path"])

    # 6) câu hỏi AI không trả lời được (gom theo nội dung chuẩn hóa)
    since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    unans = Counter()
    sample = {}
    for r in conn.execute("SELECT question, answer, rating FROM qa_log WHERE created_at>=?", (since,)):
        if r["rating"] == -1 or NOT_FOUND.search(r["answer"] or ""):
            key = people.name_key(r["question"])[:80]
            unans[key] += 1
            sample[key] = r["question"]
    for key, n in unans.most_common(20):
        add("unanswered_question", key, f"AI không trả lời được ({n} lần): \"{sample[key][:120]}\"", sample[key])

    # 7) chủ đề hay được hỏi nhưng không có trong tài liệu nào (BM25 = 0)
    words = Counter()
    for r in conn.execute("SELECT question FROM qa_log WHERE created_at>=?", (since,)):
        for w in set(re.findall(r"\w{4,}", people.name_key(r["question"]))):
            words[w] += 1
    for w, n in words.most_common(30) if index_ready else ():
        if n >= 2:
            try:
                hit = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{w}"',)).fetchone()[0]
            except Exception:
                continue
            if hit == 0:
                add("undocumented_topic", w, f"Được hỏi {n} lần nhưng không tài liệu nào nhắc tới '{w}'", w)
    return gaps


def run(conn) -> dict:
    """Ghi gap mới, đóng gap đã hết đúng, tạo việc nghiên cứu cho gap mới."""
    cur = {g["fingerprint"]: g for g in detect(conn)}
    new = 0
    for fp, g in cur.items():
        if conn.execute("SELECT 1 FROM knowledge_gaps WHERE fingerprint=?", (fp,)).fetchone():
            conn.execute("UPDATE knowledge_gaps SET status='open', resolved_at=NULL, detail=?, evidence=? "
                         "WHERE fingerprint=? AND status='resolved'", (g["detail"], g["evidence"], fp))
            continue
        gid = conn.execute("""INSERT INTO knowledge_gaps (kind, subject, detail, evidence, status, created_at, fingerprint)
                              VALUES (?,?,?,?,'open',?,?)""",
                           (g["kind"], g["subject"], g["detail"], g["evidence"], _now(), fp)).lastrowid
        new += 1
        if g["kind"] in ("unanswered_question", "undocumented_topic", "no_canonical_spec", "conflict",
                         "single_person_dependency"):
            make_research_task(conn, gid, g)
    closed = 0
    for r in conn.execute("SELECT gap_id, fingerprint FROM knowledge_gaps WHERE status='open'").fetchall():
        if r["fingerprint"] not in cur:
            conn.execute("UPDATE knowledge_gaps SET status='resolved', resolved_at=? WHERE gap_id=?", (_now(), r["gap_id"]))
            closed += 1
    conn.commit()
    return {"detected": len(cur), "new": new, "resolved": closed,
            "open": conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE status='open'").fetchone()[0]}


def make_research_task(conn, gap_id: int, g: dict):
    """Thu thập bằng chứng NỘI BỘ cho một gap: tìm lai trong tài liệu, gom ứng viên để người kiểm chứng.
    Không tự tạo fact 'đáng tin' — chỉ đưa vào hàng đợi duyệt."""
    from .index import search
    query = re.sub(r"\s+", " ", (g["evidence"] if g["kind"] in ("unanswered_question", "undocumented_topic")
                                 else f"{g['subject']} {g['kind']}"))[:200]
    try:
        hits = search(query, [ADMIN_GROUP], limit=6)
    except Exception:
        hits = []
    cands = [{"file": h["name"], "loc": h["loc"], "score": h["score"], "snippet": h["text"][:220], "path": h["path"]}
             for h in hits]
    conn.execute("INSERT INTO research_tasks (gap_id, status, candidates, created_at) VALUES (?,?,?,?)",
                 (gap_id, "open" if cands else "no_evidence", json.dumps(cands, ensure_ascii=False), _now()))


def list_gaps(conn, status: str = "open", limit: int = 100) -> list[dict]:
    rows = conn.execute("""SELECT g.*, (SELECT candidates FROM research_tasks t WHERE t.gap_id=g.gap_id
                           ORDER BY task_id DESC LIMIT 1) AS research FROM knowledge_gaps g WHERE g.status=?
                           ORDER BY gap_id DESC LIMIT ?""", (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["research"] = json.loads(d["research"]) if d.get("research") else []
        out.append(d)
    return out
