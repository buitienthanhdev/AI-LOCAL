"""Vòng B — học vào model: sinh dataset huấn luyện từ tri thức ĐÃ KIỂM CHỨNG (không từ LLM tự tin).

Nguồn: (1) tri thức approved/trusted từ file không riêng tư; (2) hỏi đáp được 👍; (3) câu bị 👎 kèm lời sửa (đánh dấu
cần duyệt); (4) mẫu "chưa biết" tự sinh để dạy model KHÔNG bịa. Lọc: khử trùng lặp, bỏ mâu thuẫn/độ tin cậy thấp, bỏ dữ
liệu nhạy cảm (lương, mật khẩu...), che email/số điện thoại. Dataset có phiên bản + manifest + băm + cổng chất lượng.
Không bao giờ tự train: xem train.py (chỉ chạy khi dataset đạt cổng và máy đủ điều kiện)."""
import hashlib
import json
import random
import re
from datetime import datetime

from .catalog import connect
from .config import DATA_DIR
from .facts import norm

MIN_EXAMPLES = 200        # cổng chất lượng: dưới mức này KHÔNG được train
MIN_CONFIDENCE = 0.8
HOLDOUT = 0.1             # 10% làm tập đánh giá, chọn theo băm (ổn định giữa các lần build)
SENSITIVE = re.compile(r"lương|salary|mật khẩu|password|passwd|token|api[_ -]?key|số tài khoản|cmnd|cccd|thẻ tín dụng", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(?:\+?84|0)\d{9,10}(?!\d)")
LABEL = {
    "target_ph": "pH mục tiêu", "measured_ph": "pH đo được", "target_viscosity": "độ nhớt mục tiêu",
    "measured_viscosity": "độ nhớt đo được", "supplier": "nhà cung cấp", "material_percent": "tỉ lệ phần trăm",
    "uses_material": "nguyên liệu sử dụng", "performed_by": "người thực hiện", "root_cause": "nguyên nhân gốc",
    "corrective_action": "biện pháp khắc phục", "decision": "quyết định", "cost_target": "mục tiêu chi phí",
    "test_result": "kết quả thử nghiệm", "belongs_to_project": "thuộc dự án",
}
SYSTEM = "Bạn là trợ lý tri thức nội bộ công ty. Chỉ trả lời dựa trên bằng chứng; chưa có bằng chứng thì nói chưa tìm thấy."


def scrub(text: str) -> str:
    return PHONE.sub("[SĐT]", EMAIL.sub("[email]", text or ""))


def _ok(*texts) -> bool:
    return not any(SENSITIVE.search(t or "") for t in texts)


def _example(q: str, a: str, source: str, kind: str, evidence: str = "", **meta) -> dict:
    return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scrub(q)},
                         {"role": "assistant", "content": scrub(a)}], "kind": kind, "source": source,
            "evidence": scrub(evidence)[:400], **meta}


