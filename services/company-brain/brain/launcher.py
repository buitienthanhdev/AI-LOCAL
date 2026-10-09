"""Thực thi theo Ý ĐỊNH: mở trang/video trên web bằng trình duyệt chỉ định, mở một file bằng app chỉ định (kể cả app
Store như Media Player), và nhớ quy tắc "loại file -> app" do người dùng dặn (vd. ảnh/video mở bằng Media Player).
Chỉ admin (người dùng trên máy chủ) dùng được. Không bao giờ sửa registry/app mặc định của Windows — quy tắc nằm
trong catalog của bot và chỉ áp dụng khi bot mở file."""
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
import winreg
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from .catalog import connect
from .config import DATA_DIR
from .facts import norm
from .vn import expand_slang

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS open_rules (
    ext        TEXT PRIMARY KEY,      -- .png
    app        TEXT NOT NULL,         -- tên app hiển thị (khớp lại bằng apps.find mỗi lần mở)
    owner      TEXT,
    created_at TEXT NOT NULL
);
"""

# file chương trình/kịch bản: không mở "như tài liệu" (tránh chạy mã ngoài ý muốn) — dùng open_app cho app đã cài
EXECUTABLE_EXT = {".exe", ".bat", ".cmd", ".com", ".msi", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".scr",
                  ".lnk", ".reg", ".hta", ".jar", ".cpl", ".msc", ".dll", ".sys", ".appx", ".msix"}

# nhóm loại file theo cách người dùng gọi
EXT_GROUPS = {
    "anh": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".ico", ".svg"],
    "video": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v", ".mpg", ".mpeg", ".3gp"],
    "nhac": [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus", ".mid"],
    "pdf": [".pdf"], "word": [".doc", ".docx", ".rtf", ".odt"], "excel": [".xls", ".xlsx", ".xlsm", ".csv", ".ods"],
    "powerpoint": [".ppt", ".pptx", ".odp"], "text": [".txt", ".log", ".md"],
    "code": [".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".sql", ".sh"],
    "nen": [".zip", ".rar", ".7z", ".tar", ".gz"],
}
GROUP_WORDS = {
    "anh": ("anh", "hinh", "hinh anh", "photo", "image", "picture", "pic"),
    "video": ("video", "phim", "clip", "film", "movie"),
    "nhac": ("nhac", "am thanh", "audio", "music", "mp3", "bai hat", "song"),
    "pdf": ("pdf",), "word": ("word", "docx", "van ban", "doc"), "excel": ("excel", "bang tinh", "xlsx", "sheet"),
    "powerpoint": ("powerpoint", "ppt", "pptx", "slide", "trinh chieu"), "text": ("text", "txt", "van ban tho", "ghi chu"),
    "code": ("code", "ma nguon", "lap trinh", "source"), "nen": ("nen", "zip", "rar", "archive"),
}

SITES = {  # tên gọi -> mẫu URL tìm kiếm
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "wikipedia": "https://vi.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "dich": "https://translate.google.com/?sl=auto&tl=vi&text={q}",
    "facebook": "https://www.facebook.com/search/top?q={q}",
    "tiktok": "https://www.tiktok.com/search?q={q}",
    "shopee": "https://shopee.vn/search?keyword={q}",
}
SITE_WORDS = {"youtube": ("youtube", "utb", "ytb", "yt", "tube", "you tube"), "google": ("google", "gg", "search"),
              "maps": ("maps", "ban do", "google maps"), "wikipedia": ("wikipedia", "wiki"),
              "github": ("github",), "dich": ("dich", "translate"), "facebook": ("facebook", "fb"),
              "tiktok": ("tiktok", "tik tok"), "shopee": ("shopee",)}
BROWSERS = {  # tên -> (exe trong App Paths, các cách gọi)
    "chrome": ("chrome.exe", ("chrome", "google chrome", "gg", "google", "cr")),
    "edge": ("msedge.exe", ("edge", "msedge", "microsoft edge", "egde")),
    "firefox": ("firefox.exe", ("firefox", "ff", "cao cao")),
    "brave": ("brave.exe", ("brave",)),
    "coccoc": ("browser.exe", ("coc coc", "coccoc")),
    "opera": ("opera.exe", ("opera",)),
}

TOOLS = [
    {"type": "function", "function": {
        "name": "open_url",
        "description": ("Mở trang web / TÌM trên web bằng trình duyệt cụ thể. Dùng khi người dùng muốn nghe/xem/tìm thứ "
                        "KHÔNG có sẵn trong máy (nhạc, video, thông tin) hoặc nói rõ 'trên youtube/google/edge/chrome'. "
                        "site='youtube' + play=true: tự mở thẳng video đầu tiên khớp (mở bài hát). 'utb'/'yt' = YouTube, "
                        "'gg' = Google/Chrome. Không nói trình duyệt nào thì bỏ trống browser (dùng trình duyệt mặc định)."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "từ khóa tìm kiếm HOẶC địa chỉ http(s)://"},
            "site": {"type": "string", "description": "youtube | google | maps | wikipedia | github | facebook | tiktok | "
                                                       "shopee | dich (mặc định google)"},
            "browser": {"type": "string", "description": "chrome | edge | firefox | brave | coccoc | opera (tùy chọn)"},
            "play": {"type": "boolean", "description": "youtube: mở thẳng video đầu tiên thay vì trang kết quả"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_file_with",
        "description": ("Mở MỘT FILE bằng đúng ứng dụng chỉ định (vd. mp3 bằng Media Player, ảnh bằng Photos, pdf bằng "
                        "Chrome). Bỏ trống app thì dùng quy tắc đã học cho loại file đó, rồi tới app mặc định của Windows. "
                        "Mở file nhạc/video/ảnh/tài liệu NGAY, không cần hỏi lại. Lấy file_id từ kết quả tìm sơ bộ/sql_catalog."),
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "integer", "description": "bỏ trống = FILE VỪA MỞ gần đây (cho 'mở bằng app này/chrome nè')"},
            "app": {"type": "string", "description": "tên app người dùng gọi (tùy chọn)"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "set_open_rule",
        "description": ("GHI NHỚ quy tắc người dùng dặn: loại file nào từ nay mở bằng app nào (vd. 'sau này ảnh, video mở "
                        "bằng Media Player'). Gọi NGAY khi người dùng dặn kiểu 'từ giờ/sau này/mặc định ... mở bằng ...'. "
                        "types: ['anh','video'] hoặc ['.png','.mp4'] hoặc 'nhạc'. Không đổi app mặc định của Windows — "
                        "chỉ áp dụng khi bot mở file."),
        "parameters": {"type": "object", "properties": {
            "types": {"type": "array", "items": {"type": "string"},
                      "description": "nhóm (anh, video, nhac, pdf, word, excel, powerpoint, text, code, nen) hoặc đuôi file"},
            "app": {"type": "string"}}, "required": ["types", "app"]}}},
    {"type": "function", "function": {
        "name": "manage_open_rules",
        "description": "Xem hoặc xóa quy tắc 'loại file -> app' đã ghi nhớ. action=list | remove (kèm types).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "remove"]},
            "types": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "file_type_info",
        "description": ("Cho biết một loại file (đuôi hoặc nhóm: .mp3, ảnh, video...) hiện mở bằng app nào: quy tắc đã học, "
                        "app mặc định của Windows, và các app đã cài có thể mở được. Dùng để tự chọn cách mở."),
        "parameters": {"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}}},
]


# ---------------------------------------------------------------- registry
def _reg(hive, path: str, name: str = ""):
    try:
        with winreg.OpenKey(hive, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _subkeys(hive, path: str) -> list[str]:
    try:
        with winreg.OpenKey(hive, path) as k:
            return [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except OSError:
        return []


def app_path(exe: str) -> str | None:
    """Đường dẫn đầy đủ của một exe đã đăng ký (App Paths) — chrome.exe, msedge.exe, wmplayer.exe..."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        v = _reg(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
        if v:
            v = os.path.expandvars(str(v)).strip('"')
            if Path(v).exists():
                return v
    return None


