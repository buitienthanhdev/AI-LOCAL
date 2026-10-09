"""Thao tác file do AI đề xuất. AI KHÔNG tự làm: mỗi thao tác thành một "yêu cầu chờ duyệt",
người dùng (admin) bấm Thực hiện thì mới chạy. Xóa = chuyển vào Thùng rác (khôi phục được).
Mọi thao tác được ghi vào nhật ký file_events (ai yêu cầu, ai duyệt, lúc nào)."""
import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .catalog import connect

PENDING_TTL = 30 * 60
_pending: dict[str, dict] = {}
_lock = threading.Lock()

# Không cho sửa/xóa/di chuyển trong vùng hệ thống, dù được đọc
PROTECTED = [Path(os.environ.get("SystemRoot", r"C:\Windows")), Path(r"C:\Program Files"),
             Path(r"C:\Program Files (x86)"), Path(r"C:\ProgramData"), Path(r"C:\Recovery")]
PROTECTED_NAMES = {"$recycle.bin", "system volume information"}

TOOLS = [
    {"type": "function", "function": {
        "name": "file_action",
        "description": ("Thao tác trên file/thư mục: open (mở NGAY, theo quy tắc đã học hoặc app mặc định; muốn app khác "
                        "thì dùng open_file_with), open_folder (mở thư mục chứa file, chạy ngay), copy, move, rename, "
                        "delete (vào Thùng rác), mkdir — 5 thao tác này chỉ chạy sau khi người dùng bấm xác nhận. "
                        "Lấy file_id từ sql_catalog hoặc kết quả tìm kiếm; KHÔNG tự đoán đường dẫn nguồn."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["open", "open_folder", "copy", "move", "rename", "delete", "mkdir"]},
            "file_id": {"type": "integer", "description": "file nguồn (bắt buộc trừ mkdir)"},
            "destination": {"type": "string", "description": "copy/move: thư mục đích (đường dẫn đầy đủ); "
                                                             "rename: tên mới; mkdir: đường dẫn thư mục mới"},
            "reason": {"type": "string", "description": "giải thích ngắn vì sao"}},
            "required": ["action"]}}},
]


def _protected(p: Path) -> bool:
    rp = str(p.resolve()).lower()
    if any(part.lower() in PROTECTED_NAMES for part in p.parts):
        return True
    return any(rp == str(x).lower() or rp.startswith(str(x).lower() + os.sep) for x in PROTECTED)


def _file(file_id) -> dict | None:
    conn = connect()
    r = conn.execute("SELECT file_id, name, path FROM files WHERE file_id=? AND status='active'",
                     (int(file_id or 0),)).fetchone()
    conn.close()
    return dict(r) if r else None


def propose(args: dict, user: str, is_admin: bool) -> dict:
    """Kiểm tra hợp lệ rồi tạo yêu cầu chờ xác nhận. Trả về kết quả cho LLM."""
    if not is_admin:
        return {"error": "Chỉ admin được thao tác file."}
    act = args.get("action")
    dest = (args.get("destination") or "").strip().strip('"')
    src = None
    if act != "mkdir":
        f = _file(args.get("file_id"))
        if not f:
            return {"error": "file_id không tồn tại trong catalog (hãy tra sql_catalog lấy file_id đúng)."}
        src = Path(f["path"])
        if not src.exists():
            return {"error": f"File không còn trên ổ đĩa: {src}"}
    if act in ("move", "rename", "delete") and _protected(src):
        return {"error": "Không được sửa/xóa file trong thư mục hệ thống."}
    if act in ("copy", "move"):
        d = Path(dest)
        if not dest or not d.is_absolute():
            return {"error": "destination phải là đường dẫn thư mục đầy đủ, vd. D:\\Backup"}
        if _protected(d):
            return {"error": "Không được ghi vào thư mục hệ thống."}
        target = d / src.name
        if target.exists():
            return {"error": f"Đã có file trùng tên ở đích: {target}"}
        summary = f"{'Sao chép' if act == 'copy' else 'Di chuyển'} {src} → {target}"
        plan = {"src": str(src), "dst": str(target), "mkdir": str(d)}
    elif act == "rename":
        if not dest or any(c in dest for c in '\\/:*?"<>|'):
            return {"error": "Tên mới không hợp lệ."}
        target = src.with_name(dest)
        if target.exists():
            return {"error": f"Đã có file tên {dest}"}
        summary, plan = f"Đổi tên {src.name} → {dest} (trong {src.parent})", {"src": str(src), "dst": str(target)}
    elif act == "delete":
        summary, plan = f"Xóa {src} (chuyển vào Thùng rác, khôi phục được)", {"src": str(src)}
    elif act == "mkdir":
        d = Path(dest)
        if not dest or not d.is_absolute() or _protected(d):
            return {"error": "Đường dẫn thư mục mới không hợp lệ."}
        if d.exists():
            return {"error": "Thư mục đã tồn tại."}
        summary, plan = f"Tạo thư mục {d}", {"dst": str(d)}
    elif act in ("open", "open_folder"):
        # Mở không làm thay đổi dữ liệu => chạy NGAY (không bắt bấm xác nhận). Áp dụng quy tắc loại-file -> app đã học.
        from . import obs
        if act == "open":
            from .launcher import open_file
            res = open_file(str(src), None, is_admin)
        else:
            subprocess.Popen(["explorer", "/select,", str(src)])
            res = {"opened_folder": str(src.parent)}
        if "error" not in res:
            obs.audit(user, "file_" + act, str(src)[:200], args.get("reason") or "")
        return res | {"note": "Đã mở trên máy chủ. Báo người dùng đã mở xong và bằng app nào."}
    else:
        return {"error": f"Không hỗ trợ thao tác {act}"}

    return add_pending(act, summary, plan, user, args.get("reason"), file_id=args.get("file_id"))


