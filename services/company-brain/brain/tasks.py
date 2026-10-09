"""Hẹn giờ + việc nền + thông báo.

- reminder: tới giờ thì nhắc (thông báo Windows + tab Việc trong panel).
- prompt:   tới giờ bot tự làm một yêu cầu như khi bạn chat (vd. "mỗi sáng 8h tóm tắt file mới hôm qua") rồi báo kết quả.
            Việc làm thay đổi máy trong lúc chạy vẫn chỉ là ĐỀ XUẤT (nút Thực hiện nằm trong thông báo, giữ 24 giờ).
- command:  tới giờ chạy một lệnh PowerShell. Lên lịch lệnh phải được người dùng bấm Thực hiện một lần lúc tạo.
- Việc nền: run_command(background=true) chạy lệnh lâu trong nền, xong thì báo.
Lặp: once | minutes (mỗi N phút) | hourly | daily (HH:MM) | weekdays (T2-T6) | weekly (các thứ trong tuần, HH:MM)."""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta

from .catalog import connect

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()
_started = False
TICK_S = 15
DAYS = ["thứ hai", "thứ ba", "thứ tư", "thứ năm", "thứ sáu", "thứ bảy", "chủ nhật"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id      INTEGER PRIMARY KEY,
    owner        TEXT NOT NULL,
    groups       TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- reminder | prompt | command
    title        TEXT NOT NULL,
    payload      TEXT NOT NULL,          -- nội dung nhắc / yêu cầu / lệnh
    repeat       TEXT NOT NULL,          -- once | minutes | hourly | daily | weekdays | weekly
    interval_min INTEGER,
    at_time      TEXT,                   -- HH:MM
    weekdays     TEXT,                   -- "0,2,4" (0 = thứ hai)
    next_run     TEXT,
    last_run     TEXT,
    last_status  TEXT,
    active       INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    notif_id   INTEGER PRIMARY KEY,
    owner      TEXT NOT NULL,
    task_id    INTEGER,
    title      TEXT NOT NULL,
    body       TEXT,
    data       TEXT,                     -- JSON: kết quả đầy đủ (answer, actions, output...)
    created_at TEXT NOT NULL,
    read       INTEGER DEFAULT 0
);
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "schedule_task",
        "description": ("HẸN GIỜ / LẶP LẠI: 'nhắc tôi 5h chiều họp', 'sau 30 phút nhắc uống nước', 'mỗi sáng 8h tóm tắt file mới', "
                        "'mỗi thứ 6 17h chạy lệnh backup'. kind: reminder (nhắc), prompt (bot tự làm yêu cầu rồi báo kết quả), "
                        "command (chạy lệnh PowerShell — cần người dùng bấm Thực hiện). when: 'HH:MM' (hôm nay, qua giờ thì "
                        "ngày mai), 'YYYY-MM-DD HH:MM', hoặc '+30m' / '+2h' / '+1d'. Tự quy đổi giờ theo 'Bây giờ' trong ngữ cảnh."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["reminder", "prompt", "command"]},
            "payload": {"type": "string", "description": "nội dung nhắc / yêu cầu cho bot / lệnh PowerShell"},
            "when": {"type": "string"},
            "repeat": {"type": "string", "enum": ["once", "minutes", "hourly", "daily", "weekdays", "weekly"]},
            "interval_minutes": {"type": "integer", "description": "repeat=minutes: mỗi bao nhiêu phút"},
            "weekdays": {"type": "array", "items": {"type": "integer"}, "description": "repeat=weekly: 0=thứ 2 ... 6=chủ nhật"},
            "title": {"type": "string", "description": "tên ngắn"}},
            "required": ["kind", "payload", "when"]}}},
    {"type": "function", "function": {
        "name": "manage_tasks",
        "description": "Xem / hủy / tạm dừng / bật lại việc đã hẹn giờ. action=list | cancel | pause | resume (kèm task_id).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "cancel", "pause", "resume"]},
            "task_id": {"type": "integer"}}, "required": ["action"]}}},
]
NAMES = ("schedule_task", "manage_tasks")


