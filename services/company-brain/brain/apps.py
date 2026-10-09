"""Mở ứng dụng đã cài trên máy. Danh sách app lấy từ Start Menu của Windows (Get-StartApps: cả app
thường lẫn app Store), mở bằng shell:AppsFolder\\<AppID> — cùng cách Windows mở khi bấm trong Start."""
import json
import os
import subprocess
import threading
import time

from .config import DATA_DIR
from .facts import norm
from .vn import best_similarity, expand_slang

APPS_FILE = DATA_DIR / "apps.json"
REFRESH_SEC = 6 * 3600
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "apps": []}

# Tên gọi thường ngày (tiếng Việt / viết tắt) -> từ khóa trong tên app
ALIASES = {
    "may tinh": "calculator", "may tinh bo tui": "calculator", "ghi chu": "notepad", "so tay": "notepad",
    "trinh duyet": "chrome", "duyet web": "chrome", "gg chrome": "chrome", "edge": "microsoft edge",
    "cai dat": "settings", "thiet lap": "settings", "quan ly file": "file explorer", "thu muc": "file explorer",
    "chup man hinh": "snipping tool", "cat man hinh": "snipping tool", "ve": "paint", "lich": "calendar",
    "camera": "camera", "dong ho": "clock", "bao thuc": "clock", "terminal": "terminal", "cmd": "command prompt",
    "word": "word", "excel": "excel", "powerpoint": "powerpoint", "ppt": "powerpoint", "outlook": "outlook",
    "cad": "autocad", "vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
    "task manager": "task manager", "quan ly tac vu": "task manager", "zalo": "zalo",
    # theo CHỨC NĂNG / loại file (người dùng mô tả thay vì gọi tên)
    "soan thao": "word", "soan van ban": "word", "van ban": "word", "docx": "word", "doc": "word",
    "bang tinh": "excel", "xlsx": "excel", "xls": "excel", "csv": "excel",
    "trinh chieu": "powerpoint", "thuyet trinh": "powerpoint", "slide": "powerpoint", "pptx": "powerpoint",
    "doc pdf": "pdf", "pdf": "pdf", "email": "outlook", "mail": "outlook", "thu dien tu": "outlook",
    "google": "google chrome", "gg": "google chrome", "web": "google chrome", "internet": "google chrome",
    "nhan tin": "zalo", "chat": "zalo", "ban ve": "autocad", "dwg": "autocad", "lap trinh": "visual studio code",
    "lien minh huyen thoai": "league of legends", "lien minh": "league of legends", "lmht": "league of legends",
    "lol": "league of legends", "riot": "riot client", "game": "riot client",
    "python": "anaconda navigator", "postgres": "pgadmin", "csdl": "pgadmin", "database": "pgadmin",
}
# App hệ thống thường không có trong Start Menu => thêm sẵn
BUILTIN = [
    {"name": "File Explorer", "id": "explorer.exe", "exe": True},
    {"name": "Task Manager", "id": "taskmgr.exe", "exe": True},
    {"name": "Command Prompt", "id": "cmd.exe", "exe": True},
    {"name": "Calculator", "id": "calc.exe", "exe": True},
    {"name": "Notepad", "id": "notepad.exe", "exe": True},
    {"name": "Paint", "id": "mspaint.exe", "exe": True},
    {"name": "Snipping Tool", "id": "snippingtool.exe", "exe": True},
    {"name": "Control Panel", "id": "control.exe", "exe": True},
]

