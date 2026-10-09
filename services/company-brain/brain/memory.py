"""Bộ nhớ kinh nghiệm: bot khôn dần qua từng lần chat mà KHÔNG sửa model.

Nguồn học:
- 👎 + góp ý  -> "lesson"     (lần trước sai ở đâu, đúng là gì)
- 👍          -> "example"    (câu hỏi kiểu này đã giải đúng bằng cách nào)
- Trong hội thoại (tự phát hiện sau mỗi lượt):
    người dùng sửa ("không phải, ý tôi là...")      -> "lesson"
    người dùng xác nhận đoán của bot ("đúng rồi")   -> "alias"  (vd. "app vẽ kỹ thuật" = AutoCAD)
    người dùng dặn ("từ giờ...", "nhớ là...")        -> "preference"
Dùng: mỗi câu hỏi mới, lấy các kinh nghiệm gần nghĩa nhất đưa vào ngữ cảnh cho model.
Mọi kinh nghiệm xem/xóa được trên web (tab Bộ nhớ)."""
import json
import threading
from datetime import datetime

import numpy as np
import requests

from .catalog import connect
from .config import ADMIN_GROUP, CHAT_KEEP_ALIVE, CHAT_MODEL, LLM_OPTIONS, OLLAMA_URL
from .index import embed

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    mem_id     INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,        -- lesson | example | alias | preference
    trigger    TEXT NOT NULL,        -- câu/cụm dùng để so khớp
    content    TEXT NOT NULL,        -- điều đã học (đưa nguyên văn vào ngữ cảnh)
    vector     TEXT,                 -- embedding của trigger (JSON)
    owner      TEXT,
    scope      TEXT NOT NULL,        -- nhóm được dùng kinh nghiệm này (giống ACL file)
    source     TEXT,                 -- feedback:qa_id | chat:qa_id | manual
    uses       INTEGER DEFAULT 0,
    last_used  TEXT,
    active     INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);
"""
MIN_SIM = 0.62
_lock = threading.Lock()


def _conn():
    c = connect()
    c.executescript(SCHEMA)
    return c


def _scope(groups: list[str]) -> str:
    # Kinh nghiệm học từ admin có thể dính dữ liệu riêng tư của máy => chỉ admin dùng lại
    return ADMIN_GROUP if ADMIN_GROUP in groups else ",".join(sorted(set(groups) - {"all"}) or ["all"])


def _visible(scope: str, groups: list[str]) -> bool:
    return ADMIN_GROUP in groups or bool(set(scope.split(",")) & {*groups, "all"})


def add(kind: str, trigger: str, content: str, owner: str, groups: list[str], source: str) -> int | None:
    trigger, content = trigger.strip()[:500], content.strip()[:1500]
    if not trigger or not content:
        return None
    vec = embed([trigger])[0]
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        c = _conn()
        # Trùng ý với kinh nghiệm cũ cùng loại (rất giống) => cập nhật thay vì thêm mới
        for r in c.execute("SELECT mem_id, vector FROM memories WHERE active=1 AND kind=? AND scope=?",
                           (kind, _scope(groups))).fetchall():
            if r["vector"] and _cos(vec, json.loads(r["vector"])) > 0.93:
                c.execute("UPDATE memories SET content=?, trigger=?, vector=?, source=?, created_at=? WHERE mem_id=?",
                          (content, trigger, json.dumps(vec), source, now, r["mem_id"]))
                c.commit()
                c.close()
                return r["mem_id"]
        cur = c.execute("""INSERT INTO memories (kind, trigger, content, vector, owner, scope, source, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (kind, trigger, content, json.dumps(vec), owner, _scope(groups), source, now))
        c.commit()
        mid = cur.lastrowid
        c.close()
    return mid


def _cos(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def recall(question: str, groups: list[str], k: int = 6) -> list[dict]:
    """Các kinh nghiệm liên quan nhất tới câu hỏi (đã lọc quyền)."""
    c = _conn()
    rows = [r for r in c.execute("SELECT * FROM memories WHERE active=1").fetchall()
            if r["vector"] and _visible(r["scope"], groups)]
    if not rows:
        c.close()
        return []
    q = np.asarray(embed([question], query=True)[0], dtype=np.float32)
    m = np.asarray([json.loads(r["vector"]) for r in rows], dtype=np.float32)
    sims = (m @ q) / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-9)
    # sở thích (preference) luôn áp dụng với người đó dù không gần nghĩa
    picked = [(float(s), r) for s, r in zip(sims, rows) if s >= MIN_SIM or r["kind"] == "preference"]
    picked.sort(key=lambda x: -x[0])
    out = []
    now = datetime.now().isoformat(timespec="seconds")
    for s, r in picked[:k]:
        out.append({"mem_id": r["mem_id"], "kind": r["kind"], "content": r["content"], "sim": round(s, 2)})
        c.execute("UPDATE memories SET uses=uses+1, last_used=? WHERE mem_id=?", (now, r["mem_id"]))
    c.commit()
    c.close()
    return out


