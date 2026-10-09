"""Nút AI nổi trên desktop (kiểu picture-in-picture).

- Nút tròn (Tk + Pillow): bể nước = % kiến thức đã học (đỏ/vàng/xanh), sóng, bọt khí, quầng sáng khi đang học,
  vòng sáng chạy khi đang trả lời. Rê chuột: xem đang học gì. Chuột phải: menu nhanh. Kéo thả để di chuyển.
- Bấm (hoặc Ctrl+Shift+Space ở bất kỳ đâu): mở khung chat hiện đại (panel.py — cửa sổ WebView2 bo tròn,
  trong suốt) với các tab Chat / Học / Nhớ / Tri thức / Hệ thống.
Nút không chạm trực tiếp dữ liệu: mọi thứ đi qua web server cục bộ (tự khởi động nếu chưa chạy)."""
import ctypes
import ctypes.wintypes
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

import requests
from PIL import ImageTk

from .config import DATA_DIR, ROOT, WEB_PORT
from .widget_art import BubbleArt

API = f"http://127.0.0.1:{WEB_PORT}"
PREFS_FILE = DATA_DIR / "widget.json"
KEY = "#010203"          # màu nền được làm trong suốt (khớp KEY_RGB ở widget_art)
BUBBLE = 72
PANEL_W, PANEL_H = 452, 672
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
C = {"card": "#1d2430", "line": "#2a3340", "text": "#e6e9ef", "accent": "#5b8cff"}
FS = ("Segoe UI", 9)

UIQ: queue.Queue = queue.Queue()


def ui(fn, *args):
    """Gọi hàm giao diện từ luồng nền: đưa vào hàng đợi, luồng chính (vòng hoạt ảnh) sẽ chạy."""
    UIQ.put((fn, args))


# ---------------------------------------------------------------- máy chủ / tiện ích
def server_alive() -> bool:
    try:
        return requests.get(f"{API}/api/health", timeout=1).ok
    except requests.RequestException:
        return False


def ensure_server():
    if server_alive():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    log = open(DATA_DIR / "server.log", "a", encoding="utf-8")
    subprocess.Popen([exe, "-m", "brain", "serve"], cwd=ROOT, stdout=log, stderr=log, creationflags=NOWIN)
    for _ in range(60):
        time.sleep(1)
        if server_alive():
            return
    raise RuntimeError("Không khởi động được máy chủ, xem data/server.log")


def api_get(path: str, timeout=10) -> dict:
    return requests.get(API + path, timeout=timeout).json()


def api_post(path: str, body: dict, timeout=60) -> dict:
    r = requests.post(API + path, json=body, timeout=timeout)
    data = r.json()
    if not r.ok:
        raise RuntimeError(data.get("error", r.status_code))
    return data


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p: Path, obj):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- giọng nói (dùng ở panel.py)
class Recorder:
    """Ghi âm micro (16 kHz mono) trong lúc giữ nút 🎤."""
    RATE = 16000

    def __init__(self):
        self.frames, self.stream, self.error = [], None, None

    @staticmethod
    def _devices() -> list:
        """Các thiết bị thu âm, ưu tiên micro mặc định của Windows rồi tới WASAPI/MME (bỏ 'Stereo Mix')."""
        import sounddevice as sd
        out = []
        try:
            if sd.default.device[0] is not None and sd.default.device[0] >= 0:
                out.append(sd.default.device[0])
        except Exception:
            pass
        apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
        order = ("Windows WASAPI", "MME", "Windows DirectSound", "Windows WDM-KS")
        cands = [(i, d) for i, d in enumerate(sd.query_devices())
                 if d["max_input_channels"] > 0 and "stereo mix" not in d["name"].lower()]
        cands.sort(key=lambda x: order.index(apis[x[1]["hostapi"]]) if apis.get(x[1]["hostapi"]) in order else 9)
        return out + [i for i, _ in cands if i not in out]

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as e:
            self.error = f"Thiếu thư viện ghi âm: {e}"
            return False
        self.frames = []
        for dev in self._devices():  # thử lần lượt tới khi mở được
            try:
                self.stream = sd.InputStream(device=dev, samplerate=self.RATE, channels=1, dtype="int16",
                                             callback=lambda data, *_: self.frames.append(bytes(data)))
                self.stream.start()
                return True
            except Exception:
                self.stream = None
        self.error = ("Không tìm thấy micro nào dùng được. Hãy cắm micro/tai nghe có mic và bật trong "
                      "Windows Settings › Privacy › Microphone, rồi thử lại.")
        return False

    def stop(self) -> bytes | None:
        import wave
        if not self.stream:
            return None
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.stream = None
        raw = b"".join(self.frames)
        if len(raw) < self.RATE * 2 * 0.4:  # < 0,4 giây
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(raw)
        return buf.getvalue()