def default_handler(ext: str) -> dict:
    """App mặc định hiện tại của Windows cho một đuôi file (chỉ đọc)."""
    base = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}"
    prog = _reg(winreg.HKEY_CURRENT_USER, base + r"\UserChoice", "ProgId") or _reg(winreg.HKEY_CLASSES_ROOT, ext)
    out: dict = {"ext": ext, "progid": prog}
    if prog:
        out["aumid"] = _reg(winreg.HKEY_CLASSES_ROOT, rf"{prog}\Application", "AppUserModelID")
        cmd = _reg(winreg.HKEY_CLASSES_ROOT, rf"{prog}\shell\open\command")
        m = re.match(r'\s*"?([^"]+?\.exe)', str(cmd or ""), re.I)
        out["exe"] = m.group(1) if m else None
    others = []
    for key in (_subkeys(winreg.HKEY_CURRENT_USER, base + r"\OpenWithProgids")
                + _subkeys(winreg.HKEY_CLASSES_ROOT, rf"{ext}\OpenWithProgids")):
        others.append(key)
    for letter in "abcdefgh":
        v = _reg(winreg.HKEY_CURRENT_USER, base + r"\OpenWithList", letter)
        if v:
            others.append(str(v))
    out["others"] = list(dict.fromkeys(others))
    return out


