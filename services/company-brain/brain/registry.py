"""Model registry + router + vòng đời triển khai.

Vai trò (role): chat (hỏi đáp/agent) · extract (trích tri thức) · vision (đọc ảnh/audio/video).
Vòng đời: candidate -> benchmarked -> canary (một phần lưu lượng) -> production -> (rollback về bản trước).
Không bao giờ đưa model thẳng vào production: phải qua benchmark và không tệ hơn bản đang chạy.
Mọi chuyển trạng thái được ghi vào `deployments` + `audit` để truy vết và hoàn tác."""
import json
import random
import threading
import time
from datetime import datetime

import requests

from . import obs
from .catalog import connect
from .config import CHAT_MODEL, MEDIA_MODEL, OLLAMA_URL

ROLES = ("chat", "extract", "vision")
DEFAULTS = {"chat": CHAT_MODEL, "extract": CHAT_MODEL, "vision": MEDIA_MODEL}
BENCH_TOLERANCE = 0.02       # candidate chỉ được lên canary nếu điểm >= điểm production - 2%
CANARY_MIN_REQUESTS = 12     # số yêu cầu tối thiểu trước khi kết luận canary
CANARY_MAX_BAD_DELTA = 0.15  # tỉ lệ lỗi/đánh giá xấu của canary vượt production quá mức này => rollback
CANARY_MAX_LAT_RATIO = 1.8   # chậm hơn production > 1,8 lần => rollback

_cache: dict = {"at": 0.0, "rows": []}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def installed_models() -> list[dict]:
    """Model đang có trong Ollama (tên, cỡ, lượng tử hóa)."""
    try:
        data = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])
    except requests.RequestException:
        return []
    return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
             "params": m.get("details", {}).get("parameter_size"), "quant": m.get("details", {}).get("quantization_level"),
             "family": m.get("details", {}).get("family")} for m in data]


def bootstrap():
    """Đăng ký model mặc định làm production của vai trò 'chat' nếu registry còn trống. Các vai trò khác (extract, vision)
    tự dùng model của 'chat' cho tới khi được đăng ký model riêng (một tên model chỉ thuộc một vai trò)."""
    conn = connect()
    if not conn.execute("SELECT 1 FROM models WHERE role='chat' AND status IN ('production','canary')").fetchone():
        conn.execute("""INSERT INTO models (name, role, status, notes, created_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(name) DO UPDATE SET role='chat', status='production'""",
                     (DEFAULTS["chat"], "chat", "production", "mặc định ban đầu", _now()))
    conn.commit()
    conn.close()
    with _lock:
        _cache["at"] = 0


def _rows():
    with _lock:
        if time.time() - _cache["at"] > 5:
            conn = connect()
            _cache["rows"] = [dict(r) for r in conn.execute(
                "SELECT * FROM models WHERE status IN ('production','canary')")]
            conn.close()
            _cache["at"] = time.time()
        return _cache["rows"]


def pick(role: str = "chat") -> str:
    """Model dùng cho một yêu cầu: canary (theo tỉ lệ) hoặc production hoặc mặc định cấu hình."""
    rows = [r for r in _rows() if r["role"] == role] or [r for r in _rows() if r["role"] == "chat"]
    canary = [r for r in rows if r["status"] == "canary" and r["canary_percent"]]
    for c in canary:
        if random.random() * 100 < c["canary_percent"]:
            return c["name"]
    prod = [r for r in rows if r["status"] == "production"]
    return prod[0]["name"] if prod else DEFAULTS.get(role, CHAT_MODEL)


def production(role: str = "chat") -> str:
    prod = [r for r in _rows() if r["role"] == role and r["status"] == "production"] or \
           [r for r in _rows() if r["role"] == "chat" and r["status"] == "production"]
    return prod[0]["name"] if prod else DEFAULTS.get(role, CHAT_MODEL)


