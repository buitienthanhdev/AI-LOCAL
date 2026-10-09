"""Fact Store: trích fact từ tài liệu (LLM local) -> lưu kèm nguồn, thời gian hiệu lực, độ tin cậy ->
phát hiện mâu thuẫn / tiến hóa phiên bản. AI KHÔNG tự quyết sự thật: mâu thuẫn chỉ được đánh dấu,
'approved'/'rejected' chỉ do con người đặt (qua web)."""
import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

from .catalog import connect
from .config import (CHAT_KEEP_ALIVE, CHAT_MODEL, LLM_OPTIONS, DEFAULT_SOURCE_WEIGHT, FACT_EXT, FACT_MAX_CHARS, FACT_ROOTS,
                     OLLAMA_URL, SOURCE_WEIGHTS, TRUSTED_AT, USABLE_AT)
from .extract import extract

# Từ vựng quan hệ có kiểm soát. single=True: mỗi chủ thể chỉ có 1 giá trị đúng tại 1 thời điểm
# (khác giá trị => mâu thuẫn hoặc thay đổi phiên bản). single=False: nhiều giá trị cùng đúng.
PREDICATES = {
    "uses_material":     (False, "chủ thể (sản phẩm/công thức) dùng nguyên liệu X"),
    "material_percent":  (True,  "tỉ lệ % của một nguyên liệu trong công thức; subject = '<sản phẩm> / <nguyên liệu>'"),
    "supplier":          (True,  "nhà cung cấp của nguyên liệu"),
    "target_ph":         (True,  "pH mục tiêu / tiêu chuẩn"),
    "measured_ph":       (True,  "pH đo được của một batch/thí nghiệm cụ thể"),
    "target_viscosity":  (True,  "độ nhớt mục tiêu"),
    "measured_viscosity": (True, "độ nhớt đo được của một batch/thí nghiệm"),
    "root_cause":        (False, "nguyên nhân của một sự cố"),
    "corrective_action": (False, "biện pháp khắc phục"),
    "test_result":       (False, "kết luận/kết quả của thí nghiệm"),
    "decision":          (False, "quyết định đã được đưa ra"),
    "performed_by":      (False, "người thực hiện thí nghiệm/công việc"),
    "belongs_to_project": (False, "thuộc dự án"),
    "cost_target":       (True,  "mục tiêu chi phí/giá thành"),
    "other_property":    (False, "thuộc tính khác quan trọng (ghi rõ trong value)"),
}

SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string", "enum": list(PREDICATES)},
            "value": {"type": "string"},
            "unit": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD nếu tài liệu ghi rõ ngày, ngược lại rỗng"},
            "evidence": {"type": "string"},
        },
        "required": ["subject", "predicate", "value", "evidence"]}}},
    "required": ["facts"],
}

PROMPT = """Bạn trích xuất TRI THỨC KỸ THUẬT từ tài liệu nội bộ công ty sơn. Chỉ lấy điều tài liệu nói RÕ, không suy diễn.
Quan hệ được phép:
{preds}
Quy tắc:
- subject: tên riêng ngắn gọn, nhất quán (vd. "M500", "Acronal PS 673", "Long An Batch 01").
- value: giá trị ngắn; số thì chỉ ghi số, đơn vị để ở unit.
- evidence: trích NGUYÊN VĂN ngắn (<150 ký tự) đoạn chứa fact.
- target_* CHỈ khi tài liệu nói rõ là mục tiêu/tiêu chuẩn/spec; số đo thực tế dùng measured_*.
- Mỗi batch/thí nghiệm/thời điểm đo là một subject riêng (vd. "Long An Batch 01 - sau 7 ngày").
- Không lặp lại cùng một thông tin bằng nhiều quan hệ; other_property tối đa 5 cái, chỉ khi không quan hệ nào khớp.
- Tối đa 40 fact quan trọng nhất. Không có gì đáng trích => trả mảng rỗng.

Tài liệu: {name}
---
{text}
---"""


# ---------- chuẩn hóa ----------
def _py_norm(s: str) -> str:
    """Bản tham chiếu (Python). Bản Rust brain_core.norm phải cho kết quả y hệt — scripts/parity_check.py."""
    s = unicodedata.normalize("NFKD", str(s).replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9.%/]+", " ", s).strip()


def _load_rust_norm():
    from .config import USE_RUST_NORM
    if not USE_RUST_NORM:
        return None
    try:
        import brain_core
        return brain_core.norm
    except ImportError:  # chưa build/cài brain_core => tự lùi về Python
        return None