TOOLS = [
    {"type": "function", "function": {
        "name": "open_app",
        "description": ("Mở một ứng dụng đã cài trên máy. Người dùng hay gọi tắt, mô tả chức năng hoặc gõ nhầm "
                        "bộ gõ: 'word'/'ửod'/'trình soạn docx' = Word; 'gg'/'trình duyệt' = Google Chrome; "
                        "'bảng tính'/'ẽcel' = Excel; 'trình chiếu'/'ppt' = PowerPoint; 'máy tính' = Calculator. "
                        "Nếu hiểu được ý thì truyền TÊN APP CHUẨN; nếu không chắc thì truyền nguyên văn. "
                        "Tool tự xử lý gõ nhầm; nếu không khớp sẽ trả về danh sách app đã cài để bạn chọn."),
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "list_apps",
        "description": "Liệt kê ứng dụng đã cài khớp từ khóa (để biết máy có app gì).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]


_LNK_PS = (r"[Console]::OutputEncoding=[Text.Encoding]::UTF8; $s=New-Object -ComObject WScript.Shell; "
           r"Get-ChildItem \"$env:ProgramData\Microsoft\Windows\Start Menu\Programs\",\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\" "
           r"-Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object { $t=$s.CreateShortcut($_.FullName).TargetPath; "
           r"if($t -like '*.exe'){ [pscustomobject]@{n=$_.BaseName;t=$t} } } | ConvertTo-Json -Compress")


def _shortcut_targets() -> dict[str, str]:
    """tên shortcut trong Start Menu (không dấu) -> file .exe thật (để mở app kèm file)."""
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", _LNK_PS.replace('\\"', '"')], capture_output=True,
                           text=True, encoding="utf-8", creationflags=_NOWIN, timeout=90)
        raw = json.loads(p.stdout or "[]")
        raw = raw if isinstance(raw, list) else [raw]
        return {norm(x["n"]): x["t"] for x in raw if x.get("n") and x.get("t") and os.path.exists(x["t"])}
    except Exception:
        return {}


def refresh() -> list[dict]:
    """Đọc danh sách app từ Start Menu (PowerShell Get-StartApps)."""
    p = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                       capture_output=True, text=True, encoding="utf-8", creationflags=_NOWIN, timeout=60)
    raw = json.loads(p.stdout or "[]")
    raw = raw if isinstance(raw, list) else [raw]
    apps = [{"name": a["Name"], "id": a["AppID"], "exe": False} for a in raw if a.get("Name") and a.get("AppID")]
    targets = _shortcut_targets()
    for a in apps:  # app thường: biết file chạy thật => mở được kèm file
        t = targets.get(norm(a["name"]))
        if t:
            a["target"] = t
    names = {norm(a["name"]) for a in apps}
    apps += [b for b in BUILTIN if norm(b["name"]) not in names]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APPS_FILE.write_text(json.dumps(apps, ensure_ascii=False), encoding="utf-8")
    with _lock:
        _cache.update(at=time.time(), apps=apps)
    return apps


def apps() -> list[dict]:
    with _lock:
        if _cache["apps"] and time.time() - _cache["at"] < REFRESH_SEC:
            return _cache["apps"]
    try:
        return refresh()
    except Exception:
        if APPS_FILE.exists():
            return json.loads(APPS_FILE.read_text(encoding="utf-8"))
        return list(BUILTIN)


# Tên chứa các từ này thường là công cụ phụ (gỡ cài đặt, trợ giúp...) => xếp sau
_MINOR = ("uninstall", "go cai", "help", "readme", "documentation", "release notes", "license", "activation",
          "repair", "update", "safe mode", "debug", "verifier", "preferences", "reset", "export ", "import ",
          "transfer", "utility")


_FILLER = {"mo", "bat", "khoi", "dong", "chay", "giup", "toi", "minh", "cai", "app", "ung", "dung", "phan", "mem",
           "trinh", "cho", "di", "nhe", "len", "ra", "voi", "open", "start", "run", "the", "di", "hay", "nao"}


def _query_forms(name: str) -> list[str]:
    """Các cách hiểu của tên người dùng gọi: nguyên văn bỏ dấu, mở rộng viết tắt, bí danh theo chức năng."""
    q = norm(name).strip()
    forms = [q, norm(expand_slang(name))]
    # bí danh: khớp cụm dài nhất nằm trong câu ("trinh soan docx" chứa "docx" -> word)
    for key in sorted(ALIASES, key=len, reverse=True):
        if f" {key} " in f" {q} ":
            forms.append(ALIASES[key])
            break
    core = " ".join(t for t in q.split() if t not in _FILLER)  # bỏ từ đệm "mở cái app ... giúp tôi"
    if core and core != q:
        forms.append(ALIASES.get(core, core))
    return [f for f in dict.fromkeys(forms) if f]


def _exact_score(q: str, n: str) -> float:
    toks = q.split()
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if f" {q}" in f" {n}":
        return 65
    if toks and all(t in n for t in toks):
        return 50
    if len(q) >= 3 and q in n.replace(" ", ""):
        return 40
    return 0


def find(name: str, limit: int = 8) -> list[dict]:
    """Tìm app theo tên người dùng gọi — chịu được bỏ dấu, viết tắt (gg, ppt), mô tả chức năng
    (bảng tính, trình soạn docx) và gõ nhầm Telex (ửod -> word, ẽcel -> excel)."""
    forms = _query_forms(name)
    if not forms:
        return []
    scored = []
    for a in apps():
        n = norm(a["name"])
        s = max(_exact_score(f, n) for f in forms)
        if s < 60:  # chưa khớp chắc => so khớp mờ (gõ nhầm, sai chính tả)
            sim = max(best_similarity(f, a["name"]) for f in [name, *forms])
            if sim >= 0.8:
                s = max(s, 45 + 40 * sim)  # 0.8 -> 77, 1.0 -> 85
        if s <= 0:
            continue
        if any(m in n for m in _MINOR):
            s -= 30
        s -= len(n) / 100  # tên ngắn gọn (app chính) ưu tiên hơn
        scored.append((s, a))
    scored.sort(key=lambda x: -x[0])
    return [dict(a, score=round(s, 1)) for s, a in scored[:limit]]


def launch(app: dict):
    if app.get("exe"):
        # "start" để app có cửa sổ riêng (cmd.exe chạy thẳng sẽ không hiện cửa sổ vì server không có console)
        subprocess.Popen(["cmd.exe", "/c", "start", "", app["id"]], creationflags=_NOWIN)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"])


def open_app(name: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được mở ứng dụng."}
    hits = find(name)
    if not hits:
        # Để model tự suy luận theo ngữ cảnh từ danh sách đầy đủ, rồi gọi lại open_app với tên đúng
        return {"not_found": name, "installed_apps": sorted({a["name"] for a in apps()}),
                "note": "Không khớp tên. Hãy hiểu ý người dùng, chọn app phù hợp nhất trong installed_apps rồi gọi "
                        "lại open_app với ĐÚNG tên đó; nếu không có app nào phù hợp thì nói rõ."}
    best = hits[0]
    if norm(name).strip() in ("office", "microsoft office", "van phong"):
        return {"ambiguous": True, "candidates": ["Word", "Excel", "PowerPoint", "Outlook", "Access"],
                "note": "Office gồm nhiều app — hỏi người dùng muốn mở app nào."}
    if best["score"] < 60:  # khớp yếu (thường là công cụ phụ) => không tự mở, hỏi lại
        return {"ambiguous": True, "candidates": [h["name"] for h in hits[:6]],
                "note": "Không chắc app nào — hỏi người dùng chọn."}
    # Mơ hồ: nhiều app cùng điểm cao nhất (vd. 'Office' khớp Word/Excel/PowerPoint) => hỏi lại
    ties = [h for h in hits if h["score"] >= best["score"] - 1 and h["score"] < 79]
    if len(ties) > 1:
        return {"ambiguous": True, "candidates": [h["name"] for h in hits[:6]],
                "note": "Hỏi người dùng muốn mở app nào trong danh sách."}
    try:
        launch(best)
    except OSError as e:
        return {"error": f"Không mở được {best['name']}: {e}"}
    return {"opened": best["name"], "note": "Đã mở ứng dụng trên máy chủ."}


def list_apps(query: str) -> dict:
    return {"apps": [h["name"] for h in find(query, limit=20)], "total_installed": len(apps())}
