"""Điều khiển chuột + bàn phím (chỉ admin) — để bot thao tác được cả những app không có lệnh/API.

Cách bot "nhìn": gui_look chụp cửa sổ đang làm việc, đánh SỐ lên các nút/ô/mục bấm được (lấy từ Windows UI Automation)
và trả về danh sách [số] tên — model chọn theo số, không phải đoán tọa độ.

An toàn:
- Mọi thao tác chuột/phím cần QUYỀN ĐIỀU KHIỂN có thời hạn (mặc định 10 phút, tối đa 200 thao tác) do người dùng bấm
  "Thực hiện" cấp. Hết hạn => phải xin lại.
- DỪNG KHẨN CẤP: đưa chuột vào GÓC TRÊN-TRÁI màn hình (bot kiểm tra trước mỗi thao tác) hoặc bấm "Dừng" trên panel.
- Không gõ vào ô mật khẩu. Mọi thao tác ghi audit."""
import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout

GRANT_MINUTES = 10
MAX_ACTIONS = 200
MAX_ELEMENTS = 120
_lock = threading.Lock()
_grant = {"until": 0.0, "purpose": "", "actions": 0, "by": ""}
_view = {"left": 0, "top": 0, "scale": 1.0, "elements": [], "hwnd": None, "title": ""}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")
user32 = ctypes.windll.user32

# loại điều khiển bấm/gõ được (UIA ControlTypeId)
INTERACTIVE = {50000: "nút", 50002: "ô tick", 50003: "hộp chọn", 50004: "ô nhập", 50005: "liên kết", 50007: "mục",
               50011: "menu", 50013: "nút chọn", 50019: "tab", 50024: "mục cây", 50031: "nút", 50029: "dòng",
               50030: "tài liệu", 50015: "thanh trượt"}