def _conn():
    c = connect()
    c.executescript(SCHEMA)
    return c


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- thời gian
def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    w = (when or "").strip().lower().replace("h", ":") if re.fullmatch(r"\s*\d{1,2}h(\d{2})?\s*", when or "") else \
        (when or "").strip().lower()
    m = re.fullmatch(r"\+\s*(\d+)\s*(m|p|phut|min|h|g|gio|d|ngay)", w)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now + (timedelta(minutes=n) if u in ("m", "p", "phut", "min") else
                      timedelta(hours=n) if u in ("h", "g", "gio") else timedelta(days=n))
    m = re.fullmatch(r"(\d{1,2}):(\d{0,2})", w)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2}) (\d{1,2}:\d{2})", w)  # "24/12 20:00" => năm nay
    if m:
        w = f"{m.group(1)}/{m.group(2)}/{now.year} {m.group(3)}"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(w, fmt)
        except ValueError:
            continue
    return None


def next_after(task: dict, after: datetime) -> datetime | None:
    """Lần chạy kế tiếp SAU thời điểm `after` (None = không lặp)."""
    rep = task["repeat"]
    if rep == "once":
        return None
    if rep == "minutes":
        return after + timedelta(minutes=max(1, int(task.get("interval_min") or 60)))
    if rep == "hourly":
        return after + timedelta(hours=1)
    hh, mm = (int(x) for x in (task.get("at_time") or "08:00").split(":"))
    days = {"daily": set(range(7)), "weekdays": {0, 1, 2, 3, 4}}.get(rep) or \
        {int(x) for x in (task.get("weekdays") or "0").split(",") if x.strip().isdigit()}
    t = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if t > after and t.weekday() in days:
            return t
        t = (t + timedelta(days=1)).replace(hour=hh, minute=mm)
    return None


def describe(t: dict) -> str:
    rep = t["repeat"]
    when = {"once": "một lần", "minutes": f"mỗi {t.get('interval_min')} phút", "hourly": "mỗi giờ",
            "daily": f"mỗi ngày {t.get('at_time')}", "weekdays": f"thứ 2–6 lúc {t.get('at_time')}",
            "weekly": "mỗi " + ", ".join(DAYS[int(d)] for d in (t.get("weekdays") or "0").split(",") if d.isdigit())
                      + f" lúc {t.get('at_time')}"}[rep]
    return f"#{t['task_id']} [{t['kind']}] {t['title']} — {when}; lần tới: {(t.get('next_run') or '—').replace('T', ' ')}"


