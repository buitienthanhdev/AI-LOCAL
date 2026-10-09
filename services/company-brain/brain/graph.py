"""Đồ thị tri thức (entities + relations) dựng NGAY trong SQLite — đủ cho duyệt vài bước ở quy mô này;
chưa cần Neo4j (chỉ thêm khi truy vấn đồ thị thực sự là nút thắt).

Quan hệ có thời gian hiệu lực (valid_from/valid_to), độ tin cậy, và LUÔN gắn với file nguồn => lọc quyền theo file."""
import json
from collections import deque
from datetime import datetime

from . import people
from .facts import norm
from .server_acl import acl_sql

# predicate trong kho tri thức -> (loại chủ thể, quan hệ, loại đối tượng)
FACT_RELATIONS = {
    "uses_material": ("product", "PRODUCT_USES_MATERIAL", "material"),
    "supplier": ("material", "MATERIAL_SUPPLIED_BY", "company"),
    "performed_by": ("experiment", "EXPERIMENT_PERFORMED_BY", "person"),
    "belongs_to_project": ("experiment", "BELONGS_TO_PROJECT", "project"),
    "root_cause": ("incident", "ROOT_CAUSE", "cause"),
    "corrective_action": ("incident", "RESOLVED_BY", "action"),
    "decision": ("project", "DECISION_MADE", "decision"),
    "test_result": ("experiment", "HAS_RESULT", "result"),
    "cost_target": ("project", "COST_TARGET", "target"),
}


def entity(conn, etype: str, name: str) -> int | None:
    name = (name or "").strip()
    if not name or len(name) > 160:
        return None
    key = people.name_key(name) if etype == "person" else norm(name)
    if not key:
        return None
    # Định danh theo TÊN chuẩn hóa: "M500" vừa là sản phẩm vừa là dự án vẫn là MỘT nút (nếu không đồ thị bị đứt).
    # Người (person) thì tách riêng để không nhầm với vật liệu/dự án trùng tên.
    r = conn.execute("SELECT entity_id, type FROM entities WHERE key=? ORDER BY (type=?) DESC", (key, etype)).fetchall()
    for e in r:
        if (e["type"] == "person") == (etype == "person"):
            return e["entity_id"]
    return conn.execute("INSERT INTO entities (type, name, key) VALUES (?,?,?)", (etype, name, key)).lastrowid


def _rel(conn, src, rel, dst, ref, source_file_id=None, evidence=None, valid_from=None, valid_to=None, conf=0.8):
    if src is None or dst is None or src == dst:
        return
    conn.execute("""INSERT INTO relations (src, rel, dst, source_file_id, evidence, valid_from, valid_to, confidence, ref)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET valid_to=excluded.valid_to,
                    confidence=excluded.confidence, evidence=excluded.evidence""",
                 (src, rel, dst, source_file_id, (evidence or "")[:300], valid_from, valid_to, conf, ref))