TOOLS = [
    {"type": "function", "function": {
        "name": "gui_look",
        "description": ("NHÌN màn hình để điều khiển chuột/bàn phím: chụp cửa sổ (window = một phần tiêu đề; bỏ trống = cửa sổ "
                        "đang dùng), đánh số các nút/ô/mục bấm được. Trả về ảnh có số + danh sách [số] loại 'tên'. "
                        "Luôn gọi trước khi bấm, và gọi lại sau vài thao tác để kiểm tra kết quả."),
        "parameters": {"type": "object", "properties": {
            "window": {"type": "string"}, "full_screen": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "gui_click",
        "description": ("Bấm chuột: element = số từ gui_look (ưu tiên), hoặc text = tên nút/mục, hoặc x,y = tọa độ trên ảnh "
                        "của gui_look gần nhất. button: left|right; double: nhấp đúp. purpose: việc đang làm (để xin quyền)."),
        "parameters": {"type": "object", "properties": {
            "element": {"type": "integer"}, "text": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right"]}, "double": {"type": "boolean"},
            "purpose": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "gui_type",
        "description": ("Gõ chữ (có dấu tiếng Việt) vào ô đang được chọn; element = số ô nhập để bấm vào trước khi gõ. "
                        "enter=true để nhấn Enter sau khi gõ."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "element": {"type": "integer"}, "enter": {"type": "boolean"},
            "purpose": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "gui_key",
        "description": "Nhấn phím / tổ hợp phím: 'enter', 'esc', 'tab', 'ctrl+s', 'ctrl+c', 'alt+tab', 'win+d', 'f5', 'down'...",
        "parameters": {"type": "object", "properties": {
            "keys": {"type": "string"}, "times": {"type": "integer"}, "purpose": {"type": "string"}}, "required": ["keys"]}}},
    {"type": "function", "function": {
        "name": "gui_scroll",
        "description": "Cuộn chuột lên/xuống (amount = số nấc, mặc định 5), tại element (nếu có) hoặc vị trí chuột hiện tại.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"},
            "element": {"type": "integer"}, "purpose": {"type": "string"}}, "required": ["direction"]}}},
    {"type": "function", "function": {
        "name": "gui_window",
        "description": "Cửa sổ: action=list (các cửa sổ đang mở) | focus (đưa cửa sổ có tiêu đề chứa title lên trước).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "focus"]}, "title": {"type": "string"}}, "required": ["action"]}}},
]
NAMES = tuple(t["function"]["name"] for t in TOOLS)


# ---------------------------------------------------------------- quyền điều khiển
def status() -> dict:
    left = _grant["until"] - time.time()
    return {"active": left > 0 and _grant["actions"] < MAX_ACTIONS, "seconds_left": max(0, int(left)),
            "purpose": _grant["purpose"], "actions": _grant["actions"]}


def grant(minutes: int = GRANT_MINUTES, purpose: str = "", by: str = ""):
    with _lock:
        _grant.update(until=time.time() + 60 * max(1, min(int(minutes), 60)), purpose=purpose[:200], actions=0, by=by)
    from . import obs
    obs.audit(by, "gui_grant", f"{minutes} phút", purpose[:300])


def revoke(reason: str = "người dùng bấm Dừng"):
    with _lock:
        was = _grant["until"] > time.time()
        _grant.update(until=0.0, actions=0)
    if was:
        from . import obs
        obs.audit(_grant.get("by"), "gui_revoke", reason, None)


def _virtual_origin() -> tuple[int, int]:
    return user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN


def _dpi_aware():
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2: tọa độ vật lý nhất quán
    except (AttributeError, OSError):
        pass


def _check(purpose: str, user: str, blocked: dict | None = None) -> dict | None:
    """None = được phép thao tác; ngược lại trả về yêu cầu cấp quyền / lỗi dừng khẩn cấp."""
    _dpi_aware()
    st = status()
    if st["active"]:
        pt = _cursor()
        vx, vy = _virtual_origin()
        if pt[0] <= vx + 2 and pt[1] <= vy + 2:  # chuột ở góc trên-trái => dừng khẩn cấp
            revoke("dừng khẩn cấp (chuột ở góc trên-trái)")
            return {"error": "DỪNG KHẨN CẤP: người dùng đưa chuột vào góc trên-trái. Đã thu hồi quyền điều khiển; "
                             "không thao tác tiếp, báo người dùng."}
        with _lock:
            _grant["actions"] += 1
        return None
    from . import actions
    if _grant.get("pending") in actions._pending:  # đã xin rồi, đang chờ người dùng bấm => không xin trùng
        return {"error": "Đang chờ người dùng bấm 'Thực hiện' để cấp quyền điều khiển. DỪNG thao tác, báo người dùng bấm "
                         "rồi nói 'tiếp'."}
    add_pending = actions.add_pending
    p = (purpose or "thao tác theo yêu cầu").strip()
    r = add_pending("gui_grant", f"Cho phép bot điều khiển chuột + bàn phím trong {GRANT_MINUTES} phút để: {p[:150]}",
                    {"minutes": GRANT_MINUTES, "purpose": p, "blocked": blocked}, user,
                    detail="Trong thời gian này bot tự bấm/gõ trên màn hình. Dừng khẩn cấp: đưa chuột vào góc "
                           "trên-trái màn hình, hoặc bấm 'Dừng' trên panel.")
    if "pending_action" not in r:  # chế độ toàn quyền: đã tự cấp quyền và làm luôn thao tác đang chờ
        return r
    _grant["pending"] = r["pending_action"]["id"]
    return r | {"note": "CHƯA có quyền điều khiển. Báo người dùng bấm 'Thực hiện': bot sẽ cấp quyền và LÀM NGAY thao tác này; "
                        "nếu còn bước sau thì người dùng nói 'tiếp'. KHÔNG nói đã làm xong; dừng gọi công cụ gui_ ở lượt này."}


# ---------------------------------------------------------------- nhập liệu cấp thấp
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor() -> tuple[int, int]:
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send(*inputs: _INPUT):
    arr = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    return _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def _mouse_input(flags: int, data: int = 0) -> _INPUT:
    return _INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(mouseData=ctypes.c_ulong(data & 0xFFFFFFFF).value, dwFlags=flags)))


def type_text(text: str):
    """Nhập chữ bằng DÁN (clipboard + Ctrl+V): gõ từng phím ảo bị bộ gõ tiếng Việt (Unikey/EVKey) và app chậm làm rơi/
    lặp ký tự. Clipboard cũ của người dùng được trả lại sau khi dán."""
    import win32clipboard as cb
    old = None
    try:
        cb.OpenClipboard()
        try:
            if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
                old = cb.GetClipboardData(cb.CF_UNICODETEXT)
            cb.EmptyClipboard()
            cb.SetClipboardText(text, cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()
    except Exception:
        return _type_keys(text)  # clipboard đang bị app khác giữ => gõ phím
    press("ctrl+v")
    time.sleep(0.35)  # chờ app đọc clipboard xong rồi mới trả lại
    try:
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            if old is not None:
                cb.SetClipboardText(old, cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()
    except Exception:
        pass


def _type_keys(text: str):
    """Dự phòng: gõ Unicode từng ký tự (KEYEVENTF_UNICODE)."""
    for ch in text:
        if ch == "\n":
            press("enter")
            continue
        code = ord(ch)
        units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for u in units:
            _send(_key_input(scan=u, flags=0x0004), _key_input(scan=u, flags=0x0004 | 0x0002))  # UNICODE, KEYUP
        time.sleep(0.004)


VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "enter": 0x0D,
      "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
      "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "printscreen": 0x2C, "capslock": 0x14,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
EXTENDED = {0x2E, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x5B}


def _vk(name: str) -> int | None:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    return None


def press(keys: str, times: int = 1):
    vks = [_vk(k) for k in keys.replace(" ", "").split("+")]
    if not vks or any(v is None for v in vks):
        raise ValueError(f"Không hiểu phím: {keys}")
    for _ in range(max(1, min(times, 50))):
        downs = [_key_input(vk=v, flags=0x0001 if v in EXTENDED else 0) for v in vks]
        ups = [_key_input(vk=v, flags=(0x0001 if v in EXTENDED else 0) | 0x0002) for v in reversed(vks)]
        _send(*downs, *ups)
        time.sleep(0.05)


def click_at(x: int, y: int, button: str = "left", double: bool = False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    for _ in range(2 if double else 1):
        _send(_mouse_input(down), _mouse_input(up))
        time.sleep(0.06)


def scroll(amount: int):
    _send(_mouse_input(0x0800, 120 * amount))  # MOUSEEVENTF_WHEEL; dương = lên


# ---------------------------------------------------------------- cửa sổ
def windows() -> list[dict]:
    import win32gui
    import win32process
    out = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h) or win32gui.GetWindow(h, 4):  # bỏ cửa sổ có owner (popup phụ)
            return True
        title = win32gui.GetWindowText(h)
        if not title or title in ("Program Manager", "Windows Input Experience") or win32gui.GetClassName(h) in (
                "TkTopLevel", "Shell_TrayWnd", "Progman"):
            return True
        if title.startswith("Company Brain") or title in ("Picture-in-picture", "Picture in picture"):
            return True  # chính panel/nút AI của bot; khung video nổi
        ex = win32gui.GetWindowLong(h, -20)  # GWL_EXSTYLE
        if ex & 0x00000080 or ex & 0x08000000 or (ex & 0x00000020 and ex & 0x00080000):
            return True  # cửa sổ công cụ / không nhận focus / lớp phủ trong suốt (overlay)
        if _cloaked(h):
            return True  # app Store đang ẩn (vd. Settings chạy nền)
        l, t, r, b = win32gui.GetWindowRect(h)
        if r - l < 200 or b - t < 120:
            return True
        out.append({"hwnd": h, "title": title, "pid": win32process.GetWindowThreadProcessId(h)[1],
                    "minimized": bool(win32gui.IsIconic(h)), "rect": (l, t, r, b)})
        return True
    _dpi_aware()
    win32gui.EnumWindows(cb, None)
    return out  # thứ tự Z: trên cùng trước


def _cloaked(hwnd: int) -> bool:
    v = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))  # DWMWA_CLOAKED
    except OSError:
        return False
    return bool(v.value)


def focus(hwnd: int):
    import win32con
    import win32gui
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    _send(_key_input(vk=0x12), _key_input(vk=0x12, flags=0x0002))  # nhả Alt => được phép đổi cửa sổ trước
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.25)


def _capture(hwnd: int):
    """Ảnh NỘI DUNG của một cửa sổ kể cả khi đang bị cửa sổ khác che (PrintWindow, PW_RENDERFULLCONTENT).
    Trả về None nếu app không hỗ trợ (ảnh đen) — khi đó chụp màn hình thường."""
    import win32gui
    import win32ui
    from PIL import Image
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc, w, h)
        mem.SelectObject(bmp)
        ok = user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    if not ok or img.convert("L").getextrema()[1] < 8:  # thất bại hoặc ảnh đen hoàn toàn
        return None
    return img