# ---------------------------------------------------------------- tạo / quản lý
def create(owner: str, groups: list[str], kind: str, payload: str, when: str, repeat: str = "once",
           interval_min: int | None = None, weekdays=None, title: str | None = None) -> dict:
    first = parse_when(when)
    if not first:
        return {"error": f"Không hiểu thời điểm '{when}'. Dùng 'HH:MM', 'YYYY-MM-DD HH:MM' hoặc '+30m'/'+2h'."}
    if repeat not in ("once", "minutes", "hourly", "daily", "weekdays", "weekly"):
        return {"error": f"repeat không hợp lệ: {repeat}"}
    if repeat == "minutes" and not interval_min:
        interval_min = 60
    wd = ",".join(str(int(d) % 7) for d in weekdays) if weekdays else (str(first.weekday()) if repeat == "weekly" else None)
    t = {"repeat": repeat, "interval_min": interval_min, "at_time": first.strftime("%H:%M"), "weekdays": wd}
    if repeat in ("daily", "weekdays", "weekly"):  # lần đầu phải rơi đúng ngày hợp lệ
        first = next_after(t, first - timedelta(seconds=1)) or first
    title = (title or payload)[:80]
    with _lock:
        c = _conn()
        cur = c.execute("""INSERT INTO tasks (owner, groups, kind, title, payload, repeat, interval_min, at_time, weekdays,
                           next_run, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (owner, ",".join(groups), kind, title, payload, repeat, interval_min, t["at_time"], wd,
                         _iso(first), _iso(datetime.now())))
        c.commit()
        tid = cur.lastrowid
        row = dict(c.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone())
        c.close()
    from . import obs
    obs.audit(owner, "task_create", describe(row)[:200], payload[:300])
    return {"scheduled": describe(row), "task_id": tid, "first_run": _iso(first)}


def schedule_tool(args: dict, user: str, groups: list[str], is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được hẹn giờ."}
    kind, payload = args.get("kind"), (args.get("payload") or "").strip()
    if kind not in ("reminder", "prompt", "command") or not payload:
        return {"error": "Cần kind (reminder|prompt|command) và payload."}
    params = dict(owner=user, groups=groups, kind=kind, payload=payload, when=str(args.get("when") or ""),
                  repeat=args.get("repeat") or "once", interval_min=args.get("interval_minutes"),
                  weekdays=args.get("weekdays"), title=args.get("title"))
    if kind == "command":  # chạy lệnh không người trông => xác nhận MỘT lần lúc lên lịch
        if not parse_when(params["when"]):
            return {"error": f"Không hiểu thời điểm '{params['when']}'."}
        from .actions import add_pending
        return add_pending("schedule_command", f"Lên lịch chạy lệnh ({params['repeat']}, từ {params['when']}): "
                           f"{payload[:120]}", params, user, detail=payload)
    return create(**params)


def manage_tool(args: dict, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    act, tid = args.get("action"), args.get("task_id")
    if act == "list":
        def state(t):
            if t["active"]:
                return ""
            if t["repeat"] == "once" and t["last_run"]:
                return f" (ĐÃ CHẠY XONG lúc {t['last_run'].replace('T', ' ')[:16]})"
            return " (đang tạm dừng)"
        return {"tasks": [describe(t) + state(t) for t in list_tasks(user)] or "(chưa có việc nào được hẹn giờ)"}
    if not tid:
        return {"error": "Cần task_id (xem bằng action=list)."}
    ok = set_active(int(tid), user, {"cancel": None, "pause": 0, "resume": 1}[act])
    return {"ok": ok, "task_id": tid, "action": act} if ok else {"error": f"Không có việc #{tid}."}


def list_tasks(owner: str) -> list[dict]:
    c = _conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE owner=? ORDER BY active DESC, next_run", (owner,))]
    c.close()
    return rows


def set_active(task_id: int, owner: str, active) -> bool:
    """active: 1 bật, 0 tạm dừng, None xóa."""
    c = _conn()
    r = c.execute("SELECT * FROM tasks WHERE task_id=? AND owner=?", (task_id, owner)).fetchone()
    if r:
        if active is None:
            c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        else:
            nxt = r["next_run"]
            if active and (not nxt or nxt < _iso(datetime.now())):  # bật lại việc đã quá giờ => tính lần kế tiếp
                n = next_after(dict(r), datetime.now())
                nxt = _iso(n) if n else nxt
            c.execute("UPDATE tasks SET active=?, next_run=? WHERE task_id=?", (active, nxt, task_id))
        c.commit()
    c.close()
    return bool(r)


# ---------------------------------------------------------------- thông báo
def toast(title: str, body: str):
    """Thông báo Windows (góc phải màn hình) — không chặn, lỗi thì bỏ qua."""
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
          "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
          "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          "$x=$t.GetElementsByTagName('text');$x.Item(0).AppendChild($t.CreateTextNode($env:CB_T)) > $null;"
          "$x.Item(1).AppendChild($t.CreateTextNode($env:CB_B)) > $null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=_NOWIN,
                         env={**os.environ, "CB_T": title[:120], "CB_B": body[:250]})
    except OSError:
        pass


def notify(owner: str, title: str, body: str, data: dict | None = None, task_id: int | None = None,
           popup: bool = True) -> int:
    with _lock:
        c = _conn()
        cur = c.execute("INSERT INTO notifications (owner, task_id, title, body, data, created_at) VALUES (?,?,?,?,?,?)",
                        (owner, task_id, title[:200], body[:4000], json.dumps(data or {}, ensure_ascii=False, default=str),
                         _iso(datetime.now())))
        c.commit()
        nid = cur.lastrowid
        c.execute("DELETE FROM notifications WHERE notif_id NOT IN (SELECT notif_id FROM notifications ORDER BY notif_id "
                  "DESC LIMIT 300)")
        c.commit()
        c.close()
    if popup:
        toast(title, body)
    return nid


def notifications(owner: str, limit: int = 40) -> list[dict]:
    c = _conn()
    rows = [dict(r) | {"data": json.loads(r["data"] or "{}")} for r in c.execute(
        "SELECT * FROM notifications WHERE owner=? ORDER BY notif_id DESC LIMIT ?", (owner, limit))]
    c.close()
    return rows


def unread(owner: str) -> int:
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM notifications WHERE owner=? AND read=0", (owner,)).fetchone()[0]
    c.close()
    return n


def mark_read(owner: str):
    c = _conn()
    c.execute("UPDATE notifications SET read=1 WHERE owner=? AND read=0", (owner,))
    c.commit()
    c.close()


# ---------------------------------------------------------------- chạy
def _llm_slot():
    try:
        from .server import _llm_slots
        return _llm_slots
    except Exception:
        return threading.BoundedSemaphore(1)


def run_task(t: dict) -> str:
    owner, kind = t["owner"], t["kind"]
    late = ""
    if t.get("next_run") and datetime.fromisoformat(t["next_run"]) < datetime.now() - timedelta(minutes=10):
        late = f" (trễ — dự kiến {t['next_run'].replace('T', ' ')}, lúc đó máy chủ không chạy)"
    if kind == "reminder":
        notify(owner, "⏰ Nhắc việc" + late, t["payload"], {"kind": kind}, t["task_id"])
        return "ok"
    if kind == "command":
        from .computer import _run
        r = _run(t["payload"], None, 600)
        notify(owner, f"⚙ {t['title']}" + late, f"Mã thoát {r['exit_code']} · {r['output'][-300:]}",
               {"kind": kind, "output": r["output"], "command": t["payload"]}, t["task_id"])
        return f"exit {r['exit_code']}"
    from .agent import ask
    with _llm_slot():
        r = ask(t["payload"], t["groups"].split(","), user=owner)
    from .actions import extend_ttl
    for a in r.get("actions") or []:
        extend_ttl(a["id"], 24 * 3600)  # đề xuất trong việc nền: giữ nút Thực hiện 24 giờ
    notify(owner, f"🤖 {t['title']}" + late, r["answer"][:400],
           {"kind": kind, **{k: r.get(k) for k in ("answer", "sources", "actions", "confidence", "evidence")}}, t["task_id"])
    return "ok"


def tick(now: datetime | None = None) -> int:
    """Chạy các việc tới hạn (mỗi việc một luồng). Trả về số việc đã khởi chạy."""
    now = now or datetime.now()
    with _lock:
        c = _conn()
        due = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE active=1 AND next_run<=?", (_iso(now),))]
        for t in due:  # dời lịch TRƯỚC khi chạy => không chạy trùng
            n = next_after(t, max(now, datetime.fromisoformat(t["next_run"])))
            c.execute("UPDATE tasks SET last_run=?, next_run=?, active=? WHERE task_id=?",
                      (_iso(now), _iso(n) if n else None, 1 if n else 0, t["task_id"]))
        c.commit()
        c.close()
    for t in due:
        threading.Thread(target=_run_and_record, args=(t,), daemon=True, name=f"task-{t['task_id']}").start()
    return len(due)


def _run_and_record(t: dict):
    try:
        status = run_task(t)
    except Exception as e:
        status = f"lỗi: {type(e).__name__}: {e}"
        notify(t["owner"], f"⚠ Việc '{t['title']}' lỗi", status, {"kind": t["kind"]}, t["task_id"])
    c = _conn()
    c.execute("UPDATE tasks SET last_status=? WHERE task_id=?", (status[:300], t["task_id"]))
    c.commit()
    c.close()


def start():
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                tick()
            except Exception:
                pass
            time.sleep(TICK_S)
    threading.Thread(target=loop, daemon=True, name="scheduler").start()


# ---------------------------------------------------------------- việc nền
def run_background(command: str, cwd: str | None, timeout: int, owner: str) -> dict:
    """Chạy lệnh lâu trong nền; xong thì gửi thông báo kèm output."""
    def work():
        from .computer import _run
        r = _run(command, cwd, timeout)
        notify(owner, "✅ Việc nền xong" if r["exit_code"] == 0 else "⚠ Việc nền kết thúc có lỗi",
               f"{command[:80]} · {r['seconds']}s · {r['output'][-250:]}",
               {"kind": "background", "command": command, "output": r["output"], "exit_code": r["exit_code"]})
    threading.Thread(target=work, daemon=True, name="bg-command").start()
    return {"background": True, "command": command,
            "note": "Đang chạy trong nền (tối đa %d phút). Xong sẽ có thông báo + kết quả trong tab Việc." % (timeout // 60)}