def _match_app(token: str | None):
    """Đổi AUMID / tên exe / ProgId thành app trong danh sách đã cài (hoặc None)."""
    from . import apps
    if not token:
        return None
    t = token.lower()
    for a in apps.apps():
        if a["id"].lower() == t:
            return a
    base = os.path.basename(t)
    for a in apps.apps():
        if a.get("target") and os.path.basename(a["target"]).lower() == base:
            return a
    stem = base.rsplit(".", 1)[0]
    if len(stem) >= 4:
        for a in apps.apps():
            if norm(a["name"]).replace(" ", "") == norm(stem).replace(" ", ""):
                return a
    return None


def default_app_name(ext: str) -> str | None:
    h = default_handler(ext)
    a = _match_app(h.get("aumid")) or _match_app(h.get("exe"))
    if a:
        return a["name"]
    exe = h.get("exe")
    return os.path.splitext(os.path.basename(exe))[0] if exe else h.get("progid")


# ---------------------------------------------------------------- loại file
def exts_from(types) -> list[str]:
    """['ảnh','video'] / '.png' / 'nhạc' -> danh sách đuôi file. Không hiểu thì bỏ qua."""
    if isinstance(types, str):
        types = re.split(r"[,;/]| và | va ", types)
    out: list[str] = []
    for t in types or []:
        raw = str(t).strip().lower()
        if re.fullmatch(r"\.?[a-z0-9]{1,6}", raw) and (raw.startswith(".") or raw in {e[1:] for g in EXT_GROUPS.values() for e in g}):
            out.append(raw if raw.startswith(".") else "." + raw)
            continue
        n = norm(expand_slang(raw)).strip()
        for g, words in GROUP_WORDS.items():
            if n == g or n in words or any(w in n.split() or (len(w) > 3 and w in n) for w in words):
                out += EXT_GROUPS[g]
                break
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- quy tắc loại file -> app
def _conn():
    c = connect()
    c.executescript(SCHEMA)
    return c


def rule_for(ext: str) -> str | None:
    c = _conn()
    r = c.execute("SELECT app FROM open_rules WHERE ext=?", (ext.lower(),)).fetchone()
    c.close()
    return r["app"] if r else None


def list_rules() -> list[dict]:
    c = _conn()
    rows = [dict(r) for r in c.execute("SELECT ext, app, owner, created_at FROM open_rules ORDER BY app, ext")]
    c.close()
    return rows