def as_context(mems: list[dict]) -> str:
    label = {"lesson": "BÀI HỌC (lần trước sai)", "example": "CÁCH ĐÃ LÀM ĐÚNG", "alias": "NGƯỜI DÙNG GỌI LÀ",
             "preference": "NGƯỜI DÙNG DẶN"}
    lines = [f"- [{label.get(m['kind'], m['kind'])}] {m['content']}" for m in mems]
    return ("KINH NGHIỆM ĐÃ HỌC từ các lần chat trước (ưu tiên áp dụng; nếu mâu thuẫn với dữ liệu mới nhất "
            "thì tin dữ liệu):\n" + "\n".join(lines))


# ---------- học từ đánh giá ----------
def learn_from_feedback(qa: dict, rating: int, comment: str | None, groups: list[str]):
    q, ans = qa["question"], (qa["answer"] or "")[:400]
    if rating == -1 and comment:
        add("lesson", q, f"Hỏi: \"{q}\". Lần trước trả lời chưa đúng (\"{ans[:200]}\"). Người dùng sửa: {comment}",
            qa["user"], groups, f"feedback:{qa['qa_id']}")
    elif rating == 1:
        steps = json.loads(qa["steps"] or "[]")
        tools = [s for s in steps if not s.startswith("prefetch")]
        how = ("Cách làm: " + "; ".join(tools)[:500]) if tools else "Trả lời từ kết quả tìm sơ bộ."
        add("example", q, f"Hỏi: \"{q}\" -> đã trả lời đúng. {how} Kết quả: {ans[:300]}",
            qa["user"], groups, f"feedback:{qa['qa_id']}")


# ---------- tự học trong hội thoại ----------
DETECT_PROMPT = """Phân tích lượt hội thoại sau giữa NGƯỜI DÙNG và TRỢ LÝ.
Lượt trước - người dùng hỏi: "{prev_q}"
Lượt trước - trợ lý trả lời: "{prev_a}"
Tin nhắn mới của người dùng: "{new_q}"

Tin nhắn mới có phải là một trong các loại sau không?
- correction: người dùng chỉ ra trợ lý hiểu sai/trả lời sai và nói điều đúng (vd. "không phải", "sai rồi", "ý tôi là...").
- alias: người dùng xác nhận một cách gọi tắt/mô tả của họ tương ứng với một thứ cụ thể (vd. trợ lý hỏi "AutoCAD phải không?" và người dùng "đúng rồi"/"ừ mở đi").
- preference: người dùng dặn cách làm từ nay về sau (vd. "từ giờ trả lời ngắn thôi", "nhớ là dự án X do Ly phụ trách").
- none: không thuộc loại nào.
Nếu không phải none, viết "memory": MỘT câu kinh nghiệm ngắn, cụ thể, tự hiểu được mà không cần đọc hội thoại
(vd. "Khi người dùng nói 'app vẽ bản vẽ kỹ thuật' nghĩa là AutoCAD 2023 - mở luôn, không cần hỏi lại."),
và "trigger": cụm từ người dùng hay dùng để nói về điều đó."""

DETECT_SCHEMA = {"type": "object", "properties": {
    "type": {"type": "string", "enum": ["correction", "alias", "preference", "none"]},
    "memory": {"type": "string"}, "trigger": {"type": "string"}}, "required": ["type"]}


def learn_from_turn(history: list[dict], new_q: str, user: str, groups: list[str], qa_id: int | None):
    """Chạy nền sau khi trả lời: xem tin nhắn mới có phải sửa sai / xác nhận / dặn dò không."""
    prev_q = next((h["content"] for h in reversed(history) if h.get("role") == "user"), None)
    prev_a = next((h["content"] for h in reversed(history) if h.get("role") == "assistant"), None)
    if not prev_q or not prev_a or len(new_q) > 400:
        return
    try:
        from . import llm
        r = llm.chat([{"role": "user", "content": DETECT_PROMPT.format(
            prev_q=prev_q[:500], prev_a=prev_a[:800], new_q=new_q)}], role="extract", fmt=DETECT_SCHEMA,
            temperature=0, timeout=300)
        out = json.loads(r["message"]["content"])
    except (llm.LLMError, ValueError, KeyError):
        return
    kind = {"correction": "lesson", "alias": "alias", "preference": "preference"}.get(out.get("type"))
    if kind and out.get("memory"):
        add(kind, out.get("trigger") or prev_q, out["memory"], user, groups, f"chat:{qa_id}")


def learn_async(*args):
    threading.Thread(target=learn_from_turn, args=args, daemon=True, name="learn-turn").start()


# ---------- quản lý ----------
def list_for(groups: list[str], user: str) -> list[dict]:
    c = _conn()
    rows = [dict(r) for r in c.execute("""SELECT mem_id, kind, trigger, content, owner, scope, source, uses,
                                          last_used, active, created_at FROM memories ORDER BY mem_id DESC""")
            if ADMIN_GROUP in groups or r["owner"] == user]
    c.close()
    return rows


def set_active(mem_id: int, active: bool, groups: list[str], user: str) -> bool:
    c = _conn()
    r = c.execute("SELECT owner FROM memories WHERE mem_id=?", (mem_id,)).fetchone()
    ok = bool(r) and (ADMIN_GROUP in groups or r["owner"] == user)
    if ok:
        c.execute("UPDATE memories SET active=? WHERE mem_id=?", (1 if active else 0, mem_id))
        c.commit()
    c.close()
    return ok