class Speaker:
    """Đọc to câu trả lời bằng giọng tiếng Việt của Windows (Microsoft An)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="tts").start()

    def say(self, text: str):
        self.q.put(text)

    def stop(self):
        self.q.put("")

    def _loop(self):
        try:
            import pythoncom
            import win32com.client as wc
            pythoncom.CoInitialize()
            voice = wc.Dispatch("SAPI.SpVoice")
            cat = wc.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
            toks = cat.EnumerateTokens()
            for i in range(toks.Count):
                if "vietnam" in toks.Item(i).GetDescription().lower():
                    voice.Voice = toks.Item(i)
                    break
            voice.Rate = 1
        except Exception:
            return
        while True:
            text = self.q.get()
            try:
                voice.Speak(text or " ", 1 | 2)  # bất đồng bộ + ngắt câu đang đọc
            except Exception:
                pass


def speakable(md: str) -> str:
    md = md.split("Nguồn:")[0]
    md = re.sub(r"`[^`]*`|\([A-Z]:\\[^)]*\)|[A-Z]:\\\S+", "", md)
    md = re.sub(r"[*#_>|]", "", md)
    return re.sub(r"\s+", " ", md).strip()[:1200]


# ---------------------------------------------------------------- phím tắt toàn cục
def start_hotkey(callback):
    """Ctrl+Shift+Space ở bất kỳ đâu => mở/đóng khung chat."""
    def loop():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, 0x0002 | 0x0004 | 0x4000, 0x20):  # CTRL|SHIFT|NOREPEAT, SPACE
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                callback()
    threading.Thread(target=loop, daemon=True, name="hotkey").start()


# ---------------------------------------------------------------- khung chat (tiến trình WebView)
class Panel:
    """Điều khiển tiến trình `python -m brain panel` qua stdin/stdout."""

    def __init__(self, on_hidden, on_quit):
        self.proc = None
        self.visible = False
        self.on_hidden, self.on_quit = on_hidden, on_quit

    def _spawn(self):
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        self.proc = subprocess.Popen([exe, "-m", "brain", "panel"], cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=open(DATA_DIR / "panel.log", "a"),
                                     text=True, encoding="utf-8", bufsize=1, creationflags=NOWIN)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="panel-out").start()

    def _read(self, proc):
        for line in proc.stdout:
            s = line.strip()
            if s == "hidden":
                self.visible = False
                ui(self.on_hidden)
            elif s == "quit":
                self.visible = False
                ui(self.on_quit)
        if self.visible:  # tiến trình chết bất thường => hiện lại nút tròn
            self.visible = False
            ui(self.on_hidden)

    def send(self, line: str):
        if not self.proc or self.proc.poll() is not None:
            self._spawn()
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except OSError:
            self._spawn()
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def preload(self):
        if not self.proc or self.proc.poll() is not None:
            self._spawn()

    def show(self, x, y, w, h, tab=None):
        self.visible = True
        self.send(f"show {x} {y} {w} {h}" + (f" {tab}" if tab else ""))

    def hide(self):
        self.visible = False
        self.send("hide")

    def close(self):
        try:
            self.send("quit")
        except Exception:
            pass


# ---------------------------------------------------------------- ứng dụng
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        self.root.attributes("-transparentcolor", KEY)
        self.prefs = load_json(PREFS_FILE, {})
        self.level, self.shown, self.learning, self.busy = 0.0, 0.0, False, False
        self.progress_info = {}
        self.dot = (234, 179, 8)
        self.hover = False
        self.t0 = time.time()
        self.art = BubbleArt(BUBBLE)
        self._drag = None
        self._moved = False
        self._tip = None
        self._tip_job = None
        self.panel = Panel(on_hidden=self._panel_hidden, on_quit=self.quit)

        self.bubble = tk.Label(self.root, bg=KEY, bd=0, cursor="hand2")
        self.bubble.pack()
        self._bind_drag(self.bubble, click=self.toggle)
        self.bubble.bind("<Enter>", self._enter)
        self.bubble.bind("<Leave>", self._leave)
        self.bubble.bind("<Button-3>", self._menu)

        x, y = self.prefs.get("x"), self.prefs.get("y")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if x is None or not (0 <= x < sw - 20 and 0 <= y < sh - 20):
            x, y = sw - self.art.full - 24, sh - self.art.full - 80
        self.root.geometry(f"{self.art.full}x{self.art.full}+{x}+{y}")
        self.root.deiconify()

        threading.Thread(target=self._start_server, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        self._hotkey_hit = threading.Event()
        start_hotkey(self._hotkey_hit.set)
        self.root.after(1500, self.panel.preload)  # nạp sẵn khung chat => lần mở đầu tức thì
        self._animate()

    # ---------- mở / đóng khung
    def toggle(self, tab=None):
        if self.panel.visible:
            self.panel.hide()
            self._panel_hidden()
            return
        self._hide_tip()
        p = load_json(PREFS_FILE, {})
        w, h = p.get("pw", PANEL_W), p.get("ph", PANEL_H)
        full = self.art.full
        bx, by = self.root.winfo_x(), self.root.winfo_y()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = min(max(0, bx + full - w), sw - w)  # khung "mọc" ra từ vị trí nút tròn
        y = min(max(0, by + full - h), sh - h)
        self.panel.show(x, y, w, h, tab)
        self.root.withdraw()

    def open_tab(self, tab):
        if self.panel.visible:
            self.panel.send(f"tab {tab}")
        else:
            self.toggle(tab)

    def _panel_hidden(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def quit(self):
        self.panel.close()
        self.root.destroy()

    # ---------- kéo thả nút
    def _bind_drag(self, w, click=None):
        def press(e):
            self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
            self._moved = False
            self._hide_tip()

        def motion(e):
            if self._drag:
                nx, ny = e.x_root - self._drag[0], e.y_root - self._drag[1]
                if abs(nx - self.root.winfo_x()) + abs(ny - self.root.winfo_y()) > 2:
                    self._moved = True
                self.root.geometry(f"+{nx}+{ny}")

        def release(e):
            self._drag = None
            if self._moved:
                self.prefs = load_json(PREFS_FILE, {})
                self.prefs["x"], self.prefs["y"] = self.root.winfo_x(), self.root.winfo_y()
                save_json(PREFS_FILE, self.prefs)
            elif click:
                click()
        w.bind("<ButtonPress-1>", press)
        w.bind("<B1-Motion>", motion)
        w.bind("<ButtonRelease-1>", release)

    # ---------- rê chuột / menu
    def _enter(self, e):
        self.hover = True
        self._tip_job = self.root.after(450, self._show_tip)

    def _leave(self, e):
        self.hover = False
        self._hide_tip()

    def _tip_text(self):
        p = self.progress_info
        if not p:
            return "Company Brain — đang kết nối…"
        j = p.get("job", {})
        s = f"Đã học {p.get('percent', 0)}%  ·  {p.get('learned', 0):,}/{p.get('total', 0):,} tệp"
        cur = j.get("current_path")
        if j.get("running"):
            s += "\n" + ("⚡ Đang học ngay" if j.get("force") else "Đang học") + (
                f": {os.path.basename(cur)}" if cur else f" — {j.get('message', '')[:60]}")
        else:
            s += "\nĐang nghỉ (học tiếp khi máy rảnh)"
        lim = p.get("limit") or {}
        if lim.get("mode"):
            s += f"\nNhịp học: {lim.get('note')} (~{lim.get('target')}%)" + (
                " · tạm dừng vì RAM gần đầy" if lim.get("paused_ram") else "")
        return s + "\nBấm để chat · Chuột phải: menu · Ctrl+Shift+Space"

    def _show_tip(self):
        if not self.hover or self._drag or self.panel.visible:
            return
        self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self._tip_text(), bg=C["card"], fg=C["text"], font=FS, justify="left", padx=10, pady=6,
                 highlightbackground=C["line"], highlightthickness=1).pack()
        tip.update_idletasks()
        x = self.root.winfo_x() - tip.winfo_width() - 6
        if x < 0:
            x = self.root.winfo_x() + self.art.full + 6
        tip.geometry(f"+{x}+{self.root.winfo_y() + 8}")
        self._tip = tip

    def _hide_tip(self):
        if self._tip_job:
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def _menu(self, e):
        self._hide_tip()
        m = tk.Menu(self.root, tearoff=0, bg=C["card"], fg=C["text"], activebackground=C["accent"],
                    activeforeground="white", bd=0)
        m.add_command(label="💬  Mở chat", command=lambda: self.open_tab("chat"))
        m.add_command(label="📊  Tiến độ học", command=lambda: self.open_tab("learn"))
        m.add_command(label="🧠  Bộ nhớ", command=lambda: self.open_tab("memory"))
        m.add_command(label="📚  Tri thức", command=lambda: self.open_tab("facts"))
        m.add_separator()
        m.add_command(label="⚡  Học ngay toàn bộ", command=self._learn_now)
        m.add_command(label="🌐  Mở trang web", command=lambda: webbrowser.open(API))
        m.add_separator()
        m.add_command(label="✕  Thoát nút AI", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def _learn_now(self):
        self.root.attributes("-topmost", False)
        ok = messagebox.askyesno("Học ngay", "Học tối đa: dùng hết GPU/CPU, chạy song song, không nhường máy "
                                             "(máy có thể chậm). Dừng lại bằng nút trong tab Học. Tiếp tục?",
                                 parent=self.root)
        self.root.attributes("-topmost", True)
        if ok:
            threading.Thread(target=lambda: api_post("/api/reindex", {"force": True}), daemon=True).start()

    # ---------- dữ liệu nền
    def _start_server(self):
        try:
            ensure_server()
            self.dot = (34, 197, 94)
        except Exception:
            self.dot = (239, 68, 68)

    def _poll(self):
        while True:
            try:
                p = api_get("/api/progress", timeout=5)
                self.progress_info = p
                self.level = float(p.get("percent", 0))
                self.learning = bool(p.get("job", {}).get("running"))
                self.dot = (34, 197, 94)
            except (requests.RequestException, ValueError):
                if self.dot == (34, 197, 94):
                    self.dot = (234, 179, 8)
            time.sleep(3)

    # ---------- hoạt ảnh
    def _animate(self):
        while not UIQ.empty():
            fn, args = UIQ.get_nowait()
            try:
                fn(*args)
            except tk.TclError:
                pass
        if self._hotkey_hit.is_set():
            self._hotkey_hit.clear()
            self.toggle()
        t = time.time() - self.t0
        self.shown += (self.level - self.shown) * 0.08
        active = self.learning or self.hover
        if not self.panel.visible:
            im = self.art.render(self.shown, t * (2.2 if self.learning else 1.0), t, self.learning, self.busy,
                                 self.hover, self.dot)
            self._ph = ImageTk.PhotoImage(im)
            self.bubble.config(image=self._ph)
        self.root.after(40 if active else 90, self._animate)


def main():
    App().root.mainloop()