_rs_norm = _load_rust_norm()


def norm(s: str) -> str:
    if _rs_norm is None:
        return _py_norm(s)
    return _rs_norm(s if type(s) is str else str(s))


_NUM = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(?:-|–|to|den)\s*(-?\d+(?:[.,]\d+)?)\s*[a-z°%]*\s*$|^\s*(-?\d+(?:[.,]\d+)?)\s*[a-z°%]*\s*$")


def norm_value(v: str) -> str:
    """'105 KU' == '105'; '8,5' == '8.5'; khoảng '8.5 - 9.0' => '8.5..9.0'."""
    t = norm(v)
    m = _NUM.match(str(v).strip().lower().replace(",", "."))
    if m:
        if m.group(3):
            return repr(round(float(m.group(3)), 6))
        return f"{round(float(m.group(1)), 6)!r}..{round(float(m.group(2)), 6)!r}"
    return t


def _compatible(a: str, b: str) -> bool:
    """Hai value_key có mâu thuẫn không: bằng nhau, hoặc một số nằm trong khoảng kia => tương thích."""
    if a == b:
        return True
    def rng(x):
        try:
            lo, _, hi = x.partition("..")
            return float(lo), float(hi or lo)
        except ValueError:
            return None
    ra, rb = rng(a), rng(b)
    return bool(ra and rb) and (ra[0] <= rb[0] <= rb[1] <= ra[1] or rb[0] <= ra[0] <= ra[1] <= rb[1])


def file_family(name: str) -> str:
    """'M500_Formula_v12.xlsx' và 'M500_Formula_v13_final.xlsx' cùng họ 'm500 formula'."""
    stem = norm(Path(name).stem)
    stem = re.sub(r"\b(v|ver|version|rev|r)\s*\d+\b|\b\d{4}[ .-]?\d{2}([ .-]?\d{2})?\b|\b(final|copy|new|old|ban|moi|cu)\b|\b\d+\b",
                  " ", stem)
    return re.sub(r"\s+", " ", stem).strip()


def source_weight(name: str) -> float:
    n = name.lower()
    for keys, w in SOURCE_WEIGHTS:
        if any(k in n for k in keys):
            return w
    return DEFAULT_SOURCE_WEIGHT


def in_scope(path: str, ext: str) -> bool:
    if ext not in FACT_EXT:
        return False
    low = path.lower()
    return any(low.startswith(str(Path(r).resolve()).lower()) for r in FACT_ROOTS)


_file_lock = threading.Lock()  # watcher và job nền không được trích cùng 1 file song song


# ---------- trích xuất ----------
def llm_extract(name: str, text: str, model: str | None = None) -> list[dict]:
    preds = "\n".join(f"- {k}: {d}" for k, (_, d) in PREDICATES.items())
    from . import llm
    r = llm.chat([{"role": "user", "content": PROMPT.format(preds=preds, name=name, text=text[:FACT_MAX_CHARS])}],
                 role="extract", fmt=SCHEMA, temperature=0, model=model)
    out = json.loads(r["message"]["content"]).get("facts", [])
    return [f for f in out if f.get("predicate") in PREDICATES and str(f.get("subject", "")).strip()
            and str(f.get("value", "")).strip()]


def _valid_date(s: str | None) -> str | None:
    try:
        return datetime.fromisoformat(str(s)[:19]).isoformat(timespec="seconds") if s else None
    except ValueError:
        return None