def _log(conn, role, action, frm, to, reason, actor="system"):
    conn.execute("INSERT INTO deployments (role, action, from_model, to_model, reason, at) VALUES (?,?,?,?,?,?)",
                 (role, action, frm, to, reason, _now()))
    conn.execute("INSERT INTO audit (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                 (_now(), actor, f"model_{action}", to or frm, f"{role}: {frm} -> {to} · {reason}"))
    _cache["at"] = 0


def register(name: str, role: str = "chat", base_model: str | None = None, quant: str | None = None,
             parent: str | None = None, dataset_version: str | None = None, notes: str | None = None) -> dict:
    """Đăng ký model mới ở trạng thái 'candidate' (chưa nhận lưu lượng)."""
    conn = connect()
    conn.execute("""INSERT INTO models (name, role, base_model, quant, parent, dataset_version, notes, status, created_at)
                    VALUES (?,?,?,?,?,?,?,'candidate',?) ON CONFLICT(name) DO UPDATE SET
                    role=excluded.role, notes=COALESCE(excluded.notes, notes)""",
                 (name, role, base_model, quant, parent, dataset_version, notes, _now()))
    _log(conn, role, "register", None, name, notes or "đăng ký candidate")
    conn.commit()
    conn.close()
    return get(name)


def get(name: str) -> dict | None:
    conn = connect()
    r = conn.execute("SELECT * FROM models WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(r) if r else None


def list_models() -> list[dict]:
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM models ORDER BY role, status='production' DESC, created_at DESC")]
    conn.close()
    return rows


def record_bench(model: str, suite: str, score: float, detail: dict, p50: float, tps: float):
    conn = connect()
    conn.execute("""INSERT INTO bench_runs (model, suite, score, detail, latency_p50, tokens_per_s, created_at)
                    VALUES (?,?,?,?,?,?,?)""", (model, suite, score, json.dumps(detail, ensure_ascii=False), p50, tps, _now()))
    conn.execute("UPDATE models SET score=?, status=CASE WHEN status='candidate' THEN 'benchmarked' ELSE status END "
                 "WHERE name=?", (score, model))
    conn.commit()
    conn.close()


def start_canary(name: str, percent: int = 10, actor: str = "system") -> dict:
    """benchmarked -> canary. Điều kiện: đã benchmark và điểm không tệ hơn production quá BENCH_TOLERANCE."""
    m = get(name)
    if not m:
        return {"ok": False, "error": "model chưa đăng ký"}
    if m["score"] is None:
        return {"ok": False, "error": "chưa benchmark — không được lên canary"}
    prod = [r for r in list_models() if r["role"] == m["role"] and r["status"] == "production"]
    if prod and prod[0]["score"] is not None and m["score"] < prod[0]["score"] - BENCH_TOLERANCE:
        return {"ok": False, "error": f"điểm {m['score']:.3f} thấp hơn production {prod[0]['score']:.3f} — bị từ chối"}
    conn = connect()
    conn.execute("UPDATE models SET status='canary', canary_percent=? WHERE name=?", (max(1, min(50, percent)), name))
    _log(conn, m["role"], "canary", prod[0]["name"] if prod else None, name, f"{percent}% lưu lượng", actor)
    conn.commit()
    conn.close()
    return {"ok": True, "status": "canary", "percent": percent}


def promote(name: str, actor: str = "system", reason: str = "đạt canary") -> dict:
    """canary/benchmarked -> production; production cũ -> retired (giữ để rollback)."""
    m = get(name)
    if not m or m["status"] not in ("canary", "benchmarked"):
        return {"ok": False, "error": "chỉ promote model đã benchmark hoặc đang canary"}
    conn = connect()
    old = conn.execute("SELECT name FROM models WHERE role=? AND status='production'", (m["role"],)).fetchone()
    conn.execute("UPDATE models SET status='retired', canary_percent=0 WHERE role=? AND status='production'", (m["role"],))
    conn.execute("UPDATE models SET status='production', canary_percent=0 WHERE name=?", (name,))
    _log(conn, m["role"], "promote", old[0] if old else None, name, reason, actor)
    conn.commit()
    conn.close()
    return {"ok": True, "from": old[0] if old else None, "to": name}


def rollback(role: str = "chat", actor: str = "system", reason: str = "regression") -> dict:
    """Hoàn tác: model production (hoặc canary) hiện tại -> retired; bản production trước đó được khôi phục."""
    conn = connect()
    last = conn.execute("""SELECT * FROM deployments WHERE role=? AND action='promote' ORDER BY dep_id DESC LIMIT 1""",
                        (role,)).fetchone()
    canary = conn.execute("SELECT name FROM models WHERE role=? AND status='canary'", (role,)).fetchone()
    if canary:  # đang canary => chỉ cần dừng canary, production giữ nguyên
        conn.execute("UPDATE models SET status='retired', canary_percent=0 WHERE name=?", (canary[0],))
        _log(conn, role, "rollback", canary[0], production(role), reason, actor)
        conn.commit()
        conn.close()
        return {"ok": True, "stopped_canary": canary[0]}
    if not last or not last["from_model"]:
        conn.close()
        return {"ok": False, "error": "không có bản trước để quay lại"}
    cur = conn.execute("SELECT name FROM models WHERE role=? AND status='production'", (role,)).fetchone()
    conn.execute("UPDATE models SET status='retired' WHERE role=? AND status='production'", (role,))
    conn.execute("UPDATE models SET status='production' WHERE name=?", (last["from_model"],))
    _log(conn, role, "rollback", cur[0] if cur else None, last["from_model"], reason, actor)
    conn.commit()
    conn.close()
    return {"ok": True, "restored": last["from_model"]}


def canary_stats(role: str = "chat", since_days: int = 3) -> dict:
    """So sánh thực tế canary vs production từ nhật ký hỏi đáp (qa_log): số yêu cầu, độ trễ, tỉ lệ 👎/lỗi."""
    conn = connect()
    since = datetime.fromtimestamp(time.time() - since_days * 86400).isoformat(timespec="seconds")
    out = {}
    for r in conn.execute("SELECT name, status FROM models WHERE role=? AND status IN ('production','canary')", (role,)):
        rows = conn.execute("""SELECT latency_s, rating, answer FROM qa_log WHERE model=? AND created_at>=?""",
                            (r["name"], since)).fetchall()
        n = len(rows)
        bad = sum(1 for x in rows if x["rating"] == -1 or (x["answer"] or "").startswith("Lỗi"))
        lat = sorted(x["latency_s"] for x in rows if x["latency_s"])
        out[r["status"]] = {"model": r["name"], "n": n, "bad_rate": round(bad / n, 3) if n else None,
                            "p50": round(lat[len(lat) // 2], 2) if lat else None}
    conn.close()
    return out


def evaluate_canary(role: str = "chat", actor: str = "system") -> dict:
    """Quyết định canary: đủ dữ liệu và không tệ hơn => promote; tệ hơn => rollback; chưa đủ => chờ."""
    st = canary_stats(role)
    if "canary" not in st:
        return {"decision": "none", "reason": "không có canary"}
    c, p = st["canary"], st.get("production", {})
    if c["n"] < CANARY_MIN_REQUESTS:
        return {"decision": "wait", "reason": f"mới {c['n']}/{CANARY_MIN_REQUESTS} yêu cầu", "stats": st}
    base_bad = p.get("bad_rate") or 0.0
    if (c["bad_rate"] or 0) > base_bad + CANARY_MAX_BAD_DELTA:
        r = rollback(role, actor, f"canary tệ hơn: lỗi/👎 {c['bad_rate']} vs {base_bad}")
        return {"decision": "rollback", "reason": "tỉ lệ lỗi/đánh giá xấu cao hơn production", "result": r, "stats": st}
    if p.get("p50") and c["p50"] and c["p50"] > p["p50"] * CANARY_MAX_LAT_RATIO:
        r = rollback(role, actor, f"canary chậm: p50 {c['p50']}s vs {p['p50']}s")
        return {"decision": "rollback", "reason": "chậm hơn production quá mức", "result": r, "stats": st}
    r = promote(c["model"], actor, f"canary đạt trên {c['n']} yêu cầu")
    return {"decision": "promote", "result": r, "stats": st}


def status() -> dict:
    return {"models": list_models(), "installed": installed_models(),
            "production": {r: production(r) for r in ROLES},
            "history": _history()}


def _history(limit=15):
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM deployments ORDER BY dep_id DESC LIMIT ?", (limit,))]
    conn.close()
    return rows