def auto_approve(value: bool | None = None) -> bool:
    """Chế độ TOÀN QUYỀN (admin bật): bot tự thực hiện thao tác thay vì chờ bấm 'Thực hiện'. Lưu trong bảng settings.
    Vẫn giữ: vùng hệ thống được bảo vệ, không gõ mật khẩu, xóa = vào Thùng rác, mọi việc ghi audit, ACL."""
    conn = connect()
    try:
        if value is not None:
            conn.execute("INSERT INTO settings (key, value) VALUES ('auto_approve', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if value else "0",))
            conn.commit()
        r = conn.execute("SELECT value FROM settings WHERE key='auto_approve'").fetchone()
        return bool(r) and str(r[0]).strip('"') == "1"
    finally:
        conn.close()


def _in_user_turn() -> bool:
    from . import llm
    return getattr(llm._tls, "fg", 0) > 0  # chỉ tự làm trong lượt người dùng đang hỏi, không phải đề xuất của việc nền


def add_pending(act: str, summary: str, plan: dict, user: str, reason: str | None = None, file_id=None,
                detail: str | None = None) -> dict:
    """Tạo yêu cầu chờ người dùng bấm Thực hiện (dùng chung cho thao tác file, ghi/sửa file, chạy lệnh).
    Chế độ toàn quyền: thực hiện luôn (vẫn qua đúng đường execute => audit, nhật ký file như khi bấm)."""
    aid = secrets.token_urlsafe(8)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > v.get("ttl", PENDING_TTL)]:
            _pending.pop(k)
        _pending[aid] = {"id": aid, "action": act, "summary": summary, "plan": plan, "user": user,
                         "reason": reason, "file_id": file_id, "created": now}
    if _in_user_turn() and auto_approve():
        res = execute(aid, user, True)
        with _lock:  # đã báo trong câu trả lời => lượt sau không nhắc lại
            _done_log.pop(user, None)
        out = {"auto_executed": True, "ok": res["ok"], "result": res["message"]}
        if res.get("output") is not None:
            out["output"] = res["output"][-3000:]
        return out | {"note": "Chế độ toàn quyền: ĐÃ TỰ THỰC HIỆN (không cần người dùng bấm). Báo đúng kết quả ở trên"
                              + ("" if res["ok"] else " — BỊ LỖI, nói rõ lỗi") + "."}
    pa = {"id": aid, "action": act, "summary": summary}
    if detail:
        pa["detail"] = detail[:3000]
    return {"pending_action": pa,
            "note": "Đã tạo yêu cầu. Hãy nói với người dùng bấm 'Thực hiện' để xác nhận; KHÔNG nói là đã làm xong."}


_done_log: dict[str, list[dict]] = {}


def recently_done(user: str, within_s: int = 1800) -> list[dict]:
    """Các thao tác người dùng vừa bấm Thực hiện từ lượt chat trước (lấy ra một lần)."""
    with _lock:
        items = [x for x in _done_log.pop(user, []) if time.time() - x["at"] < within_s]
    return items


