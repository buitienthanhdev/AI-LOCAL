"""Thao tác máy tính cho bot (chỉ admin = người dùng trên máy chủ) — bộ "tay chân" tương đương một trợ lý lập trình:
đọc file bất kỳ, tìm file / tìm trong nội dung trực tiếp trên ổ đĩa, ghi/sửa file, chạy lệnh PowerShell, tìm + đọc web,
chụp màn hình (model xem được), điều khiển âm lượng/phát nhạc, xem tình trạng máy.

An toàn:
- Việc CHỈ ĐỌC chạy ngay. Việc làm THAY ĐỔI máy (ghi/sửa file, lệnh không thuộc danh sách chỉ-đọc) thành yêu cầu chờ
  người dùng bấm "Thực hiện" (actions.py), có xem trước lệnh / diff.
- Không ghi vào thư mục hệ thống. Trước khi ghi đè/sửa, bản cũ được sao lưu vào data/backups.
- Mọi thao tác thay đổi được ghi audit."""
import base64
import ctypes
import difflib
import fnmatch
import hashlib
import io
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import DATA_DIR
from .facts import norm

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_OUT = 8000
SKIP_DIRS = {"$recycle.bin", "system volume information", "winsxs", "node_modules", ".git", "__pycache__",
             "windowsapps", "installer", "servicing", "softwaredistribution"}