def _extract_file(conn, f) -> set[tuple[str, str]]:
    """Trích fact cho 1 file, thay fact cũ (trừ fact người đã duyệt). Trả về các khóa cần resolve lại."""
    # Gọi LLM (chậm) TRƯỚC khi ghi DB để không giữ khóa ghi trong lúc chờ
    text, _ = extract(Path(f["path"]))
    items = llm_extract(f["name"], text) if text.strip() else []
    affected = {(r["subject_key"], r["predicate_key"]) for r in conn.execute(
        "SELECT subject_key, predicate_key FROM facts WHERE source_file_id=?", (f["file_id"],))}
    conn.execute("DELETE FROM facts WHERE source_file_id=? AND status NOT IN ('approved','rejected')", (f["file_id"],))
    base = source_weight(f["name"])
    # Phiên bản này có hiệu lực từ lúc file được lưu. (Ngày "created" trong Office hay bị kế thừa từ template cũ.)
    default_from = f["fs_modified_at"]
    now = datetime.now().isoformat(timespec="seconds")
    seen = set()
    for it in items:
        sk, pk, vk = norm(it["subject"]), it["predicate"], norm_value(it["value"])
        if (sk, pk, vk) in seen or vk == sk:  # bỏ trùng và fact vô nghĩa kiểu "X thuộc X"
            continue
        seen.add((sk, pk, vk))
        conn.execute("""INSERT INTO facts (subject, predicate, value, unit, subject_key, predicate_key, value_key,
                        evidence, source_file_id, source_sha256, author, valid_from, base_confidence, confidence,
                        status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'candidate', ?)""",
                     (it["subject"].strip(), pk, str(it["value"]).strip(), (it.get("unit") or "").strip() or None,
                      sk, pk, vk, (it.get("evidence") or "")[:300], f["file_id"], f["sha256"],
                      f["doc_last_editor"] or f["doc_author"] or f["fs_owner"],
                      _valid_date(it.get("date")) or default_from, base, base, now))
        affected.add((sk, pk))
    conn.execute("UPDATE files SET facts_sha256=? WHERE file_id=?", (f["sha256"], f["file_id"]))
    return affected


# ---------- giải quyết mâu thuẫn / phiên bản ----------
def _status(conf: float) -> str:
    return "trusted" if conf >= TRUSTED_AT else "usable" if conf >= USABLE_AT else "candidate"


def resolve_group(conn, subject_key: str, predicate_key: str):
    """Tính lại trạng thái cho mọi fact cùng (subject, predicate). Thuần xác định + idempotent."""
    rows = conn.execute("""SELECT x.*, f.name AS file_name FROM facts x JOIN files f ON f.file_id = x.source_file_id
                           WHERE x.subject_key=? AND x.predicate_key=? AND x.status != 'rejected'
                           ORDER BY x.valid_from, x.fact_id""", (subject_key, predicate_key)).fetchall()
    if not rows:
        return
    single = PREDICATES.get(predicate_key, (False, ""))[0]
    upd = {r["fact_id"]: {"status": None, "valid_to": None, "note": None, "confidence": r["base_confidence"]}
           for r in rows}

    # Đồng thuận: cùng giá trị từ nhiều nguồn khác nhau (khác nội dung file) => tăng tin cậy
    sources_by_value: dict[str, set] = {}
    for r in rows:
        sources_by_value.setdefault(r["value_key"], set()).add(r["source_sha256"])
    for r in rows:
        n = len(sources_by_value[r["value_key"]])
        upd[r["fact_id"]]["confidence"] = min(0.99, r["base_confidence"] + 0.05 * (n - 1))

    if single:
        approved = [r for r in rows if r["status"] == "approved"]
        if approved:
            ref = approved[-1]
            for r in rows:
                if r["status"] == "approved" or r["value_key"] == ref["value_key"]:
                    continue
                if (r["valid_from"] or "") < (ref["valid_from"] or ""):
                    upd[r["fact_id"]].update(status="superseded", valid_to=ref["valid_from"],
                                             note=f"Đã duyệt giá trị mới: {ref['value']}")
                else:
                    upd[r["fact_id"]].update(status="conflicted", note=f"Khác giá trị đã duyệt ({ref['value']})")
        else:
            # Trong cùng một họ tài liệu (các phiên bản của 1 file): bản mới thay bản cũ => không phải mâu thuẫn
            latest_by_family: dict[str, str] = {}
            for r in rows:
                fam = file_family(r["file_name"])
                latest_by_family[fam] = max(latest_by_family.get(fam, ""), r["valid_from"] or "")
            current = []
            for r in rows:
                fam = file_family(r["file_name"])
                newest = latest_by_family[fam]
                if (r["valid_from"] or "") < newest:
                    newer_vals = {x["value_key"] for x in rows
                                  if file_family(x["file_name"]) == fam and (x["valid_from"] or "") == newest}
                    if r["value_key"] not in newer_vals:
                        upd[r["fact_id"]].update(status="superseded", valid_to=newest,
                                                 note="Phiên bản tài liệu mới hơn ghi giá trị khác")
                        continue
                current.append(r)
            keys = sorted({r["value_key"] for r in current})
            incompatible = any(not _compatible(a, b) for i, a in enumerate(keys) for b in keys[i + 1:])
            # Nhiều giá trị nhưng từ cùng 1 nguồn => thường do subject mơ hồ, không phải các nguồn mâu thuẫn
            if incompatible and len({r["source_sha256"] for r in current}) > 1:
                vals = "; ".join(sorted({f"{r['value']} ({r['file_name']})" for r in current}))
                for r in current:
                    u = upd[r["fact_id"]]
                    u.update(status="conflicted", note=f"Các nguồn hiện hành không thống nhất: {vals}",
                             confidence=max(0.05, u["confidence"] - 0.2))

    for r in rows:
        if r["status"] == "approved":
            continue
        u = upd[r["fact_id"]]
        conn.execute("UPDATE facts SET status=?, valid_to=?, note=?, confidence=? WHERE fact_id=?",
                     (u["status"] or _status(u["confidence"]), u["valid_to"], u["note"],
                      round(u["confidence"], 3), r["fact_id"]))