def extend_ttl(aid: str, seconds: int):
    """Giữ nút Thực hiện lâu hơn (đề xuất sinh ra trong việc nền, người dùng có thể xem muộn)."""
    with _lock:
        if aid in _pending:
            _pending[aid]["ttl"] = seconds


def _recycle(path: str):
    from win32com.shell import shell, shellcon
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    res, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, path, None, flags, None, None))
    if res != 0 or aborted:
        raise OSError(f"Không xóa được (mã {res})")


def execute(aid: str, user: str, confirm: bool) -> dict:
    with _lock:
        a = _pending.pop(aid, None)
    if not a:
        return {"ok": False, "message": "Yêu cầu không tồn tại hoặc đã hết hạn."}
    if a["user"] != user:
        return {"ok": False, "message": "Chỉ người tạo yêu cầu mới được xác nhận."}
    if not confirm:
        return {"ok": True, "message": "Đã hủy."}
    p, act = a["plan"], a["action"]
    output = None
    try:
        if act == "run_command" and p.get("background"):
            from .tasks import run_background
            run_background(p["command"], p.get("cwd"), p.get("timeout", 3600), user)
            a["summary"] += " — đang chạy nền, xong sẽ có thông báo"
        elif act == "run_command":
            from .computer import _run
            r = _run(p["command"], p.get("cwd"), p.get("timeout", 60))
            output = r["output"]
            a["summary"] += f" (mã thoát {r['exit_code']})"
        elif act == "batch":
            from .batch import run
            a["summary"] = run(p)
        elif act == "batch_undo":
            from .batch import run_undo
            a["summary"] = run_undo(p)
        elif act == "gui_grant":
            from . import gui
            gui.grant(p["minutes"], p["purpose"], user)
            done = ""
            if p.get("blocked"):  # làm luôn thao tác đang chờ quyền — người dùng không phải nói "tiếp"
                r = gui.call(p["blocked"]["name"], p["blocked"]["args"], user, True)
                done = f" Đã làm: {r['done']}." if r.get("done") else f" Thao tác chờ bị lỗi: {r.get('error')}."
            a["summary"] = (f"Đã cho phép điều khiển chuột/bàn phím {p['minutes']} phút.{done} Còn bước khác thì nói 'tiếp'. "
                            "Dừng khẩn cấp: đưa chuột vào góc trên-trái màn hình.")
        elif act == "schedule_command":
            from .tasks import create
            r = create(**p)
            if r.get("error"):
                raise ValueError(r["error"])
            a["summary"] = "Đã lên lịch: " + r["scheduled"]
        elif act in ("write_file", "edit_file"):
            from .computer import do_write
            a["summary"] = do_write(p)
        elif act == "open":
            os.startfile(p["src"])  # mở trên máy chủ (máy chạy bot)
        elif act == "open_folder":
            subprocess.Popen(["explorer", "/select,", p["src"]])
        elif act == "copy":
            Path(p["mkdir"]).mkdir(parents=True, exist_ok=True)
            shutil.copy2(p["src"], p["dst"])
        elif act in ("move", "rename"):
            if "mkdir" in p:
                Path(p["mkdir"]).mkdir(parents=True, exist_ok=True)
            shutil.move(p["src"], p["dst"])
        elif act == "delete":
            _recycle(p["src"])
        elif act == "mkdir":
            Path(p["dst"]).mkdir(parents=True)
    except Exception as e:
        return {"ok": False, "message": f"Lỗi: {type(e).__name__}: {e}"}
    from . import obs
    obs.audit(user, act if act in ("run_command", "write_file", "edit_file", "schedule_command", "gui_grant", "batch",
                                   "batch_undo")
              else "file_" + act,
              a["summary"][:200],
              a.get("reason") or "")
    if act not in ("open", "open_folder", "mkdir") and a.get("file_id"):
        conn = connect()
        conn.execute("INSERT INTO file_events (file_id, event, actor, at, detail) VALUES (?,?,?,?,?)",
                     (int(a["file_id"]), f"ai_{act}", user, datetime.now().isoformat(timespec="seconds"),
                      a["summary"][:500]))
        conn.commit()
        conn.close()
    with _lock:  # để lượt chat sau biết việc này ĐÃ xong (người dùng bấm nút, không qua chat)
        _done_log.setdefault(user, []).append({"at": time.time(), "summary": a["summary"][:300],
                                               "output": (output or "")[-400:]})
    res = {"ok": True, "message": "Xong: " + a["summary"]}
    if output is not None:
        res["output"] = output
    return res