def collect(conn) -> tuple[list[dict], dict]:
    out, stats = [], {"fact_pairs": 0, "unknown_pairs": 0, "thumbs_up": 0, "corrections": 0, "dropped_sensitive": 0,
                      "dropped_low_quality": 0}
    # (1) tri thức đã kiểm chứng
    facts = conn.execute("""SELECT x.*, f.name AS fname FROM facts x JOIN files f ON f.file_id=x.source_file_id
                            WHERE x.status IN ('approved','trusted') AND f.acl!='private' AND f.status='active'
                            AND x.confidence>=? AND x.valid_to IS NULL""", (MIN_CONFIDENCE,)).fetchall()
    known = set()
    for x in facts:
        known.add((x["subject_key"], x["predicate_key"]))
        if not _ok(x["subject"], x["value"], x["evidence"]):
            stats["dropped_sensitive"] += 1
            continue
        lab = LABEL.get(x["predicate"], x["predicate"])
        unit = f" {x['unit']}" if x["unit"] else ""
        status = "đã được duyệt" if x["status"] == "approved" else "tin cậy"
        a = f"{lab.capitalize()} của {x['subject']} là {x['value']}{unit}. (Nguồn: {x['fname']}, hiệu lực từ {(x['valid_from'] or '?')[:10]}; " \
            f"độ tin cậy: {status}.)"
        out.append(_example(f"{lab.capitalize()} của {x['subject']} là gì?", a, x["fname"], "fact", x["evidence"],
                            fact_id=x["fact_id"]))
        stats["fact_pairs"] += 1
    # (4) mẫu "chưa biết": hỏi tổ hợp (chủ thể, quan hệ) KHÔNG có trong kho => dạy từ chối thay vì bịa
    subjects = sorted({(x["subject"], x["subject_key"]) for x in facts})
    preds = sorted({(x["predicate"], x["predicate_key"]) for x in facts})
    rnd = random.Random(7)
    target = max(0, int(stats["fact_pairs"] * 0.15))
    tries = 0
    while stats["unknown_pairs"] < target and tries < target * 20 and subjects and preds:
        tries += 1
        (s, sk), (p, pk) = rnd.choice(subjects), rnd.choice(preds)
        if (sk, pk) in known:
            continue
        known.add((sk, pk))
        lab = LABEL.get(p, p)
        out.append(_example(f"{lab.capitalize()} của {s} là gì?",
                            f"Mình chưa tìm thấy thông tin về {lab} của {s} trong tài liệu. Chưa đủ bằng chứng để khẳng định.",
                            "synthetic-unknown", "unknown"))
        stats["unknown_pairs"] += 1
    # (2)(3) hỏi đáp có đánh giá
    for r in conn.execute("SELECT * FROM qa_log WHERE rating IS NOT NULL").fetchall():
        srcs = json.loads(r["sources"] or "[]")
        if srcs:
            ids = [s["file_id"] for s in srcs if s.get("file_id")]
            priv = conn.execute(f"SELECT COUNT(*) FROM files WHERE file_id IN ({','.join('?' * len(ids)) or 'NULL'}) "
                                f"AND acl='private'", ids).fetchone()[0]
            if priv:
                stats["dropped_low_quality"] += 1
                continue
        if not _ok(r["question"], r["answer"], r["comment"]):
            stats["dropped_sensitive"] += 1
            continue
        if r["rating"] == 1 and r["answer"]:
            out.append(_example(r["question"], r["answer"], f"qa#{r['qa_id']}", "thumbs_up"))
            stats["thumbs_up"] += 1
        elif r["rating"] == -1 and r["comment"]:
            out.append(_example(r["question"], r["comment"], f"qa#{r['qa_id']}", "correction", needs_review=True))
            stats["corrections"] += 1
    return out, stats


def build_dataset(out_root=None) -> dict:
    """Sinh dataset có phiên bản. Trả về manifest (kèm cổng chất lượng)."""
    conn = connect(readonly=True)
    examples, stats = collect(conn)
    conn.close()
    # khử trùng lặp (theo câu hỏi + trả lời chuẩn hóa); bản "cần duyệt" không vào tập train tự động
    seen, uniq = set(), []
    for e in examples:
        k = hashlib.sha1((norm(e["messages"][1]["content"]) + "|" + norm(e["messages"][2]["content"])).encode()).hexdigest()
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    review = [e for e in uniq if e.get("needs_review")]
    clean = [e for e in uniq if not e.get("needs_review")]
    train, evalset = [], []
    for e in clean:
        h = int(hashlib.sha1(e["messages"][1]["content"].encode()).hexdigest(), 16) % 100
        (evalset if h < HOLDOUT * 100 else train).append(e)
    root = out_root or (DATA_DIR / "datasets")
    root.mkdir(parents=True, exist_ok=True)
    n = len([p for p in root.iterdir() if p.is_dir()]) + 1
    version = f"v{n}-{datetime.now():%Y%m%d-%H%M}"
    d = root / version
    d.mkdir()
    files = {}
    for name, rows in (("train.jsonl", train), ("eval.jsonl", evalset), ("needs_review.jsonl", review)):
        data = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        (d / name).write_text(data, encoding="utf-8")
        files[name] = {"count": len(rows), "sha256": hashlib.sha256(data.encode()).hexdigest()}
    manifest = {"version": version, "created_at": datetime.now().isoformat(timespec="seconds"), "files": files,
                "sources": stats, "total_unique": len(uniq),
                "gate": {"min_examples": MIN_EXAMPLES, "train_count": len(train), "pass": len(train) >= MIN_EXAMPLES,
                         "reason": None if len(train) >= MIN_EXAMPLES else
                         f"mới {len(train)} mẫu train, cần ≥ {MIN_EXAMPLES} — chưa đủ để train có ý nghĩa"},
                "filters": {"min_confidence": MIN_CONFIDENCE, "private_files_excluded": True,
                            "sensitive_regex": SENSITIVE.pattern, "scrub": ["email", "phone"]}}
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def list_datasets() -> list[dict]:
    root = DATA_DIR / "datasets"
    out = []
    for d in sorted(root.glob("v*")) if root.exists() else []:
        try:
            out.append(json.loads((d / "manifest.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


def export_dataset() -> dict:
    """Tương thích lệnh cũ: dựng dataset mới và trả về manifest."""
    return build_dataset()
