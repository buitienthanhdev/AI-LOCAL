"""Sổ đóng góp (Contribution Ledger). Mọi dòng có người, loại, trọng số, thời điểm và BẰNG CHỨNG (file/commit).

Nguyên tắc: không đếm "số file" như đóng góp. Mỗi loại việc có trọng số riêng (cấu hình được); file thư viện
phần mềm (bậc 3) không bao giờ được tính. Phân biệt rõ người tạo / người sửa / người duyệt / người commit."""
import json
import math
from datetime import datetime, timedelta

from . import people
from .catalog import connect

# Trọng số theo loại đóng góp (cấu hình được: sửa dict này hoặc ghi đè bằng bảng riêng sau)
WEIGHTS = {
    "created_doc": 3.0,       # tạo tài liệu mới
    "modified_doc": 1.0,      # chỉnh sửa tài liệu
    "created_code": 2.0,      # tạo file mã nguồn
    "modified_code": 0.7,
    "git_commit": 2.0,        # mỗi commit (cộng thêm theo quy mô thay đổi, có trần)
    "fact_validated": 5.0,    # người duyệt tri thức => kiểm chứng
    "fact_rejected": 2.0,     # loại bỏ tri thức sai cũng là đóng góp chất lượng
    "feedback": 0.5,          # đánh giá/chỉnh câu trả lời của AI
    "approved_doc": 6.0,
    "reviewed_doc": 4.0,
}
KIND_LABEL = {"created_doc": "Tạo tài liệu", "modified_doc": "Sửa tài liệu", "created_code": "Tạo mã nguồn",
              "modified_code": "Sửa mã nguồn", "git_commit": "Git commit", "fact_validated": "Duyệt tri thức",
              "fact_rejected": "Loại tri thức sai", "feedback": "Phản hồi cho AI", "approved_doc": "Phê duyệt tài liệu",
              "reviewed_doc": "Review tài liệu"}