def rebuild(conn) -> dict:
    """Dựng/cập nhật đồ thị từ files + facts. Idempotent (ref duy nhất cho mỗi quan hệ)."""
    n0 = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    # --- từ file (chỉ tài liệu/mã nguồn người dùng: bậc 1-2)
    for f in conn.execute("SELECT * FROM files WHERE status='active' AND tier IN (1,2)").fetchall():
        doc = entity(conn, "document", f["name"] + f" #{f['file_id']}")
        if f["project"]:
            proj = entity(conn, "project", f["project"])
            _rel(conn, proj, "PROJECT_CONTAINS_DOCUMENT", doc, f"file:{f['file_id']}:proj", f["file_id"], f["name"])
            if f["department"]:
                _rel(conn, entity(conn, "department", f["department"]), "DEPARTMENT_OWNS_PROJECT", proj,
                     f"dept:{f['department']}:{f['project']}", f["file_id"], f["project"])
        creator = people.clean_name(f["doc_author"] or f["fs_owner"])
        if creator:
            _rel(conn, entity(conn, "person", creator), "PERSON_CREATED_DOCUMENT", doc, f"file:{f['file_id']}:cre",
                 f["file_id"], f["name"], f["doc_created_at"] or f["fs_created_at"])
        editor = people.clean_name(f["doc_last_editor"])
        if editor and editor != creator:
            _rel(conn, entity(conn, "person", editor), "PERSON_MODIFIED_DOCUMENT", doc, f"file:{f['file_id']}:mod",
                 f["file_id"], f["name"], f["fs_modified_at"])
    # --- từ tri thức (kèm thời gian hiệu lực + nguồn)
    conn.execute("DELETE FROM relations WHERE ref LIKE 'fact:%'")  # dựng lại từ kho tri thức hiện tại
    for x in conn.execute("""SELECT * FROM facts WHERE status NOT IN ('rejected')""").fetchall():
        spec = FACT_RELATIONS.get(x["predicate"])
        fdoc = conn.execute("SELECT name FROM files WHERE file_id=?", (x["source_file_id"],)).fetchone()
        if spec:
            stype, rel, otype = spec
            _rel(conn, entity(conn, stype, x["subject"]), rel, entity(conn, otype, x["value"]), f"fact:{x['fact_id']}",
                 x["source_file_id"], x["evidence"], x["valid_from"], x["valid_to"], x["confidence"] or 0.6)
        elif x["predicate"] == "material_percent" and "/" in x["subject"]:
            prod, mat = [t.strip() for t in x["subject"].split("/", 1)]
            _rel(conn, entity(conn, "product", prod), "FORMULA_CONTAINS_MATERIAL", entity(conn, "material", mat),
                 f"fact:{x['fact_id']}", x["source_file_id"], f"{x['value']}% — {x['evidence']}",
                 x["valid_from"], x["valid_to"], x["confidence"] or 0.6)
        if fdoc:  # FACT_DERIVED_FROM_DOCUMENT: tài liệu "hỗ trợ" thực thể chủ thể
            subj = entity(conn, (spec[0] if spec else "topic"), x["subject"].split("/")[0])
            d = entity(conn, "document", fdoc["name"] + f" #{x['source_file_id']}")
            _rel(conn, d, "DOCUMENT_SUPPORTS_ENTITY", subj, f"fact:{x['fact_id']}:doc", x["source_file_id"],
                 x["evidence"], x["valid_from"], x["valid_to"], x["confidence"] or 0.6)
    # file đã xóa/chuyển bậc: bỏ quan hệ mồ côi
    conn.execute("""DELETE FROM relations WHERE source_file_id IS NOT NULL AND source_file_id NOT IN
                    (SELECT file_id FROM files WHERE status='active')""")
    conn.commit()
    n1 = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    return {"entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], "relations": n1,
            "new_relations": n1 - n0}


def _edges(conn, entity_ids: list[int], groups: list[str]):
    """Mọi cạnh chạm tới các entity (cả hai chiều), đã lọc quyền theo file nguồn."""
    if not entity_ids:
        return []
    cond, args = acl_sql(groups, "f.acl")
    ph = ",".join("?" * len(entity_ids))
    return conn.execute(f"""SELECT r.*, a.name AS src_name, a.type AS src_type, b.name AS dst_name, b.type AS dst_type,
            f.name AS file_name FROM relations r JOIN entities a ON a.entity_id=r.src JOIN entities b ON b.entity_id=r.dst
            LEFT JOIN files f ON f.file_id=r.source_file_id
            WHERE (r.src IN ({ph}) OR r.dst IN ({ph})) AND (r.source_file_id IS NULL OR {cond})""",
                        [*entity_ids, *entity_ids, *args]).fetchall()


def find_entities(conn, text: str, limit: int = 5) -> list:
    k = norm(text)
    pk = people.name_key(text)
    rows = conn.execute("""SELECT * FROM entities WHERE key=? OR key=? ORDER BY type LIMIT ?""", (k, pk, limit)).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM entities WHERE key LIKE ? OR key LIKE ? LIMIT ?",
                            (f"%{k}%", f"%{pk}%", limit)).fetchall()
    return rows