BINARY_EXT = {".exe", ".dll", ".sys", ".bin", ".dat", ".db", ".sqlite", ".zip", ".rar", ".7z", ".gz", ".iso", ".msi",
              ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".mp3", ".mp4", ".mkv", ".avi", ".mov", ".wav",
              ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".pyc", ".class", ".jar", ".lib", ".obj", ".pdb"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
RICH_EXT = {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls"}  # cần trích chữ; .md/.csv đọc thẳng như văn bản

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": ("Đọc TRỰC TIẾP một file bất kỳ trên máy theo đường dẫn (kể cả file chưa học): văn bản/code theo dòng, "
                        "docx/xlsx/pptx/pdf ra chữ, ẢNH thì bạn được xem ảnh. Dùng khi cần nội dung chính xác/mới nhất."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "find_files",
        "description": ("Tìm file/thư mục TRỰC TIẾP trên ổ đĩa theo mẫu tên (*.mp3, *bao cao*, setup*.exe), không dấu cũng được. "
                        "root mặc định là thư mục người dùng; 'C:\\' hoặc 'D:\\' để tìm cả ổ (chậm hơn)."),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "root": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "search_in_files",
        "description": ("Tìm một chuỗi/regex bên trong nội dung các file văn bản/code dưới một thư mục (như grep). "
                        "Trả về file:dòng:nội dung."),
        "parameters": {"type": "object", "properties": {
            "regex": {"type": "string"}, "root": {"type": "string"},
            "file_glob": {"type": "string", "description": "lọc tên file, vd *.py (mặc định mọi file văn bản)"},
            "limit": {"type": "integer"}}, "required": ["regex", "root"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": ("Tạo mới hoặc ghi đè/ghi thêm một file văn bản (ghi chú, code, script, csv...). Cần người dùng bấm "
                        "Thực hiện; bản cũ được sao lưu."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"]}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": ("Sửa một file văn bản/code bằng cách thay đoạn old_text (phải khớp CHÍNH XÁC, nên đọc file trước) "
                        "bằng new_text. Cần người dùng bấm Thực hiện (có xem trước diff); bản cũ được sao lưu."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": ("Chạy lệnh PowerShell trên máy (tra cứu hệ thống, mạng, tiến trình, cài đặt, git, python, đóng app "
                        "Stop-Process...). Lệnh chỉ-đọc (Get-*, ipconfig, tasklist, systeminfo, nvidia-smi, git status...) "
                        "chạy ngay và trả output; lệnh làm thay đổi máy cần người dùng bấm Thực hiện."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "cwd": {"type": "string"},
            "timeout_s": {"type": "integer", "description": "mặc định 60, tối đa 600 (nền: tối đa 7200)"},
            "background": {"type": "boolean", "description": "việc lâu (cài đặt, sao lưu, quét...): chạy nền, xong báo"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Tìm trên Internet (thông tin mới, tài liệu, hướng dẫn, giá, tin tức). Trả về tiêu đề + link + trích đoạn.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Tải và đọc nội dung chữ của một trang web (http/https) — dùng sau web_search để đọc chi tiết.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "screenshot",
        "description": "Chụp màn hình hiện tại để BẠN XEM (người dùng hỏi 'màn hình đang có gì', 'lỗi này là gì', 'đọc giúp').",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "media_control",
        "description": ("Điều khiển âm thanh/nhạc đang phát: volume_up, volume_down (mỗi bước ~2%), mute (bật/tắt tiếng), "
                        "play_pause, next, previous, stop. 'to hơn chút' = volume_up steps 5; 'bài khác/bài tiếp' = next."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["volume_up", "volume_down", "mute", "play_pause", "next", "previous",
                                                  "stop"]},
            "steps": {"type": "integer"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "system_status",
        "description": "Tình trạng máy: CPU, RAM, GPU/VRAM, dung lượng ổ đĩa, các tiến trình tốn RAM nhất.",
        "parameters": {"type": "object", "properties": {}}}},
]

NAMES = tuple(t["function"]["name"] for t in TOOLS)


def _deny(is_admin: bool):
    return None if is_admin else {"error": "Chỉ người dùng trên máy chủ được thao tác máy tính."}


def _path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser((p or "").strip().strip('"'))))


# ---------------------------------------------------------------- đọc
def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1258", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def image_b64(source, max_side: int = 1600) -> str:
    from PIL import Image
    im = source if hasattr(source, "convert") else Image.open(source)
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def read_file(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    p = _path(args.get("path", ""))
    if not p.is_file():
        return {"error": f"Không có file: {p}" + (" (đây là thư mục — dùng find_files)" if p.is_dir() else "")}
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return {"path": str(p), "file": p.name, "note": "Ảnh đã được đính kèm để bạn xem.", "_image_b64": image_b64(p)}
    if ext in RICH_EXT:
        from .extract import extract
        text, _meta = extract(p, light=True)
    elif ext in BINARY_EXT:
        return {"error": f"{p.name} là file nhị phân/media — không đọc như văn bản được."}
    elif p.stat().st_size > 20_000_000:
        return {"error": "File quá lớn (>20 MB) — dùng search_in_files để tìm đoạn cần thiết."}
    else:
        text = _read_text(p)
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    n = min(max(1, int(args.get("max_lines") or 300)), 2000)
    body = "\n".join(f"{i}\t{line}" for i, line in enumerate(lines[start - 1:start - 1 + n], start))
    return {"path": str(p), "file": p.name, "total_lines": len(lines), "from_line": start,
            "content": body[:15000], "truncated": start - 1 + n < len(lines) or len(body) > 15000}


def _walk(root: Path, deadline: float):
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if time.time() > deadline:
            return
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith("$")]
        yield dirpath, dirnames, filenames


def find_files(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    pat = norm(args.get("pattern") or "*").strip() or "*"
    if not any(c in pat for c in "*?["):
        pat = f"*{pat}*"
    root = _path(args.get("root") or str(Path.home()))
    if not root.is_dir():
        return {"error": f"Không có thư mục: {root}"}
    limit, deadline = min(int(args.get("limit") or 50), 300), time.time() + 25
    out = []
    for dirpath, dirnames, filenames in _walk(root, deadline):
        for name in dirnames + filenames:
            if fnmatch.fnmatch(norm(name), pat):
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                    out.append({"path": fp, "dir": name in dirnames, "size": st.st_size,
                                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
                except OSError:
                    continue
                if len(out) >= limit:
                    return {"root": str(root), "matches": out, "more": True}
    return {"root": str(root), "matches": out, "timed_out": time.time() > deadline}


def search_in_files(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    try:
        rx = re.compile(args.get("regex") or "", re.I)
    except re.error as e:
        return {"error": f"regex sai: {e}"}
    root = _path(args.get("root") or "")
    if not root.exists():
        return {"error": f"Không có thư mục: {root}"}
    glob = (args.get("file_glob") or "*").lower()
    limit, deadline = min(int(args.get("limit") or 50), 300), time.time() + 30
    hits, scanned = [], 0
    files = [(str(root.parent), [], [root.name])] if root.is_file() else _walk(root, deadline)
    for dirpath, _d, filenames in files:
        for name in filenames:
            if Path(name).suffix.lower() in BINARY_EXT or not fnmatch.fnmatch(name.lower(), glob):
                continue
            fp = os.path.join(dirpath, name)
            try:
                if os.path.getsize(fp) > 5_000_000:
                    continue
                text = _read_text(Path(fp))
            except OSError:
                continue
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"file": fp, "line": i, "text": line.strip()[:200]})
                    if len(hits) >= limit:
                        return {"lines": hits, "files_scanned": scanned, "more": True}
            if time.time() > deadline:
                return {"lines": hits, "files_scanned": scanned, "timed_out": True}
    return {"lines": hits, "files_scanned": scanned}


# ---------------------------------------------------------------- ghi / sửa (chờ xác nhận)
def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _diff(old: str, new: str, name: str) -> str:
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"{name} (cũ)", f"{name} (mới)", n=2))
    return d[:3000] + ("\n…" if len(d) > 3000 else "")


def propose_write(args: dict, user: str, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    from .actions import _protected, add_pending
    p = _path(args.get("path", ""))
    if not p.is_absolute():
        return {"error": "path phải là đường dẫn đầy đủ, vd. C:\\Users\\...\\note.txt"}
    if _protected(p):
        return {"error": "Không được ghi vào thư mục hệ thống."}
    if p.is_dir():
        return {"error": "path là thư mục."}
    content, mode = str(args.get("content") or ""), args.get("mode") or "overwrite"
    old = _read_text(p) if p.is_file() else ""
    new = old + content if mode == "append" else content
    verb = "Tạo" if not p.exists() else ("Ghi thêm vào" if mode == "append" else "Ghi đè")
    return add_pending("write_file", f"{verb} {p} ({len(new)} ký tự)", {"dst": str(p), "content": new, "sha": _sha(p)},
                       user, args.get("reason"), detail=_diff(old, new, p.name) if old else new[:3000])


def propose_edit(args: dict, user: str, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    from .actions import _protected, add_pending
    p = _path(args.get("path", ""))
    if not p.is_file():
        return {"error": f"Không có file: {p}"}
    if _protected(p):
        return {"error": "Không được sửa file trong thư mục hệ thống."}
    old_t, new_t = args.get("old_text") or "", args.get("new_text") or ""
    text = _read_text(p)
    n = text.count(old_t) if old_t else 0
    if n == 0:
        return {"error": "Không thấy old_text trong file (phải khớp chính xác) — đọc lại file bằng read_file."}
    if n > 1 and not args.get("replace_all"):
        return {"error": f"old_text xuất hiện {n} lần — thêm ngữ cảnh cho duy nhất hoặc đặt replace_all=true."}
    new = text.replace(old_t, new_t) if args.get("replace_all") else text.replace(old_t, new_t, 1)
    return add_pending("edit_file", f"Sửa {p} ({n} chỗ)", {"dst": str(p), "content": new, "sha": _sha(p)}, user,
                       args.get("reason"), detail=_diff(text, new, p.name))


def do_write(plan: dict) -> str:
    p = Path(plan["dst"])
    if _sha(p) != plan.get("sha"):
        raise OSError("File đã thay đổi kể từ lúc đề xuất — hãy yêu cầu lại.")
    backup = None
    if p.is_file():
        bdir = DATA_DIR / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        backup = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}_{p.name}"
        backup.write_bytes(p.read_bytes())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plan["content"], encoding="utf-8", newline="")
    return f"Đã ghi {p}" + (f" (bản cũ sao lưu: {backup})" if backup else "")


# ---------------------------------------------------------------- lệnh
SAFE_CMDS = {"get-childitem", "gci", "dir", "ls", "get-content", "gc", "cat", "type", "get-item", "gi", "get-itemproperty",
             "get-process", "gps", "ps", "get-service", "gsv", "get-ciminstance", "get-wmiobject", "get-psdrive",
             "get-volume", "get-disk", "get-netipaddress", "get-netadapter", "get-nettcpconnection", "get-date",
             "get-computerinfo", "get-hotfix", "get-package", "get-startapps", "get-command", "get-help", "get-location",
             "get-host", "get-filehash", "get-acl", "get-printer", "get-localuser", "get-timezone", "get-culture",
             "get-uptime", "get-eventlog", "get-winevent", "get-appxpackage", "get-clipboard", "test-path",
             "test-connection", "resolve-path", "split-path", "join-path", "select-object", "select", "where-object",
             "where", "?", "sort-object", "sort", "format-table", "ft", "format-list", "fl", "measure-object", "measure",
             "group-object", "out-string", "convertto-json", "select-string", "sls",
             "ipconfig", "systeminfo", "tasklist", "whoami", "hostname", "nvidia-smi", "where.exe", "ping", "nslookup",
             "netstat", "ver", "wmic", "driverquery", "getmac", "echo", "write-output", "python", "py", "pip", "node",
             "npm", "git", "ollama", "winget", "tree", "findstr", "sc.exe", "query", "reg"}
SAFE_SUB = {"git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "tag"},
            "pip": {"list", "show", "freeze", "--version"}, "npm": {"ls", "list", "-v", "--version", "view"},
            "python": {"--version", "-v", "-V"}, "py": {"--version", "-0"}, "node": {"--version", "-v"},
            "ollama": {"list", "ps", "show", "--version"}, "winget": {"list", "search", "show"},
            "reg": {"query"}, "wmic": set(), "sc.exe": {"query", "qc"}, "query": {"user", "session"}}


def is_read_only(cmd: str) -> bool:
    """Lệnh chỉ đọc => chạy ngay. Có ghi file (>), nối lệnh tùy ý (; & `), gọi biểu thức con => phải xác nhận."""
    c = cmd.strip()
    if not c or re.search(r"[;&`>]|\$\(|::|\.\w+\s*\(|\binvoke-|\biex\b|\bstart-|\bset-|\bremove-|\bnew-|\bstop-|\bcopy-|\bmove-|"
                          r"\brename-|\bout-file|\badd-content|\bclear-|-encodedcommand|\bdownloadstring|\binstall", c, re.I):
        return False
    for seg in c.split("|"):
        toks = seg.strip().split()
        if not toks:
            return False
        head = toks[0].lower()
        if head not in SAFE_CMDS:
            return False
        if head in SAFE_SUB:
            subs = SAFE_SUB[head]
            if not subs or len(toks) < 2 or toks[1].lower() not in subs:
                return False
    return True


def _run(cmd: str, cwd: str | None, timeout: int) -> dict:
    t = time.time()
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
                            + cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=cwd or str(Path.home()), creationflags=_NOWIN, timeout=timeout, stdin=subprocess.DEVNULL)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), -1
        out += f"\n[hết thời gian {timeout}s]"
    return {"exit_code": code, "output": out.strip()[-MAX_OUT:] or "(không có output)",
            "seconds": round(time.time() - t, 1)}


def run_command(args: dict, user: str, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return {"error": "Thiếu command."}
    cwd = args.get("cwd") or None
    if cwd and not Path(cwd).is_dir():
        return {"error": f"Không có thư mục cwd: {cwd}"}
    bg = bool(args.get("background"))
    timeout = max(5, min(int(args.get("timeout_s") or (3600 if bg else 60)), 7200 if bg else 600))
    if is_read_only(cmd):
        if bg:
            from .tasks import run_background
            return run_background(cmd, cwd, timeout, user)
        return {"command": cmd} | _run(cmd, cwd, timeout)
    from .actions import add_pending
    return add_pending("run_command", f"Chạy lệnh{' NỀN' if bg else ''}: {cmd[:160]}",
                       {"command": cmd, "cwd": cwd, "timeout": timeout, "background": bg}, user, args.get("reason"),
                       detail=cmd)


# ---------------------------------------------------------------- web
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
       "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8"}


def web_search(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    import requests
    from bs4 import BeautifulSoup
    q, limit = (args.get("query") or "").strip(), min(int(args.get("limit") or 6), 10)
    if not q:
        return {"error": "Thiếu query."}
    results = []
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "vn-vi"}, headers=_UA, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for res in soup.select(".result"):
            a = res.select_one("a.result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            sn = res.select_one(".result__snippet")
            results.append({"title": a.get_text(" ", strip=True), "url": href,
                            "snippet": sn.get_text(" ", strip=True) if sn else ""})
    except Exception:
        pass
    if not results:  # dự phòng: Bing
        try:
            r = requests.get("https://www.bing.com/search", params={"q": q, "setlang": "vi"}, headers=_UA, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            for li in soup.select("li.b_algo"):
                a, sn = li.select_one("h2 a"), li.select_one("p")
                if a:
                    results.append({"title": a.get_text(" ", strip=True), "url": a.get("href", ""),
                                    "snippet": sn.get_text(" ", strip=True) if sn else ""})
        except Exception as e:
            return {"error": f"Không tìm được trên web: {type(e).__name__}"}
    results = [x for x in results if x["url"].startswith("http")][:limit]
    return {"query": q, "results": results} if results else {"query": q, "results": [], "note": "Không có kết quả."}


def fetch_url(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    import requests
    from bs4 import BeautifulSoup
    url = (args.get("url") or "").strip()
    if urlparse(url).scheme not in ("http", "https"):
        return {"error": "Chỉ đọc địa chỉ http/https."}
    try:
        r = requests.get(url, headers=_UA, timeout=20)
    except Exception as e:
        return {"error": f"Không tải được: {type(e).__name__}: {e}"}
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg", "aside"]):
            t.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = re.sub(r"\n\s*\n+", "\n\n", main.get_text("\n", strip=True))
        title = soup.title.get_text(strip=True) if soup.title else url
    elif "text" in ctype or "json" in ctype:
        text, title = r.text, url
    else:
        return {"error": f"Không phải trang chữ ({ctype or 'không rõ'})", "status": r.status_code}
    n = min(int(args.get("max_chars") or 8000), 20000)
    return {"url": r.url, "title": title, "status": r.status_code, "content": text[:n], "truncated": len(text) > n}


# ---------------------------------------------------------------- màn hình / âm thanh / hệ thống
def screenshot(is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    from PIL import ImageGrab
    im = ImageGrab.grab(all_screens=True)
    return {"captured": f"{im.width}x{im.height}", "note": "Ảnh màn hình đã được đính kèm để bạn xem.",
            "_image_b64": image_b64(im, 1600)}


_VK = {"volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
       "play_pause": 0xB3}


def media_control(args: dict, is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    act = args.get("action")
    if act not in _VK:
        return {"error": f"Không hỗ trợ: {act}"}
    steps = max(1, min(int(args.get("steps") or (5 if act.startswith("volume") else 1)), 50)) if act.startswith("volume") else 1
    for _ in range(steps):
        ctypes.windll.user32.keybd_event(_VK[act], 0, 0, 0)
        ctypes.windll.user32.keybd_event(_VK[act], 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.02)
    return {"done": act, "steps": steps}


def system_status(is_admin: bool) -> dict:
    if d := _deny(is_admin):
        return d
    from . import governor
    ps = ("$os=Get-CimInstance Win32_OperatingSystem; "
          "'RAM: ' + [math]::Round(($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/1MB,1) + ' / ' + "
          "[math]::Round($os.TotalVisibleMemorySize/1MB,1) + ' GB'; "
          "Get-PSDrive -PSProvider FileSystem | Where-Object Used | ForEach-Object { 'Ổ ' + $_.Name + ': trống ' + "
          "[math]::Round($_.Free/1GB,1) + ' / ' + [math]::Round(($_.Used+$_.Free)/1GB,1) + ' GB' }; "
          "'Tiến trình tốn RAM nhất:'; Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 | "
          "ForEach-Object { '  ' + $_.ProcessName + ' — ' + [math]::Round($_.WorkingSet64/1MB) + ' MB' }")
    out = _run(ps, None, 60)["output"]
    gpu = None
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, creationflags=_NOWIN,
                           timeout=15).stdout.strip()
        if q:
            n, u, mu, mt = [x.strip() for x in q.splitlines()[0].split(",")]
            gpu = f"{n}: dùng {u}%, VRAM {mu}/{mt} MB"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"cpu_percent": governor.cpu_load(), "gpu": gpu, "details": out}


# ---------------------------------------------------------------- điều phối
def call(name: str, args: dict, user: str, is_admin: bool) -> dict:
    if name == "read_file":
        return read_file(args, is_admin)
    if name == "find_files":
        return find_files(args, is_admin)
    if name == "search_in_files":
        return search_in_files(args, is_admin)
    if name == "write_file":
        return propose_write(args, user, is_admin)
    if name == "edit_file":
        return propose_edit(args, user, is_admin)
    if name == "run_command":
        return run_command(args, user, is_admin)
    if name == "web_search":
        return web_search(args, is_admin)
    if name == "fetch_url":
        return fetch_url(args, is_admin)
    if name == "screenshot":
        return screenshot(is_admin)
    if name == "media_control":
        return media_control(args, is_admin)
    if name == "system_status":
        return system_status(is_admin)
    return {"error": f"công cụ không tồn tại: {name}"}
