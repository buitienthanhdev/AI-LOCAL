"""Kỹ năng do người dùng DẠY: một quy trình nhiều bước được đặt tên, lần sau gọi tên là bot làm lại.

Hai cách dạy:
- "lưu cách làm vừa rồi thành kỹ năng 'mở nhạc thiền'" -> lấy đúng các bước (công cụ + tham số) bot vừa làm ở lượt trước;
  có thể biến một phần thành tham số ({bai_hat}) để lần sau thay giá trị khác.
- "dạy kỹ năng 'dọn Downloads': chuyển file .exe trong Downloads sang D:\\Setup" -> lưu HƯỚNG DẪN bằng lời; lúc chạy bot
  tự làm theo hướng dẫn bằng các công cụ hiện có.
Chạy: bước nào lặp lại được nguyên xi (không phụ thuộc kết quả bước trước, vd. file_id) thì chạy thẳng; còn lại bot đọc
hướng dẫn + các bước mẫu rồi tự thực hiện. Việc làm thay đổi máy vẫn phải bấm Thực hiện như bình thường."""
import json
import re
import threading
from datetime import datetime

from .catalog import connect
from .facts import norm

_lock = threading.Lock()
_last_calls: dict[str, list[dict]] = {}   # user -> các bước của lượt hỏi gần nhất (để "lưu cách làm vừa rồi")
NOT_RECORDED = {"save_skill", "run_skill", "manage_skills", "ask_choice", "manage_tasks", "list_apps",
                "manage_open_rules", "file_type_info"}