def record(conn, raw_actor, kind: str, at: str, ref: str, file_id=None, project=None, department=None,
           evidence: str | None = None, weight: float | None = None) -> bool:
    """Ghi một dòng đóng góp (idempotent theo ref). Trả True nếu có dòng mới."""
    pid = people.resolve(conn, raw_actor)
    if pid is None:
        return False
    cur = conn.execute("""INSERT OR IGNORE INTO contributions (person_id, kind, weight, file_id, project, department,
                          at, evidence, ref) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (pid, kind, WEIGHTS.get(kind, 1.0) if weight is None else weight, file_id, project,
                        department, at, (evidence or "")[:300], ref))
    return cur.rowcount > 0


OWNER_ONLY_FACTOR = 0.5   # chỉ biết "chủ sở hữu NTFS" (không có tác giả khai trong tài liệu) => bằng chứng yếu
BULK_THRESHOLD = 25       # > ngần này file cùng người, cùng dự án, cùng phút => giải nén/sao chép hàng loạt, không phải việc làm
FILE_KINDS = ("created_doc", "modified_doc", "created_code", "modified_code")
# Chỉ TÀI LIỆU mới là đóng góp. Ảnh/audio/video (ảnh chụp màn hình, ảnh do AI sinh ra...) thì không.
DOC_EXT = (".docx", ".xlsx", ".pdf", ".pptx", ".md", ".txt", ".csv")


def record_file_event(conn, f, kind: str, actor: str | None, at: str, version_no: int, declared: bool = True):
    """f: dòng files. Chỉ tính TÀI LIỆU của người dùng (bậc 1). Mã nguồn chỉ được tính qua commit Git (nguồn tác giả
    đáng tin); file code tải về/giải nén có chủ sở hữu NTFS là chính bạn nhưng KHÔNG phải do bạn viết.
    declared=False: người được suy ra từ chủ sở hữu NTFS (không có tác giả khai) => trọng số giảm một nửa."""
    if f["tier"] != 1 or (f["ext"] or "") not in DOC_EXT:
        return
    k = {"created": "created_doc", "modified": "modified_doc"}[kind]
    w = WEIGHTS[k] * (1.0 if declared else OWNER_ONLY_FACTOR)
    record(conn, actor, k, at, f"file:{f['file_id']}:v{version_no}:{kind}", f["file_id"], f["project"],
           f["department"], f"{f['name']} (v{version_no})" + ("" if declared else " · theo chủ sở hữu NTFS"), w)


def record_commit(conn, repo_name: str, sha: str, author: str, at: str, subject: str, ins: int, dele: int,
                  files: int) -> bool:
    size_bonus = min(3.0, math.log10(1 + ins + dele))  # commit lớn được cộng nhưng có trần
    return record(conn, author, "git_commit", at, f"git:{sha}", None, repo_name, None,
                  f"{sha[:8]} {subject} (+{ins}/-{dele}, {files} file)", WEIGHTS["git_commit"] + size_bonus)


def backfill(conn) -> int:
    """Dựng sổ đóng góp từ dữ liệu đã có (files + phiên bản) cho các file chưa có dòng nào. Idempotent."""
    n = 0
    # chính sách mới: mã nguồn chỉ tính qua Git => gỡ các dòng cũ tính theo file code
    conn.execute("DELETE FROM contributions WHERE kind IN ('created_code','modified_code')")
    ph = ",".join("?" * len(DOC_EXT))
    conn.execute(f"""DELETE FROM contributions WHERE kind IN ('created_doc','modified_doc') AND file_id IN
                     (SELECT file_id FROM files WHERE ext NOT IN ({ph}) OR tier != 1)""", DOC_EXT)
    # người thuộc nhóm "tài khoản hệ thống/phần mềm" (vd. '(anonymous)') không phải con người
    sysk = [people.name_key(a) for a in people.SYSTEM_ACCOUNTS]
    conn.execute(f"""DELETE FROM contributions WHERE person_id IN
                     (SELECT person_id FROM people WHERE key IN ({','.join('?' * len(sysk))}))""", sysk)
    rows = conn.execute(f"""SELECT * FROM files WHERE status='active' AND tier=1 AND ext IN ({ph})""", DOC_EXT).fetchall()
    for f in rows:
        creator = f["doc_author"] or f["fs_owner"]
        at = f["doc_created_at"] or f["fs_created_at"] or f["first_seen_at"]
        if creator and at:
            record_file_event(conn, f, "created", creator, at, 1, declared=bool(f["doc_author"]))
            n += 1
        editor = f["doc_last_editor"]
        if editor and editor != creator and f["fs_modified_at"]:
            record_file_event(conn, f, "modified", editor, f["fs_modified_at"], f["version_no"] or 1)
    return n


def report(conn, groups: list[str], admin: bool, project: str | None = None, person: str | None = None,
           since_days: int | None = 365, limit: int = 20) -> dict:
    """Thống kê đóng góp từ SỔ, kèm bằng chứng. Chỉ tính đóng góp trên file người hỏi có quyền xem."""
    where, args = ["1=1"], []
    if project:
        where.append("lower(c.project) LIKE ?")
        args.append(f"%{project.lower()}%")
    if since_days:
        where.append("c.at >= ?")
        args.append((datetime.now() - timedelta(days=since_days)).isoformat(timespec="seconds"))
    rows = conn.execute(f"""SELECT c.*, p.name AS person, f.acl, f.name AS file_name
                            FROM contributions c JOIN people p ON p.person_id=c.person_id
                            LEFT JOIN files f ON f.file_id=c.file_id WHERE {' AND '.join(where)}""", args).fetchall()
    from .server_acl import can_read  # tránh vòng import
    # đóng góp gắn với file: theo quyền của file; commit Git (không gắn file): chỉ admin (kho mã mặc định riêng tư)
    rows = [r for r in rows if admin or (r["file_id"] is not None and can_read(r["acl"] or "private", groups))]
    if person:
        pk = people.name_key(person)
        rows = [r for r in rows if pk in people.name_key(r["person"])]
    # chống "thổi phồng": giải nén/sao chép hàng loạt (nhiều file cùng người+dự án+phút) không phải đóng góp
    from collections import Counter
    key = lambda r: (r["person"], r["project"], (r["at"] or "")[:16])  # noqa: E731
    cnt = Counter(key(r) for r in rows if r["kind"] in FILE_KINDS)
    bulk = {k for k, v in cnt.items() if v > BULK_THRESHOLD}
    ignored = sum(cnt[k] for k in bulk)
    rows = [r for r in rows if not (r["kind"] in FILE_KINDS and key(r) in bulk)]
    by: dict[str, dict] = {}
    for r in rows:
        d = by.setdefault(r["person"], {"person": r["person"], "score": 0.0, "kinds": {}, "evidence": []})
        d["score"] += r["weight"]
        d["kinds"][r["kind"]] = d["kinds"].get(r["kind"], 0) + 1
        if len(d["evidence"]) < 4:
            d["evidence"].append(f"{r['at'][:10]} {KIND_LABEL.get(r['kind'], r['kind'])}: {r['evidence']}")
    out = sorted(by.values(), key=lambda x: -x["score"])[:limit]
    for d in out:
        d["score"] = round(d["score"], 1)
        d["kinds"] = {KIND_LABEL.get(k, k): v for k, v in d["kinds"].items()}
    return {"project": project, "since_days": since_days, "total_events": len(rows), "bulk_events_ignored": ignored,
            "weights": {KIND_LABEL.get(k, k): v for k, v in WEIGHTS.items()}, "ranking": out,
            "note": "Điểm = tổng trọng số các loại việc (không phải số file). Xem 'evidence' để kiểm chứng."}


def to_json(report_: dict) -> str:
    return json.dumps(report_, ensure_ascii=False)