def _grouped(rows: list[dict]) -> list[dict]:
    """Gộp theo app để hiển thị: 'Media Player: ảnh (.jpg .png...), video (...)'."""
    by_app: dict[str, set] = {}
    for r in rows:
        by_app.setdefault(r["app"], set()).add(r["ext"])
    out = []
    for app, exts in by_app.items():
        names = [g for g, lst in EXT_GROUPS.items() if set(lst) <= exts]
        covered = {e for g in names for e in EXT_GROUPS[g]}
        out.append({"app": app, "groups": names, "other_exts": sorted(exts - covered), "count": len(exts), "exts": sorted(exts)})
    return out


def set_rules(types, app_query: str, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được đặt quy tắc mở file."}
    from . import apps
    exts = exts_from(types)
    if not exts:
        return {"error": f"Không hiểu loại file: {types}. Dùng nhóm (anh, video, nhac, pdf...) hoặc đuôi file (.png)."}
    hits = [h for h in apps.find(app_query) if h["score"] >= 60]
    if not hits:
        return {"error": f"Máy không có app khớp '{app_query}'.",
                "installed_apps": [h["name"] for h in apps.find(app_query, 8)] or sorted({a['name'] for a in apps.apps()})[:40]}
    app = hits[0]["name"]
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        c = _conn()
        for e in exts:
            c.execute("INSERT INTO open_rules (ext, app, owner, created_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(ext) DO UPDATE SET app=excluded.app, owner=excluded.owner, created_at=excluded.created_at",
                      (e, app, user, now))
        c.commit()
        c.close()
    from . import obs
    obs.audit(user, "open_rule_set", f"{app}: {' '.join(exts)}"[:200], "")
    return {"saved": True, "app": app, "extensions": exts,
            "note": f"Đã ghi nhớ: từ giờ các file {', '.join(exts[:12])}{'...' if len(exts) > 12 else ''} bot sẽ mở bằng {app}."}


def remove_rules(types) -> dict:
    exts = exts_from(types)
    c = _conn()
    n = 0
    for e in exts:
        n += c.execute("DELETE FROM open_rules WHERE ext=?", (e,)).rowcount
    c.commit()
    c.close()
    return {"removed": n, "extensions": exts}


def manage_rules(args: dict, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    if args.get("action") == "remove":
        return remove_rules(args.get("types"))
    rows = list_rules()
    g = _grouped(rows)
    lines = [f"{', '.join(r['groups'] + r['other_exts'])} → {r['app']}" for r in g]
    return {"rules": g, "total_extensions": len(rows), "summary": lines or "(chưa có quy tắc nào)",
            "note": "Chỉ đọc lại ĐÚNG phần summary cho người dùng."}


def file_type_info(t: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    exts = exts_from(t)
    if not exts:
        return {"error": f"Không hiểu loại file: {t}"}
    ext = exts[0]
    h = default_handler(ext)
    cands = []
    for tok in h["others"]:
        a = _match_app(tok)
        if a and a["name"] not in cands:
            cands.append(a["name"])
    return {"ext": ext, "group_extensions": exts[:15], "learned_rule": rule_for(ext), "windows_default": default_app_name(ext),
            "apps_that_can_open": cands}


# ---------------------------------------------------------------- mở file
_PS_ACTIVATE = r"""
param([string]$Aumid, [string]$Path)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IApplicationActivationManager {
  int ActivateApplication([In, MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [In, MarshalAs(UnmanagedType.LPWStr)] string arguments, [In] int options, [Out] out uint processId);
  int ActivateForFile([In, MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [In] IntPtr itemArray, [In, MarshalAs(UnmanagedType.LPWStr)] string verb, [Out] out uint processId);
  int ActivateForProtocol([In, MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [In] IntPtr itemArray, [Out] out uint processId);
}
[ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")] public class ApplicationActivationManager {}
public static class Act {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, [In] ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out object ppv);
  [DllImport("shell32.dll", PreserveSig = false)]
  static extern void SHCreateShellItemArrayFromShellItem([MarshalAs(UnmanagedType.Interface)] object psi, [In] ref Guid riid, out IntPtr ppv);
  public static uint Run(string aumid, string path) {
    Guid shellItem = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
    Guid shellItemArray = new Guid("b63ea76d-1f85-456f-a19c-48159efa858b");
    object item; SHCreateItemFromParsingName(path, IntPtr.Zero, ref shellItem, out item);
    IntPtr arr; SHCreateShellItemArrayFromShellItem(item, ref shellItemArray, out arr);
    uint pid; var m = (IApplicationActivationManager)new ApplicationActivationManager();
    int hr = m.ActivateForFile(aumid, arr, "open", out pid);
    if (hr != 0) throw new Exception("HRESULT 0x" + hr.ToString("X"));
    return pid;
  }
}
'@
try { [Act]::Run($Aumid, $Path) | Out-Null; "OK" } catch { "ERR " + $_.Exception.Message; exit 1 }
"""


def activate_packaged(aumid: str, path: str) -> tuple[bool, str]:
    """Mở file bằng app Store (Media Player, Photos...) qua IApplicationActivationManager.ActivateForFile."""
    script = DATA_DIR / "activate_packaged.ps1"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not script.exists() or script.read_text(encoding="utf-8") != _PS_ACTIVATE:
        script.write_text(_PS_ACTIVATE, encoding="utf-8")
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                        "-Aumid", aumid, "-Path", path], capture_output=True, text=True, encoding="utf-8",
                       creationflags=_NOWIN, timeout=60)
    out = (p.stdout or p.stderr or "").strip()
    return p.returncode == 0 and out.endswith("OK"), out[-200:]


def _launch_with(app: dict, path: str) -> tuple[bool, str]:
    """Mở file bằng app cụ thể. Trả về (ok, phương thức/lỗi)."""
    if "!" in app["id"] and not app.get("exe"):  # app Store (AUMID dạng PackageFamily!AppId)
        return activate_packaged(app["id"], path)
    exe = app["id"] if app.get("exe") else (app.get("target") or app_path(app["id"] + ".exe"))
    if not exe:
        return False, "không xác định được file chạy của app này"
    subprocess.Popen([exe, path])
    return True, "exe"


def open_file(path: str, app_name: str | None, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được mở file."}
    from . import apps
    p = Path(path)
    if not p.exists():
        return {"error": f"File không còn trên ổ đĩa: {p}"}
    ext = p.suffix.lower()
    if ext in EXECUTABLE_EXT:
        return {"error": f"{p.name} là file chương trình/kịch bản — bot không chạy trực tiếp. Dùng open_app cho app đã cài."}
    source = "chỉ định"
    if not app_name:
        app_name, source = rule_for(ext), "quy tắc đã học"
    if app_name:
        hits = [h for h in apps.find(app_name) if h["score"] >= 60]
        if not hits:
            return {"error": f"Máy không có app khớp '{app_name}'.",
                    "installed_apps": [h["name"] for h in apps.find(app_name, 8)] or sorted({a['name'] for a in apps.apps()})[:40]}
        app = hits[0]
        try:
            ok, how = _launch_with(app, str(p))
        except OSError as e:
            ok, how = False, str(e)
        if ok:
            return {"opened": p.name, "with": app["name"], "chosen_by": source, "path": str(p)}
        # app không nhận file theo cách này => nói thật rồi dùng app mặc định
        os.startfile(str(p))
        return {"opened": p.name, "with": default_app_name(ext) or "app mặc định của Windows", "chosen_by": "mặc định",
                "warning": f"Không mở được bằng {app['name']} ({how}) nên đã mở bằng app mặc định.", "path": str(p)}
    os.startfile(str(p))
    return {"opened": p.name, "with": default_app_name(ext) or "app mặc định của Windows", "chosen_by": "mặc định",
            "path": str(p), "note": "Chưa có quy tắc cho loại file này; nếu người dùng muốn app khác thì nhờ họ dặn, bot sẽ nhớ."}


_last: dict = {}


def last_opened() -> dict | None:
    """File vừa mở gần đây (để hiểu 'mở bằng app này', 'mở lại bằng chrome' không nhắc tên file)."""
    return dict(_last) if _last and time.time() - _last["at"] < 1800 else None


def open_file_by_id(file_id, app_name: str | None, is_admin: bool) -> dict:
    if not file_id:
        last = last_opened()
        if not last:
            return {"error": "Chưa có file nào vừa mở; hãy nói rõ file nào (tên file)."}
        file_id = last["file_id"]
    conn = connect()
    r = conn.execute("SELECT path, name FROM files WHERE file_id=? AND status='active'", (int(file_id or 0),)).fetchone()
    conn.close()
    if not r:
        return {"error": "file_id không tồn tại trong catalog (lấy file_id từ kết quả tìm sơ bộ hoặc sql_catalog)."}
    res = open_file(r["path"], app_name, is_admin)
    if "error" not in res:
        _last.update(file_id=int(file_id), name=r["name"], app=res.get("with"), at=time.time())
    return res


# ---------------------------------------------------------------- mở web
def pick_site(text: str | None) -> str:
    n = norm(expand_slang(text or "")).strip()
    for site, words in SITE_WORDS.items():
        if n == site or n in words:
            return site
    for site, words in SITE_WORDS.items():
        if any(w in n.split() for w in words):
            return site
    return "google"


def pick_browser(text: str | None) -> str | None:
    n = norm(text or "").strip()
    if not n:
        return None
    for b, (_exe, words) in BROWSERS.items():
        if n == b or n in words:
            return b
    for b, (_exe, words) in BROWSERS.items():
        if any(w in n.split() for w in words):
            return b
    return None


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
       "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8"}


def youtube_first(query: str) -> dict | None:
    """Video đầu tiên của kết quả tìm YouTube (đọc trang kết quả công khai). Lỗi mạng => None."""
    try:
        import requests
        r = requests.get("https://www.youtube.com/results", params={"search_query": query, "hl": "vi"}, headers=_UA,
                         timeout=8)
        m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"(.*?)"\}', r.text, re.S)
        if m:
            try:
                title = json.loads('"' + m.group(2) + '"')  # giải mã \uXXXX / \" trong JSON nhúng của trang
            except ValueError:
                title = m.group(2)
            return {"id": m.group(1), "title": title}
        m = re.search(r'"videoId":"([\w-]{11})"', r.text)
        return {"id": m.group(1), "title": ""} if m else None
    except Exception:
        return None


