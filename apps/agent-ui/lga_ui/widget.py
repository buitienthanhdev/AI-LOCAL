"""Floating popup to give the agent tasks and watch it work.

A small always-on-top button sits on the desktop; clicking it (or pressing the global-ish hotkey while
it is focused) opens a chat panel. You type a task, the agent runs it in the real CLI, and the panel
streams each step, asks for confirmations and answers questions inline. The panel drives the same
`TaskClient` as the web UI, so it owns no agent logic.

Run: python -m lga_ui.widget   (or scripts/start-widget.ps1)
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from lga_common.config import Config, load_config

from lga_ui.tasks import TaskClient, find_agent_exe

POLL_MS = 700
BG = "#1c2027"
PANEL = "#242a33"
TEXT = "#e6e8ec"
MUTED = "#9aa3b2"
ACCENT = "#2f6fde"
OK = "#4cc386"
BAD = "#f07272"
WARN = "#e2b04a"
TERMINAL = {"completed", "failed", "cancelled"}
STATUS_VI = {
    "pending": "đang chuẩn bị", "planning": "đang lập kế hoạch", "executing": "đang thao tác",
    "observing": "đang quan sát", "verifying": "đang kiểm chứng", "recovering": "đang khắc phục",
    "needs_user": "cần bạn trả lời", "paused": "đã tạm dừng", "completed": "hoàn thành",
    "failed": "thất bại", "cancelled": "đã huỷ",
}


def short(value: Any, limit: int = 240) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


class ChatPanel(tk.Toplevel):
    """The chat window. One at a time; it follows one task."""

    def __init__(self, master: tk.Tk, client: TaskClient) -> None:
        super().__init__(master)
        self.client = client
        self.task_id: str | None = None
        self.cursor = 0
        self._answered_request: str | None = None
        self._updates: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.title("Local Gemini Agent")
        self.configure(bg=BG)
        self.geometry("460x620")
        self.minsize(360, 420)
        self.attributes("-topmost", True)

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 4))
        self.status_label = tk.Label(header, text="Sẵn sàng", fg=MUTED, bg=BG, font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side="left")
        self.cancel_btn = tk.Button(header, text="Huỷ", command=self._cancel, state="disabled", relief="flat",
                                    bg=PANEL, fg=TEXT, activebackground=ACCENT, bd=0, padx=10)
        self.cancel_btn.pack(side="right")
        self.pause_btn = tk.Button(header, text="Tạm dừng", command=self._toggle_pause, state="disabled",
                                   relief="flat", bg=PANEL, fg=TEXT, activebackground=ACCENT, bd=0, padx=10)
        self.pause_btn.pack(side="right", padx=(0, 6))

        self.feed = tk.Text(self, bg=PANEL, fg=TEXT, wrap="word", bd=0, padx=10, pady=10,
                            font=("Segoe UI", 10), state="disabled", spacing1=2, spacing3=4)
        self.feed.pack(fill="both", expand=True, padx=12, pady=6)
        for name, colour in (("muted", MUTED), ("ok", OK), ("bad", BAD), ("warn", WARN),
                             ("accent", ACCENT), ("user", TEXT)):
            self.feed.tag_configure(name, foreground=colour)
        self.feed.tag_configure("bold", font=("Segoe UI", 10, "bold"))

        # Inline answer row (confirmations / questions). Hidden until needed.
        self.answer_row = tk.Frame(self, bg=BG)
        self.answer_prompt = tk.Label(self.answer_row, text="", fg=WARN, bg=BG, font=("Segoe UI", 9),
                                      wraplength=430, justify="left")
        self.answer_prompt.pack(fill="x", padx=12)
        self.answer_controls = tk.Frame(self.answer_row, bg=BG)
        self.answer_controls.pack(fill="x", padx=12, pady=4)

        entry_row = tk.Frame(self, bg=BG)
        entry_row.pack(fill="x", padx=12, pady=(4, 10))
        self.mode = tk.StringVar(value=client.config.data["agent"]["mode"])
        ttk.Combobox(entry_row, textvariable=self.mode, values=["normal", "trusted", "full_access"],
                     width=11, state="readonly").pack(side="left")
        self.input = tk.Text(entry_row, height=2, bg=PANEL, fg=TEXT, insertbackground=TEXT, bd=0,
                             font=("Segoe UI", 10), padx=8, pady=6, wrap="word")
        self.input.pack(side="left", fill="x", expand=True, padx=6)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<Shift-Return>", lambda e: None)
        tk.Button(entry_row, text="Gửi", command=self._send, relief="flat", bg=ACCENT, fg="white",
                  activebackground="#255bbd", bd=0, padx=14).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.after(POLL_MS, self._poll)
        self.after(60, self._drain)
        self.input.focus_set()

    # -- feed helpers -----------------------------------------------------------------------
    def _write(self, text: str, tag: str = "") -> None:
        self.feed.configure(state="normal")
        self.feed.insert("end", text + "\n", tag)
        self.feed.see("end")
        self.feed.configure(state="disabled")

    def _on_return(self, event: tk.Event) -> str:
        if event.state & 0x0001:  # Shift held: newline
            return ""
        self._send()
        return "break"

    def _send(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        if self.task_id and self.client.record(self.task_id) and \
                self.client.record(self.task_id)["status"] not in TERMINAL:
            self._write("Đang có việc chạy dở; đợi xong đã nhé.", "warn")
            return
        self.input.delete("1.0", "end")
        self.feed.configure(state="normal")
        self.feed.delete("1.0", "end")
        self.feed.configure(state="disabled")
        self._write(f"Bạn: {text}", "user")
        self.cursor = 0
        self._answered_request = None
        try:
            self.task_id = self.client.start(text, self.mode.get())
        except (ValueError, FileNotFoundError) as exc:
            self._write(f"Không khởi động được: {exc}", "bad")

    def _cancel(self) -> None:
        if self.task_id:
            self.client.control(self.task_id, "cancel")

    def _toggle_pause(self) -> None:
        if not self.task_id:
            return
        record = self.client.record(self.task_id)
        action = "unpause" if record and record.get("status") == "paused" else "pause"
        self.client.control(self.task_id, action)

    # -- polling (reads happen on a worker thread; UI updates via the queue) -----------------
    def _poll(self) -> None:
        task_id = self.task_id
        if task_id:
            threading.Thread(target=self._fetch, args=(task_id,), daemon=True).start()
        self.after(POLL_MS, self._poll)

    def _fetch(self, task_id: str) -> None:
        record = self.client.record(task_id)
        if record is None:
            return
        events, cursor = self.client.events(task_id, self.cursor)
        self.cursor = cursor
        pending = self.client.pending_request(task_id)
        self._updates.put(("update", (record, events, pending)))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._updates.get_nowait()
                if kind == "update":
                    self._apply(*payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _apply(self, record: dict[str, Any], events: list[dict[str, Any]], pending: dict[str, Any] | None) -> None:
        status = record.get("status", "")
        self.status_label.configure(text=STATUS_VI.get(status, status),
                                    fg=OK if status == "completed" else BAD if status in ("failed", "cancelled")
                                    else WARN if status in ("needs_user", "paused") else ACCENT)
        active = status not in TERMINAL
        self.cancel_btn.configure(state="normal" if active else "disabled")
        self.pause_btn.configure(state="normal" if active else "disabled",
                                 text="Tiếp tục" if status == "paused" else "Tạm dừng")
        for event in events:
            self._render_event(event)
        if pending and pending.get("id") != self._answered_request:
            self._show_request(pending)
        elif not pending:
            self._hide_request()
        if status in TERMINAL:
            summary = record.get("final_summary") or record.get("failure_reason") or ""
            tag = "ok" if status == "completed" else "bad"
            if events or summary:
                self._write(f"— {STATUS_VI.get(status, status)}: {summary}", tag)

    def _render_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "step_started":
            detail = event.get("detail", {})
            thought = detail.get("thought")
            if thought:
                self._write(short(thought, 300), "muted")
            self._write(f"→ {event.get('tool', '')} {short(detail.get('args', ''), 140)}", "accent")
        elif kind == "step":
            detail = event.get("detail", {})
            err = detail.get("error")
            if err:
                self._write(f"   ✕ {err.get('code')}: {short(err.get('message'), 200)}", "bad")
            else:
                verification = detail.get("verification")
                self._write(f"   ✓ {detail.get('tool', '')} ({verification or 'ok'})", "ok")
                obs = detail.get("observation")
                if obs:
                    self._write(f"   {short(obs, 240)}", "muted")
        elif kind == "plan":
            plan = event.get("detail", {}).get("plan", [])
            self._write("Kế hoạch: " + " → ".join(short(p, 60) for p in plan), "muted")
        elif kind == "completion_verifier":
            verdict = event.get("verification_result")
            if verdict == "reject":
                self._write("   Trình kiểm chứng từ chối lần báo hoàn thành; làm tiếp.", "warn")

    # -- inline confirmation / question ------------------------------------------------------
    def _show_request(self, pending: dict[str, Any]) -> None:
        for child in self.answer_controls.winfo_children():
            child.destroy()
        request_id = pending.get("id")
        if pending.get("kind") == "confirm":
            reasons = "; ".join(pending.get("reasons", []))
            self.answer_prompt.configure(text=f"Cho phép chạy {pending.get('tool')}? {reasons}")
            tk.Button(self.answer_controls, text="Cho phép", bg=OK, fg="white", relief="flat", bd=0, padx=12,
                      command=lambda: self._answer(request_id, approve=True)).pack(side="left")
            tk.Button(self.answer_controls, text="Từ chối", bg=BAD, fg="white", relief="flat", bd=0, padx=12,
                      command=lambda: self._answer(request_id, approve=False)).pack(side="left", padx=6)
        else:
            self.answer_prompt.configure(text=pending.get("question", ""))
            box = tk.Entry(self.answer_controls, bg=PANEL, fg=TEXT, insertbackground=TEXT, bd=0, font=("Segoe UI", 10))
            box.pack(side="left", fill="x", expand=True, ipady=4)
            box.focus_set()
            box.bind("<Return>", lambda e: self._answer(request_id, answer=box.get()))
            tk.Button(self.answer_controls, text="Gửi", bg=ACCENT, fg="white", relief="flat", bd=0, padx=12,
                      command=lambda: self._answer(request_id, answer=box.get())).pack(side="right")
        self.answer_row.pack(fill="x", after=self.feed)

    def _hide_request(self) -> None:
        self.answer_row.pack_forget()

    def _answer(self, request_id: str | None, approve: bool | None = None, answer: str | None = None) -> None:
        if self.task_id and self.client.respond(self.task_id, approve=approve, answer=answer):
            self._answered_request = request_id
            verdict = "Cho phép" if approve else "Từ chối" if approve is False else f"Trả lời: {answer}"
            self._write(f"   [bạn] {verdict}", "muted")
        self._hide_request()


class FloatingButton:
    """A small always-on-top button that toggles the chat panel."""

    def __init__(self, client: TaskClient) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = ChatPanel(self.root, client)
        self.panel.withdraw()

        self.button = tk.Toplevel(self.root)
        self.button.overrideredirect(True)
        self.button.attributes("-topmost", True)
        self.button.configure(bg=ACCENT)
        screen_w = self.button.winfo_screenwidth()
        screen_h = self.button.winfo_screenheight()
        self.button.geometry(f"120x44+{screen_w - 150}+{screen_h - 130}")
        label = tk.Label(self.button, text="● Agent", bg=ACCENT, fg="white", font=("Segoe UI", 11, "bold"),
                         cursor="hand2")
        label.pack(expand=True, fill="both")
        for widget in (self.button, label):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)
        self._drag_from: tuple[int, int] | None = None
        self._moved = False

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_from = (event.x_root - self.button.winfo_x(), event.y_root - self.button.winfo_y())
        self._moved = False

    def _drag(self, event: tk.Event) -> None:
        if self._drag_from is None:
            return
        self._moved = True
        self.button.geometry(f"+{event.x_root - self._drag_from[0]}+{event.y_root - self._drag_from[1]}")

    def _end_drag(self, event: tk.Event) -> None:
        self._drag_from = None
        if not self._moved:
            self._toggle_panel()

    def _toggle_panel(self) -> None:
        if self.panel.state() == "withdrawn":
            self.panel.deiconify()
            self.panel.lift()
            self.panel.input.focus_set()
        else:
            self.panel.withdraw()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    config: Config = load_config()
    client = TaskClient(config=config, agent_exe=find_agent_exe(config.root))
    FloatingButton(client).run()


if __name__ == "__main__":
    main()
