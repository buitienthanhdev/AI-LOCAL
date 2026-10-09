"""Hộp cát SQL cho AI: chỉ SELECT, chỉ qua các VIEW đã lọc quyền theo người hỏi. Mọi bảng gốc (files, facts,
people, audit, memories...) đều bị chặn đọc trực tiếp bằng SQLite authorizer (cơ chế allowlist: không phải liệt kê
từng bảng cần cấm — bảng mới thêm sau mặc định cũng bị cấm)."""
import sqlite3

from .catalog import connect
from .config import ADMIN_GROUP
from .facts import norm

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

SCHEMA_HINT = """
visible_files(file_id, path, name, ext, size, sha256, fs_created_at, fs_modified_at, fs_owner,
              doc_author, doc_last_editor, doc_created_at, title, department, project, status,
              first_seen_at, last_seen_at, tier, version_no, name_key)   -- tier 1 tài liệu · 2 mã nguồn · 3 thư viện
              -- name_key = tên + tiêu đề viết thường, KHÔNG DẤU: tìm theo tên dùng name_key LIKE '%bao cao%' (nhanh)
visible_events(event_id, file_id, event, actor, at, detail)   -- event: discovered|modified|moved|deleted|restored
visible_versions(file_id, file_name, version_no, sha256, size, fs_modified_at, editor, observed_at, diff_added,
                 diff_removed, diff_summary)   -- lịch sử phiên bản nội dung + khác biệt so với bản trước
visible_contributions(person, kind, weight, file_id, file_name, project, department, at, evidence)
  -- sổ đóng góp có bằng chứng; kind: created_doc, modified_doc, created_code, modified_code, git_commit (chỉ admin),
  --   fact_validated, fact_rejected, feedback ...  Điểm = SUM(weight), KHÔNG phải số file.
visible_relations(from_name, from_type, relation, to_name, to_type, source_file_id, source_file, evidence, valid_from,
                  valid_to, confidence)   -- đồ thị tri thức (người–tài liệu–dự án–vật liệu–nhà cung cấp...)
visible_facts(fact_id, subject, predicate, value, unit, subject_key, predicate_key, evidence, source_file_id,
              source_name, source_path, file_id, name, path, author, valid_from, valid_to, confidence, status, note, reviewed_by)
  -- Tri thức đã trích từ tài liệu. subject_key: chữ thường không dấu (lọc bằng LIKE '%m500%').
  -- predicate: uses_material, material_percent, supplier, target_ph, measured_ph, target_viscosity,
  --   measured_viscosity, root_cause, corrective_action, test_result, decision, performed_by, belongs_to_project,
  --   cost_target, other_property
  -- status: approved (người duyệt, chuẩn nhất) > trusted > usable > candidate; conflicted = các nguồn mâu thuẫn;
  --   superseded = đã bị phiên bản mới thay (valid_to = ngày hết hiệu lực); rejected = bị loại.
visible_gaps(kind, subject, detail, evidence, status, created_at)   -- lỗ hổng tri thức (CHỈ admin có dữ liệu)
Thời gian dạng ISO 'YYYY-MM-DDTHH:MM:SS'. status: 'active' | 'deleted'.
Hàm norm(text): chữ thường, bỏ dấu tiếng Việt. Tìm tên file KHÔNG phân biệt dấu/hoa thường:
  WHERE norm(name) LIKE '%cong thuc%'   (từ khóa viết thường, không dấu)
"""

_FILE_COLS = ("file_id, path, name, ext, size, sha256, fs_created_at, fs_modified_at, fs_owner, doc_author, "
              "doc_last_editor, doc_created_at, title, department, project, status, first_seen_at, last_seen_at, "
              "tier, version_no, name_key")


def _views(conn, groups: list[str]):
    admin = ADMIN_GROUP in groups
    lit = [f"%,{g},%".replace("'", "''") for g in sorted({*groups, "all"})]
    cond = "1=1" if admin else " OR ".join(f"(',' || acl || ',') LIKE '{p}'" for p in lit)
    conn.execute(f"CREATE TEMP VIEW visible_files AS SELECT {_FILE_COLS} FROM main.files WHERE {cond}")
    conn.execute("CREATE TEMP VIEW visible_events AS SELECT e.* FROM main.file_events e "
                 "JOIN visible_files f ON f.file_id = e.file_id")
    conn.execute("""CREATE TEMP VIEW visible_versions AS SELECT v.file_id, f.name AS file_name, v.version_no, v.sha256,
                    v.size, v.fs_modified_at, v.editor, v.observed_at, v.diff_added, v.diff_removed, v.diff_summary
                    FROM main.file_versions v JOIN visible_files f ON f.file_id = v.file_id""")
    # đóng góp: gắn file => theo quyền file; không gắn file (Git) => chỉ admin
    file_or_admin = "f.file_id IS NOT NULL" if admin else "f.file_id IS NOT NULL"
    conn.execute(f"""CREATE TEMP VIEW visible_contributions AS SELECT p.name AS person, c.kind, c.weight, c.file_id,
                    f.name AS file_name, c.project, c.department, c.at, c.evidence
                    FROM main.contributions c JOIN main.people p ON p.person_id = c.person_id
                    LEFT JOIN visible_files f ON f.file_id = c.file_id
                    WHERE {file_or_admin} OR {'1=1' if admin else '0=1'}""")
    conn.execute("""CREATE TEMP VIEW visible_relations AS SELECT a.name AS from_name, a.type AS from_type, r.rel AS relation,
                    b.name AS to_name, b.type AS to_type, r.source_file_id, f.name AS source_file, r.evidence,
                    r.valid_from, r.valid_to, r.confidence FROM main.relations r
                    JOIN main.entities a ON a.entity_id = r.src JOIN main.entities b ON b.entity_id = r.dst
                    LEFT JOIN visible_files f ON f.file_id = r.source_file_id
                    WHERE r.source_file_id IS NULL OR f.file_id IS NOT NULL""")
    conn.execute("""CREATE TEMP VIEW visible_facts AS SELECT x.fact_id, x.subject, x.predicate, x.value, x.unit,
                    x.subject_key, x.predicate_key, x.evidence, x.source_file_id, f.name AS source_name,
                    f.path AS source_path, f.file_id, f.name, f.path, x.author, x.valid_from, x.valid_to, x.confidence,
                    x.status, x.note, x.reviewed_by FROM main.facts x JOIN visible_files f ON f.file_id = x.source_file_id
                    WHERE x.status != 'rejected'""")
    conn.execute(f"""CREATE TEMP VIEW visible_gaps AS SELECT kind, subject, detail, evidence, status, created_at
                    FROM main.knowledge_gaps WHERE {'1=1' if admin else '0=1'}""")


def _authorizer(action, arg1, arg2, dbname, source):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    # Đọc cột của bảng gốc (schema 'main') chỉ hợp lệ khi đi qua một view (source != None)
    if action == sqlite3.SQLITE_READ and dbname == "main" and source is None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run_sql(query: str, groups: list[str], max_rows: int = 50) -> dict:
    """SQL chỉ đọc, chỉ qua view đã lọc quyền — AI không đọc được bảng gốc."""
    conn = connect(readonly=True)
    _views(conn, groups)
    conn.create_function("norm", 1, lambda s: norm(s) if s is not None else None, deterministic=True)
    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(query)
        rows = [dict(r) for r in cur.fetchmany(max_rows)]
        return {"rows": rows, "truncated": len(rows) == max_rows}
    except sqlite3.Error as e:
        return {"error": str(e), "hint": "Chỉ được SELECT trên các view visible_* (xem schema)."}
    finally:
        conn.close()