def _open_in_browser(url: str, browser: str | None) -> tuple[bool, str]:
    if browser:
        exe = app_path(BROWSERS[browser][0])
        if not exe:
            return False, f"Máy không cài {browser}"
        subprocess.Popen([exe, url])
        return True, browser
    webbrowser.open(url)
    return True, "trình duyệt mặc định"


def open_url(args: dict, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được mở trình duyệt."}
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "Thiếu query/địa chỉ."}
    browser = pick_browser(args.get("browser"))
    info: dict = {}
    if re.match(r"^https?://", q, re.I):
        url = q
    elif " " not in q and re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", q):
        url = "https://" + q
    else:
        site = pick_site(args.get("site"))
        url = SITES[site].format(q=quote_plus(q))
        info["site"] = site
        if site == "youtube" and args.get("play", True):
            v = youtube_first(q)
            if v:
                url = f"https://www.youtube.com/watch?v={v['id']}"
                info["video"] = v["title"] or v["id"]
    if urlparse(url).scheme not in ("http", "https"):
        return {"error": "Chỉ mở địa chỉ http/https."}
    try:
        ok, used = _open_in_browser(url, browser)
    except OSError as e:
        return {"error": f"Không mở được trình duyệt: {e}"}
    if not ok:
        return {"error": used, "note": "Thử không chỉ định browser hoặc chọn trình duyệt khác (chrome/edge)."}
    return {"opened_url": url, "browser": used, **info}
