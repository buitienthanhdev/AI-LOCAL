"""Khung chat hiện đại của nút AI: cửa sổ WebView2 (Edge) không viền, bo tròn, trong suốt, luôn nằm trên.

Chạy thành tiến trình riêng (python -m brain panel) vì WebView cần luồng chính riêng, tách khỏi Tk của nút tròn.
Giao tiếp với nút tròn qua stdin/stdout:
  vào:  "show X Y W H [tab]" | "hide" | "toggle X Y W H" | "quit"
  ra:   "hidden" (người dùng thu nhỏ khung) | "quit"
Trang giao diện (web/panel.html) do máy chủ cục bộ phục vụ; các việc cần quyền máy (mở file, chọn file, micro,
đọc to, đổi cỡ) đi qua cầu nối js_api bên dưới."""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests
import webview

from .config import DATA_DIR, WEB_PORT
from .widget import PREFS_FILE, Recorder, Speaker, ensure_server, load_json, save_json, speakable

API = f"http://127.0.0.1:{WEB_PORT}"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MIME = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp", ".bmp": "bmp", ".gif": "gif"}


def out(msg: str):
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


class Bridge:
    """Các hàm JS gọi được qua window.pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._rec = Recorder()
        self._speaker = Speaker()

    # ---- cửa sổ
    def hide(self):
        self._window.hide()
        out("hidden")

    def quit(self):
        out("quit")
        self._window.destroy()

    def resize(self, w, h):
        self._window.resize(int(w), int(h))

    def save_size(self, w, h):
        p = load_json(PREFS_FILE, {})
        p["pw"], p["ph"] = int(w), int(h)
        save_json(PREFS_FILE, p)

    def get_prefs(self):
        return load_json(PREFS_FILE, {})

    def set_pref(self, key, value):
        p = load_json(PREFS_FILE, {})
        p[key] = value
        save_json(PREFS_FILE, p)

    # ---- máy
    def open_path(self, path):
        try:
            os.startfile(path)
        except OSError as e:
            return str(e)
        return "ok"

    def open_url(self, url):
        webbrowser.open(url)

    def open_data_dir(self):
        os.startfile(DATA_DIR)

    def ensure_server(self):
        try:
            ensure_server()
            return True
        except RuntimeError:
            return False

    def restart_server(self):
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                        "Where-Object { $_.CommandLine -like '*-m brain serve*' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                       capture_output=True, creationflags=0x08000000)
        return self.ensure_server()

    def pick_files(self):
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=(
            "Ảnh, audio, video (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif;*.mp3;*.wav;*.m4a;*.ogg;*.flac;*.mp4;*.mov;"
            "*.mkv;*.avi;*.webm)", "Tất cả (*.*)")) or []
        files = []
        for p in list(paths)[:4]:
            p = Path(p)
            if not p.is_file() or p.stat().st_size > 15 * 1024 * 1024:
                continue
            data = base64.b64encode(p.read_bytes()).decode()
            ext = p.suffix.lower()
            files.append({"name": p.name, "data": data,
                          "preview": f"data:image/{MIME[ext]};base64,{data}" if ext in IMG_EXT else None})
        return files

    # ---- giọng nói
    def rec_start(self):
        ok = self._rec.start()
        return {"ok": ok, "error": None if ok else self._rec.error}

    def rec_stop(self):
        wav = self._rec.stop()
        if not wav:
            return {"text": "", "error": "Ghi âm quá ngắn — giữ nút lâu hơn khi nói."}
        try:
            r = requests.post(f"{API}/api/transcribe", json={"data": base64.b64encode(wav).decode()}, timeout=300)
            return {"text": r.json().get("text", "")}
        except requests.RequestException as e:
            return {"text": "", "error": f"Lỗi chép lời: {e}"}

    def speak(self, text):
        self._speaker.say(speakable(text))

    def stop_speak(self):
        self._speaker.stop()


def _round_corners(window):
    """Windows 11: bo góc cửa sổ không viền bằng DWM (đẹp hơn, có bóng đổ hệ thống)."""
    try:
        import ctypes
        hwnd = int(str(window.native.Handle))
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _stdin_loop(window):
    _round_corners(window)
    for line in sys.stdin:
        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0]
        try:
            if cmd in ("show", "toggle") and len(parts) >= 5:
                x, y, w, h = map(int, parts[1:5])
                window.resize(w, h)
                window.move(x, y)
                window.show()
                window.restore()
                if len(parts) > 5:
                    window.evaluate_js(f"window.showTab && showTab({json.dumps(parts[5])})")
                window.evaluate_js("document.getElementById('q') && document.getElementById('q').focus()")
            elif cmd == "hide":
                window.hide()
            elif cmd == "tab" and len(parts) > 1:
                window.evaluate_js(f"window.showTab && showTab({json.dumps(parts[1])})")
            elif cmd == "quit":
                window.destroy()
                return
        except Exception as e:
            out(f"error {e}")
    window.destroy()  # nút tròn đã thoát => đóng theo


def main():
    try:
        ensure_server()
    except RuntimeError:
        pass
    bridge = Bridge()
    prefs = load_json(PREFS_FILE, {})
    win = webview.create_window("Company Brain", f"{API}/panel", js_api=bridge, width=prefs.get("pw", 440),
                                height=prefs.get("ph", 660), frameless=True, easy_drag=False, on_top=True,
                                hidden=True, transparent=False, background_color="#111723", min_size=(380, 480))
    bridge._window = win  # thuộc tính '_' => không bị đưa sang JS
    win.events.closed += lambda: out("quit")
    webview.start(_stdin_loop, win, private_mode=False, storage_path=str(DATA_DIR / "webview"))