def neighbors(conn, name: str, groups: list[str], depth: int = 2, limit: int = 60) -> dict:
    """Duyệt đồ thị từ một thực thể tới `depth` bước. Trả về danh sách quan hệ kèm nguồn + thời gian hiệu lực."""
    depth = max(1, min(3, depth))
    start = find_entities(conn, name)
    if not start:
        return {"found": False, "entity": name, "edges": []}
    seen_e, seen_r, edges = {r["entity_id"] for r in start}, set(), []
    frontier = list(seen_e)
    for d in range(1, depth + 1):
        nxt = []
        for r in _edges(conn, frontier, groups):
            if r["rel_id"] in seen_r:
                continue
            seen_r.add(r["rel_id"])
            edges.append({"hop": d, "from": f"{r['src_name']} ({r['src_type']})", "relation": r["rel"],
                          "to": f"{r['dst_name']} ({r['dst_type']})", "source_file": r["file_name"],
                          "source_file_id": r["source_file_id"], "evidence": r["evidence"],
                          "valid_from": (r["valid_from"] or "")[:10], "valid_to": (r["valid_to"] or "")[:10] or None,
                          "confidence": r["confidence"]})
            for e in (r["src"], r["dst"]):
                if e not in seen_e:
                    seen_e.add(e)
                    nxt.append(e)
        frontier = nxt
        if not frontier or len(edges) >= limit:
            break
    return {"found": True, "entity": [f"{r['name']} ({r['type']})" for r in start], "edges": edges[:limit]}


def path(conn, a: str, b: str, groups: list[str], max_depth: int = 4) -> dict:
    """Đường đi ngắn nhất giữa hai thực thể (BFS) — vd. 'Thành' -> 'Acronal PS 673'."""
    sa, sb = find_entities(conn, a, 3), {r["entity_id"] for r in find_entities(conn, b, 3)}
    if not sa or not sb:
        return {"found": False}
    q = deque([(r["entity_id"], [r["name"]]) for r in sa])
    seen = {r["entity_id"] for r in sa}
    while q:
        cur, trail = q.popleft()
        if cur in sb:
            return {"found": True, "path": trail}
        if len(trail) > max_depth * 2:
            continue
        for r in _edges(conn, [cur], groups):
            other = r["dst"] if r["src"] == cur else r["src"]
            if other not in seen:
                seen.add(other)
                q.append((other, trail + [f"--{r['rel']}-->" if r["src"] == cur else f"<--{r['rel']}--",
                                          r["dst_name"] if r["src"] == cur else r["src_name"]]))
    return {"found": False}


def related_files(conn, file_ids: list[int], groups: list[str], limit: int = 5) -> list[int]:
    """Mở rộng truy xuất bằng đồ thị: các file khác cùng nói về những thực thể mà các file đã chọn nói tới."""
    if not file_ids:
        return []
    ph = ",".join("?" * len(file_ids))
    ents = {e for r in conn.execute(f"SELECT src, dst FROM relations WHERE source_file_id IN ({ph})", file_ids)
            for e in (r[0], r[1])}
    # bỏ thực thể quá chung chung (document/project/department) để khỏi kéo cả thư mục
    ents = {r[0] for r in conn.execute(
        f"SELECT entity_id FROM entities WHERE entity_id IN ({','.join('?' * len(ents)) or 'NULL'}) "
        f"AND type NOT IN ('document','project','department','person')", list(ents))} if ents else set()
    if not ents:
        return []
    cond, args = acl_sql(groups, "f.acl")
    eph = ",".join("?" * len(ents))
    rows = conn.execute(f"""SELECT r.source_file_id, COUNT(*) n FROM relations r JOIN files f ON f.file_id=r.source_file_id
            WHERE (r.src IN ({eph}) OR r.dst IN ({eph})) AND r.source_file_id NOT IN ({ph}) AND f.status='active' AND {cond}
            GROUP BY r.source_file_id ORDER BY n DESC LIMIT ?""", [*ents, *ents, *file_ids, *args, limit]).fetchall()
    return [r[0] for r in rows]


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