def _ensure_front():
    """Thao tác chuột/phím phải rơi vào ĐÚNG cửa sổ vừa nhìn => đưa nó lên trước nếu đang bị che."""
    h = _view.get("hwnd")
    if h and user32.GetForegroundWindow() != h and user32.IsWindow(h):
        focus(h)


def _target(window: str | None) -> dict | None:
    ws = windows()
    if window:
        from .facts import norm
        q = norm(window)
        ws = [w for w in ws if q in norm(w["title"])]
    else:  # ưu tiên cửa sổ người dùng đang dùng (nếu không phải panel của bot)
        fg = user32.GetForegroundWindow()
        hit = next((w for w in ws if w["hwnd"] == fg), None)
        if hit:
            return hit
    return next((w for w in ws if not w["minimized"]), ws[0] if ws else None)


# ---------------------------------------------------------------- UI Automation
def _uia():
    import comtypes
    import comtypes.client as cc
    try:
        comtypes.CoInitializeEx()
    except OSError:
        pass
    cc.GetModule("UIAutomationCore.dll")
    from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
    return cc.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)


def _elements(hwnd: int | None) -> list[dict]:
    """Các điều khiển bấm/gõ được, đang hiển thị, trong cửa sổ hwnd (hoặc cả màn hình)."""
    def work():
        _dpi_aware()
        u = _uia()
        root = u.ElementFromHandle(hwnd) if hwnd else u.GetRootElement()
        cond = None
        for ct in INTERACTIVE:
            c = u.CreatePropertyCondition(30003, ct)  # UIA_ControlTypePropertyId
            cond = c if cond is None else u.CreateOrCondition(cond, c)
        cr = u.CreateCacheRequest()
        for pid in (30005, 30003, 30001, 30022, 30010, 30019):  # Name, ControlType, Rect, IsOffscreen, IsEnabled, IsPassword
            cr.AddProperty(pid)
        found = root.FindAllBuildCache(4, cond, cr)  # TreeScope_Descendants
        out = []
        for i in range(min(found.Length, 2000)):
            e = found.GetElement(i)
            r = e.CachedBoundingRectangle
            if e.CachedIsOffscreen or r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            name = (e.CachedName or "").strip()
            if not name and e.CachedControlType not in (50004, 50030):  # ô nhập/tài liệu không tên vẫn giữ
                continue
            out.append({"type": INTERACTIVE.get(e.CachedControlType, "?"), "name": name[:80],
                        "rect": (r.left, r.top, r.right, r.bottom), "enabled": bool(e.CachedIsEnabled),
                        "password": bool(e.CachedIsPassword)})
        return out
    try:
        els = _pool.submit(work).result(timeout=12)
    except FutTimeout:
        return []
    els.sort(key=lambda e: (e["rect"][1] // 12, e["rect"][0]))  # đọc từ trên xuống, trái sang phải
    return els[:MAX_ELEMENTS]


def look(args: dict, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ."}
    from PIL import ImageDraw, ImageFont, ImageGrab
    from .computer import image_b64
    _dpi_aware()
    w = None if args.get("full_screen") else _target(args.get("window"))
    if args.get("window") and not w:
        return {"error": f"Không thấy cửa sổ '{args['window']}'.", "windows": [x["title"] for x in windows()][:20]}
    if w and w["minimized"]:
        focus(w["hwnd"])
        w = _target(w["title"]) or w
    img = None
    if w:
        try:
            img = _capture(w["hwnd"])  # nội dung đúng của cửa sổ dù đang bị che
        except Exception:
            img = None
        left, top = w["rect"][0], w["rect"][1]
    if img is None:
        full = ImageGrab.grab(all_screens=True)
        vx, vy = _virtual_origin()
        if w:
            l, t, r, b = w["rect"]
            img = full.crop((max(l - vx, 0), max(t - vy, 0), min(r - vx, full.width), min(b - vy, full.height)))
            left, top = max(l, vx), max(t, vy)
        else:
            img, left, top = full, vx, vy
    els = _elements(w["hwnd"] if w else None)
    draw, font = ImageDraw.Draw(img), None
    try:
        font = ImageFont.truetype("arialbd.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    lines = []
    for i, e in enumerate(els, 1):
        x0, y0, x1, y1 = (e["rect"][0] - left, e["rect"][1] - top, e["rect"][2] - left, e["rect"][3] - top)
        draw.rectangle((x0, y0, x1, y1), outline=(255, 40, 40), width=2)
        tag = str(i)
        tw = draw.textlength(tag, font=font)
        draw.rectangle((x0, y0, x0 + tw + 6, y0 + 18), fill=(255, 40, 40))
        draw.text((x0 + 3, y0), tag, fill=(255, 255, 255), font=font)
        lines.append(f"[{i}] {e['type']} '{e['name']}'" + ("" if e["enabled"] else " (mờ)") + (" (mật khẩu)" if e["password"] else ""))
    scale = min(1.0, 1600 / max(img.width, img.height))
    _view.update(left=left, top=top, scale=scale, elements=els, hwnd=w["hwnd"] if w else None,
                 title=w["title"] if w else "toàn màn hình")
    return {"window": _view["title"], "elements": "\n".join(lines) or "(không đọc được điều khiển — dùng x,y trên ảnh)",
            "image_size": [int(img.width * scale), int(img.height * scale)], "control": status(),
            "note": "Ảnh đã đính kèm, số đỏ = element. Bấm bằng gui_click(element=số).", "_image_b64": image_b64(img, 1600)}


def _resolve(args: dict) -> tuple[int, int] | dict:
    els = _view["elements"]
    if args.get("element"):
        i = int(args["element"])
        if not 1 <= i <= len(els):
            return {"error": f"Không có element {i} — gọi gui_look để xem lại."}
        r = els[i - 1]["rect"]
        return (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    if args.get("text"):
        from .facts import norm
        q = norm(args["text"])
        hit = next((e for e in els if norm(e["name"]) == q), None) or next((e for e in els if q in norm(e["name"])), None)
        if not hit:
            return {"error": f"Không thấy nút/mục '{args['text']}' trong lần nhìn gần nhất — gọi gui_look."}
        r = hit["rect"]
        return (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    if args.get("x") is not None and args.get("y") is not None:
        s = _view["scale"] or 1.0
        return int(_view["left"] + int(args["x"]) / s), int(_view["top"] + int(args["y"]) / s)
    return {"error": "Cần element, text hoặc x,y."}


def _done(what: str, user: str, **extra) -> dict:
    from . import obs
    obs.audit(user, "gui", what[:200], _grant["purpose"][:200])
    time.sleep(0.35)
    return {"done": what, **extra, "control": status(), "hint": "Gọi gui_look để kiểm tra màn hình sau thao tác."}


def call(name: str, args: dict, user: str, is_admin: bool) -> dict:
    if not is_admin:
        return {"error": "Chỉ người dùng trên máy chủ được điều khiển chuột/bàn phím."}
    if name == "gui_look":
        return look(args, is_admin)
    if name == "gui_window":
        if args.get("action") == "focus":
            if d := _check(args.get("purpose") or f"chuyển sang cửa sổ {args.get('title')}", user):
                return d
            w = _target(args.get("title"))
            if not w:
                return {"error": f"Không thấy cửa sổ '{args.get('title')}'.", "windows": [x["title"] for x in windows()][:20]}
            focus(w["hwnd"])
            return _done(f"đưa lên trước: {w['title']}", user)
        return {"windows": [x["title"] + (" (thu nhỏ)" if x["minimized"] else "") for x in windows()][:30]}
    if d := _check(args.get("purpose") or "", user, {"name": name, "args": args}):
        return d
    try:
        _ensure_front()
        if name == "gui_click":
            pos = _resolve(args)
            if isinstance(pos, dict):
                return pos
            click_at(*pos, button=args.get("button") or "left", double=bool(args.get("double")))
            return _done(f"bấm {'đúp ' if args.get('double') else ''}{args.get('button') or 'trái'} tại {pos}", user)
        if name == "gui_type":
            if args.get("element"):
                pos = _resolve({"element": args["element"]})
                if isinstance(pos, dict):
                    return pos
                el = _view["elements"][int(args["element"]) - 1]
                if el["password"]:
                    return {"error": "Đây là ô MẬT KHẨU — bot không gõ mật khẩu. Nhờ người dùng tự nhập."}
                click_at(*pos)
            if _focused_is_password():
                return {"error": "Ô đang chọn là ô MẬT KHẨU — bot không gõ mật khẩu. Nhờ người dùng tự nhập."}
            type_text(str(args.get("text") or ""))
            if args.get("enter"):
                press("enter")
            return _done(f"gõ {len(str(args.get('text') or ''))} ký tự" + (" + Enter" if args.get("enter") else ""), user)
        if name == "gui_key":
            press(str(args.get("keys") or ""), int(args.get("times") or 1))
            return _done(f"nhấn {args.get('keys')}" + (f" x{args.get('times')}" if (args.get("times") or 1) > 1 else ""), user)
        if name == "gui_scroll":
            if args.get("element"):
                pos = _resolve({"element": args["element"]})
                if isinstance(pos, dict):
                    return pos
                user32.SetCursorPos(*pos)
            n = max(1, min(int(args.get("amount") or 5), 50))
            scroll(n if args.get("direction") == "up" else -n)
            return _done(f"cuộn {args.get('direction')} {n} nấc", user)
    except (ValueError, OSError) as e:
        return {"error": str(e)}
    return {"error": f"công cụ không tồn tại: {name}"}


def _focused_is_password() -> bool:
    def work():
        try:
            return bool(_uia().GetFocusedElement().CurrentIsPassword)
        except Exception:
            return False
    try:
        return _pool.submit(work).result(timeout=5)
    except FutTimeout:
        return False