ID_ARGS = {"file_id", "task_id", "id", "element", "x", "y"}  # số element/tọa độ trên màn hình cũng đổi mỗi lần  # tham số phụ thuộc kết quả lúc đó => không chạy lại nguyên xi được

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id     INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL UNIQUE,
    description  TEXT,
    triggers     TEXT,             -- các cách người dùng hay gọi (phân tách bằng ;)
    params       TEXT,             -- JSON: {"bai_hat": "giá trị mẫu"}
    steps        TEXT,             -- JSON: [{"tool": ..., "args": {...}}] có thể chứa {tham_so}
    instructions TEXT,             -- hướng dẫn bằng lời
    owner        TEXT,
    uses         INTEGER DEFAULT 0,
    last_used    TEXT,
    active       INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL
);
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "save_skill",
        "description": ("DẠY/LƯU KỸ NĂNG khi người dùng nói 'lưu cách làm này thành kỹ năng X', 'dạy kỹ năng X: ...', 'từ giờ "
                        "khi tôi nói X thì làm ...'. from_last_turn=true: lưu đúng các bước vừa làm ở lượt trước. "
                        "parameters: phần nào lần sau sẽ thay đổi, dạng {\"ten_tham_so\": \"giá trị trong lần vừa rồi\"}. "
                        "instructions: hướng dẫn bằng lời (khi dạy bằng mô tả)."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"},
            "triggers": {"type": "array", "items": {"type": "string"}, "description": "các câu người dùng sẽ nói để gọi"},
            "from_last_turn": {"type": "boolean"},
            "parameters": {"type": "object", "description": "{ten_tham_so: giá trị mẫu}"},
            "instructions": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "run_skill",
        "description": ("CHẠY một kỹ năng đã được dạy (xem 'skills_matching' trong KẾT QUẢ TÌM SƠ BỘ). params: giá trị cho các "
                        "tham số của kỹ năng."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "params": {"type": "object"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "manage_skills",
        "description": "Xem hoặc xóa kỹ năng đã dạy. action=list | delete (kèm name).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "delete"]}, "name": {"type": "string"}}, "required": ["action"]}}},
]
NAMES = ("save_skill", "run_skill", "manage_skills")


def _conn():
    c = connect()
    c.executescript(SCHEMA)
    return c


def record_turn(user: str, calls: list[dict]):
    """Gọi sau mỗi lượt hỏi: nhớ các bước đã làm (bỏ các công cụ quản lý)."""
    kept = [c for c in calls if c["tool"] not in NOT_RECORDED]
    if kept:
        _last_calls[user] = kept


def _templ(value, params: dict):
    """Thay giá trị mẫu bằng {tham_so} trong tham số công cụ."""
    if isinstance(value, str):
        for k, v in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
            if v and str(v).lower() in value.lower():
                value = re.sub(re.escape(str(v)), "{" + k + "}", value, flags=re.I)
        return value
    if isinstance(value, dict):
        return {k: _templ(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_templ(v, params) for v in value]
    return value


def _fill(value, params: dict):
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, params) for v in value]
    return value


def save(args: dict, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được dạy kỹ năng."}
    name = (args.get("name") or "").strip().strip("'\"")
    if not name:
        return {"error": "Kỹ năng cần có tên."}
    params = {str(k): str(v) for k, v in (args.get("parameters") or {}).items() if str(k).strip()}
    steps = []
    if args.get("from_last_turn"):
        last = _last_calls.get(user)
        if not last:
            return {"error": "Chưa có bước nào ở lượt trước để lưu. Hãy làm việc đó một lần trước, hoặc dạy bằng instructions."}
        steps = [{"tool": c["tool"], "args": _templ(c["args"], params)} for c in last]
    instructions = (args.get("instructions") or "").strip()
    if not steps and not instructions:
        return {"error": "Cần from_last_turn=true (lưu các bước vừa làm) hoặc instructions (hướng dẫn bằng lời)."}
    triggers = [t for t in (args.get("triggers") or []) if str(t).strip()]
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        c = _conn()
        c.execute("""INSERT INTO skills (name, name_key, description, triggers, params, steps, instructions, owner, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(name_key) DO UPDATE SET name=excluded.name,
                     description=excluded.description, triggers=excluded.triggers, params=excluded.params,
                     steps=excluded.steps, instructions=excluded.instructions, owner=excluded.owner, active=1,
                     created_at=excluded.created_at""",
                  (name, norm(name).strip(), args.get("description") or "", ";".join(triggers),
                   json.dumps(params, ensure_ascii=False), json.dumps(steps, ensure_ascii=False), instructions, user, now))
        c.commit()
        c.close()
    from . import obs
    obs.audit(user, "skill_save", name, json.dumps(steps, ensure_ascii=False)[:500] or instructions[:500])
    return {"saved": name, "steps": [f"{s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:120]})" for s in steps],
            "parameters": list(params), "instructions": instructions[:300],
            "note": f"Đã học kỹ năng '{name}'. Lần sau chỉ cần nói tên hoặc một câu tương tự."}


def get(name: str) -> dict | None:
    c = _conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM skills WHERE active=1")]
    c.close()
    key = norm(name).strip()
    exact = [r for r in rows if r["name_key"] == key]
    if exact:
        return exact[0]
    loose = [r for r in rows if key and (key in r["name_key"] or r["name_key"] in key)]
    return loose[0] if len(loose) == 1 else None


def list_all() -> list[dict]:
    c = _conn()
    rows = [dict(r) | {"params": json.loads(r["params"] or "{}"), "steps": json.loads(r["steps"] or "[]")}
            for r in c.execute("SELECT * FROM skills WHERE active=1 ORDER BY uses DESC, name")]
    c.close()
    return rows


def matching(question: str, limit: int = 3) -> list[dict]:
    """Kỹ năng có tên/câu gọi khớp câu hỏi (đưa vào tìm sơ bộ để model biết mà dùng run_skill)."""
    from .vn import best_similarity
    q = norm(question)
    qt = set(q.split())
    out = []
    for s in list_all():
        names = [s["name"], *[t for t in (s["triggers"] or "").split(";") if t]]
        best = 0.0
        for n in names:
            nk = norm(n).strip()
            toks = set(nk.split())
            if nk and nk in q:
                best = max(best, 1.0)
            elif toks:
                best = max(best, len(toks & qt) / len(toks), best_similarity(question, n) * 0.9 if len(nk) > 3 else 0)
        if best >= 0.6:
            out.append((best, s))
    out.sort(key=lambda x: -x[0])
    return [{"name": s["name"], "description": s["description"], "params": s["params"], "match": round(b, 2)}
            for b, s in out[:limit]]


def run(args: dict, user: str, groups: list[str], is_admin: bool, call_tool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    s = get(args.get("name") or "")
    if not s:
        return {"error": f"Không có kỹ năng '{args.get('name')}'.", "skills": [x["name"] for x in list_all()]}
    params = {**json.loads(s["params"] or "{}"), **{str(k): v for k, v in (args.get("params") or {}).items()}}
    steps = _fill(json.loads(s["steps"] or "[]"), params)
    c = _conn()
    c.execute("UPDATE skills SET uses=uses+1, last_used=? WHERE skill_id=?",
              (datetime.now().isoformat(timespec="seconds"), s["skill_id"]))
    c.commit()
    c.close()
    replayable = steps and not any(set(st["args"]) & ID_ARGS for st in steps) and not s["instructions"]
    if not replayable:  # bot tự làm theo hướng dẫn + bước mẫu (bước phụ thuộc dữ liệu lúc chạy)
        return {"skill": s["name"], "params": params, "instructions": s["instructions"] or s["description"],
                "example_steps": steps,
                "note": "Hãy tự thực hiện kỹ năng này NGAY bằng các công cụ (làm theo instructions / các bước mẫu, thay "
                        "file_id bằng kết quả tìm mới), rồi báo kết quả."}
    results, pendings = [], []
    for st in steps:
        r = call_tool(st["tool"], st["args"], groups, user)
        if r.get("pending_action"):
            pendings.append(r["pending_action"])
        results.append({"tool": st["tool"], "args": st["args"],
                        "result": {k: v for k, v in r.items() if not k.startswith("_")}})
        if r.get("error"):
            break
    return {"skill": s["name"], "params": params, "ran": results, "pending_actions": pendings,
            "note": "Đã chạy kỹ năng; báo người dùng kết quả từng bước (lỗi nếu có)."}


def manage(args: dict, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    if args.get("action") == "delete":
        s = get(args.get("name") or "")
        if not s:
            return {"error": f"Không có kỹ năng '{args.get('name')}'."}
        c = _conn()
        c.execute("DELETE FROM skills WHERE skill_id=?", (s["skill_id"],))
        c.commit()
        c.close()
        return {"deleted": s["name"]}
    return {"skills": [{"name": s["name"], "description": s["description"], "params": list(s["params"]),
                        "steps": len(s["steps"]), "uses": s["uses"]} for s in list_all()] or "(chưa dạy kỹ năng nào)"}


def delete_by_id(skill_id: int) -> bool:
    c = _conn()
    n = c.execute("DELETE FROM skills WHERE skill_id=?", (skill_id,)).rowcount
    c.commit()
    c.close()
    return bool(n)