def _drop_deleted_sources(conn) -> set:
    affected = {(r["subject_key"], r["predicate_key"]) for r in conn.execute(
        """SELECT DISTINCT x.subject_key, x.predicate_key FROM facts x JOIN files f ON f.file_id=x.source_file_id
           WHERE f.status='deleted' AND x.status NOT IN ('approved')""")}
    conn.execute("""DELETE FROM facts WHERE status != 'approved' AND source_file_id IN
                    (SELECT file_id FROM files WHERE status='deleted')""")
    return affected


def _run(file_rows, conn, progress=None, throttle=None) -> dict:
    affected, stats = _drop_deleted_sources(conn), {"files": 0, "errors": 0}
    conn.commit()  # không giữ khóa ghi trong lúc throttle chờ máy rảnh
    for n, f in enumerate(file_rows, 1):
        if throttle:
            throttle()
        if progress:
            progress(f"Trích fact {n}/{len(file_rows)}: {f['name']}")
        try:
            with _file_lock:
                affected |= _extract_file(conn, f)
            stats["files"] += 1
        except Exception as e:
            stats["errors"] += 1
            conn.execute("UPDATE files SET facts_sha256=? WHERE file_id=?", (f["sha256"], f["file_id"]))
            if progress:
                progress(f"Lỗi trích fact {f['name']}: {type(e).__name__}")
        conn.commit()
    for sk, pk in affected:
        resolve_group(conn, sk, pk)
    conn.commit()
    stats["groups_resolved"] = len(affected)
    return stats


def _pending(conn, file_ids=None):
    # chỉ tài liệu bậc 1 nằm trong FACT_ROOTS (lọc ngay trong SQL — không nạp hàng trăm nghìn dòng lên Python)
    roots = [str(Path(r).resolve()).replace("'", "''") for r in FACT_ROOTS]
    like = " OR ".join(f"path LIKE '{r}%'" for r in roots) or "0"
    sql = f"""SELECT * FROM files WHERE status='active' AND extract_error IS NULL AND tier=1 AND ({like})
             AND (facts_sha256 IS NULL OR facts_sha256 != sha256)"""
    if file_ids is not None:
        sql += f" AND file_id IN ({','.join(str(int(i)) for i in file_ids) or 'NULL'})"
    return [f for f in conn.execute(sql).fetchall() if in_scope(f["path"], f["ext"])]


def extract_facts_pending(progress=None, throttle=None) -> dict:
    conn = connect()
    try:
        return _run(_pending(conn), conn, progress, throttle)
    finally:
        conn.close()


def extract_facts_for(file_ids: list[int]) -> dict:
    conn = connect()
    try:
        return _run(_pending(conn, file_ids), conn)
    finally:
        conn.close()


def review(fact_id: int, action: str, reviewer: str, note: str | None = None) -> bool:
    """Con người duyệt: approve => giá trị chuẩn (authoritative); reject => loại bỏ."""
    if action not in ("approve", "reject"):
        return False
    conn = connect()
    r = conn.execute("SELECT subject_key, predicate_key FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
    if not r:
        conn.close()
        return False
    conn.execute("""UPDATE facts SET status=?, reviewed_by=?, reviewed_at=?, note=COALESCE(?, note),
                    confidence=CASE WHEN ?='approve' THEN 1.0 ELSE confidence END WHERE fact_id=?""",
                 ("approved" if action == "approve" else "rejected", reviewer,
                  datetime.now().isoformat(timespec="seconds"), note, action, fact_id))
    resolve_group(conn, r["subject_key"], r["predicate_key"])
    conn.commit()
    conn.close()
    return True
