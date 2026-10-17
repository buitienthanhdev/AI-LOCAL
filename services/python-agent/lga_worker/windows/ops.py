"""Higher-level, verified desktop operations built on the Windows provider's primitives.

Every method runs on the provider's STA thread (the tool layer calls them through `provider.executor`).
They exist because the planner used to assemble these from raw clicks and hotkeys and could not tell when a
step had gone wrong (a Save dialog that saved under the old name, a "Save changes?" prompt left behind, keys
typed into another window). Each operation here reads its own result back from Windows.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import win32con
import win32gui

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.windows import win32
from lga_worker.windows.feedback import no_change_note, observation_signature
from lga_worker.windows.provider import CloseResult, ElementQuery, Rect, UIElement, WindowInfo

SAVE_DIALOG_WAIT_S = 4.0
DIALOG_CLASS = "#32770"
# The common file dialog's controls (stable AutomationIds since Vista).
FILE_NAME_EDIT_ID = "1001"
SAVE_BUTTON_ID = "1"
FILE_TYPE_COMBO_ID = "FileTypeControlHost"
CONFIRM_YES_ID = "6"
OVERWRITE_WORDS = ("already exists", "replace", "đã tồn tại", "thay thế", "ghi đè")
DISCARD_BUTTON_NAMES = ("don't save", "don’t save", "do not save", "không lưu", "discard", "no")
SAVE_BUTTON_NAMES = ("save", "lưu", "yes")
# Office opens Backstage on Ctrl+S and the Styles pane on Ctrl+Shift+S; F12 is its classic Save As dialog.
OFFICE_PROCESSES = frozenset({"winword.exe", "excel.exe", "powerpnt.exe"})
SAVE_HOTKEYS = (["ctrl", "shift", "s"], ["f12"], ["ctrl", "s"])
OFFICE_SAVE_HOTKEYS = (["f12"], ["ctrl", "shift", "s"])
WINDOW_POLL_S = 0.15
MAX_DISCARD_PRESSES = 12
OBSERVE_TEXT_ITEMS = 40
OBSERVE_TEXT_BUDGET_S = 2.0
# Processes whose foreground window is never a "surprise" (the shell, the agent's own UI).
QUIET_PROCESSES = frozenset({"explorer.exe", "agent-desktop.exe", "searchhost.exe", "shellexperiencehost.exe"})


def _union(a: Rect, b: Rect) -> Rect:
    return Rect(min(a.left, b.left), min(a.top, b.top), max(a.right, b.right), max(a.bottom, b.bottom))


def _pick(elements: list[UIElement], names: tuple[str, ...]) -> UIElement | None:
    """The first element whose name is one of `names` (exact), else one that contains a longer name as a
    whole phrase. Short names ("no") never match by substring: "No Errors" is not a No button."""
    for wanted in names:
        for e in elements:
            if e.name.strip().lower() == wanted:
                return e
    for wanted in names:
        if len(wanted) < 6:
            continue
        for e in elements:
            if wanted in e.name.strip().lower():
                return e
    return None


class DesktopOpsMixin:
    """Mixed into `WindowsUseProvider`; relies on its primitives (`bring_to_front`, `hotkey`, `click`,
    `find_element`, `write_element`, `window_texts`, `take_screenshot`, `get_ui_tree`, `_target_hwnd`)."""

    # ------------------------------------------------------------------------------------------ dialogs

    def _new_dialog(self, pid: int, before: set[int], wait_s: float) -> WindowInfo | None:
        """A dialog window of `pid` that was not in `before`: a classic #32770, or a window owned by one of ours."""
        deadline = time.monotonic() + wait_s
        while True:
            for w in win32.list_user_windows():
                if w.hwnd in before or w.pid != pid:
                    continue
                owner = win32gui.GetWindow(w.hwnd, win32con.GW_OWNER)
                if w.class_name == DIALOG_CLASS or owner:
                    return w
            if time.monotonic() >= deadline:
                return None
            time.sleep(WINDOW_POLL_S)

    def dialog_buttons(self, hwnd: int) -> tuple[list[UIElement], list[str]]:
        """Buttons and text of a dialog: a separate dialog window, or a dialog drawn inside an app window (Windows
        11 Notepad's 'Save changes?'). For the latter only the dialog's own subtree is reported, not the whole
        window's buttons, so the planner reads the question and its answers."""
        buttons = self.find_element(ElementQuery(window_hwnd=hwnd, control_type="ButtonControl", max_results=24))
        if win32gui.GetClassName(hwnd) != DIALOG_CLASS:
            anchor = _pick(buttons, DISCARD_BUTTON_NAMES)
            if anchor is not None:
                scoped = self._dialog_scope(hwnd, anchor)
                if scoped is not None:
                    return scoped
        texts = self.window_texts(hwnd, max_items=8, max_nodes=800, budget_s=1.0)
        return buttons, texts

    def _dialog_scope(self, hwnd: int, anchor: UIElement) -> tuple[list[UIElement], list[str]] | None:
        """Buttons and texts of the smallest dialog-like ancestor of `anchor` (a ContentDialog / Popup / Pane)."""
        try:
            control = self._resolve(anchor.element_id).control
            scope = control
            found_dialog = False
            for _ in range(8):
                parent = scope.GetParentControl()
                if parent is None:
                    break
                scope = parent
                cls = (scope.ClassName or "").lower()
                kind = (scope.ControlTypeName or "").lower()
                if "dialog" in cls or "popup" in cls or "flyout" in cls or kind == "windowcontrol":
                    found_dialog = True
                    break
            if not found_dialog:
                return None
            title = win32gui.GetWindowText(hwnd)
            buttons: list[UIElement] = []
            texts: list[str] = []
            frontier = [(scope, 0)]
            visited = 0
            while frontier and visited < 400:
                node, depth = frontier.pop(0)
                visited += 1
                kind = node.ControlTypeName or ""
                if kind == "ButtonControl" and node.Name:
                    buttons.append(self._to_element(node, hwnd, title, with_patterns=False))
                elif kind in ("TextControl",) and node.Name and node.Name.strip():
                    texts.append(" ".join(node.Name.split())[:300])
                if depth < 10:
                    frontier.extend((child, depth + 1) for child in node.GetChildren())
            if not buttons:
                return None
            return buttons, texts
        except Exception:  # noqa: BLE001 - UIA hiccup: fall back to the whole window
            return None

    def in_window_dialog(self, hwnd: int) -> dict[str, Any] | None:
        """A 'Save changes?' style prompt drawn inside the window itself (XAML ContentDialog), if one is showing:
        a Don't-save button that sits inside a dialog-like container. Plain windows have no such button."""
        buttons = self.find_element(ElementQuery(window_hwnd=hwnd, control_type="ButtonControl", max_results=30))
        anchor = _pick(buttons, DISCARD_BUTTON_NAMES)
        if anchor is None:
            return None
        scoped = self._dialog_scope(hwnd, anchor)
        if scoped is None:
            return None
        scoped_buttons, texts = scoped
        discard = _pick(scoped_buttons, DISCARD_BUTTON_NAMES) or anchor
        return {"buttons": [b.name for b in scoped_buttons if b.name], "text": " | ".join(texts[:4]), "discard": discard}

    def press_button(self, button: UIElement) -> None:
        if not self.try_invoke(button.element_id):
            x, y = button.rect.center
            self.click(x, y, "left")

    # ------------------------------------------------------------------------------------------ save as

    def _file_name_box(self, dialog_hwnd: int, wait_s: float = 1.5) -> UIElement | None:
        """The file-name edit of a common file dialog, or None when `dialog_hwnd` is not one. The dialog's
        controls appear a moment after its window, so the lookup is repeated for up to `wait_s`."""
        deadline = time.monotonic() + wait_s
        while True:
            edits = self.find_element(ElementQuery(window_hwnd=dialog_hwnd, automation_id=FILE_NAME_EDIT_ID,
                                                   control_type="EditControl", include_offscreen=True, max_results=2))
            if edits:
                return edits[0]
            for e in self.find_element(ElementQuery(window_hwnd=dialog_hwnd, control_type="EditControl",
                                                    include_offscreen=True, max_results=12)):
                label = e.name.lower()
                if "file name" in label or "tên tệp" in label or "tên file" in label:
                    return e
            if time.monotonic() >= deadline or not win32.is_window(dialog_hwnd):
                return None
            time.sleep(0.15)

    def _select_file_type(self, dialog_hwnd: int, ext: str) -> str | None:
        """Pick the 'Save as type' entry that matches the extension of the file name, so the dialog writes that format
        (Paint's list can start on HEIC: a name ending in .png would then hold HEIC bytes). Best effort: returns the
        entry chosen, or None when the dialog has no type list or no entry for `ext` (the caller verifies the file)."""
        if not ext:
            return None
        combos = self.find_element(ElementQuery(window_hwnd=dialog_hwnd, automation_id=FILE_TYPE_COMBO_ID,
                                                include_offscreen=True, max_results=2))
        if not combos:
            return None
        x, y = combos[0].rect.center
        self.click(x, y, "left")
        time.sleep(0.6)
        items = self.find_element(ElementQuery(window_hwnd=dialog_hwnd, control_type="ListItemControl",
                                               include_offscreen=True, max_results=60))
        matches = [i for i in items if ext in re.findall(r"\*(\.\w+)", i.name.lower())]
        if ext == ".bmp":  # several bitmap depths: the full-colour one
            matches = [i for i in matches if "24-bit" in i.name] or matches
        if not matches:
            self.press_key("escape", 1)
            return None
        x, y = matches[0].rect.center
        self.click(x, y, "left")
        time.sleep(0.4)
        return matches[0].name

    def save_as(self, hwnd: int, path: Path, overwrite: bool, timeout_s: float) -> dict[str, Any]:
        """Save the document of `hwnd` to `path` through the application's Save As dialog, and prove it."""
        if not win32.is_window(hwnd):
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"window {hwnd} does not exist")
        if not path.is_absolute():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "path must be absolute")
        if path.exists() and not overwrite:
            raise ToolError(ErrorCode.CONFLICT, f"{path} already exists; pass overwrite=true to replace it")
        info = win32.window_info(hwnd)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.set_target(hwnd)
        if not self.bring_to_front(hwnd):
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"could not bring '{info.title}' to the front", retryable=True)
        started = time.time()
        before = {w.hwnd for w in win32.list_user_windows()}
        dialog: WindowInfo | None = None
        name_box: UIElement | None = None
        used = ""
        tried: list[str] = []
        answered: list[str] = []
        hotkeys = OFFICE_SAVE_HOTKEYS if info.process_name.lower() in OFFICE_PROCESSES else SAVE_HOTKEYS
        for keys in hotkeys:
            tried.append("+".join(keys))
            self.hotkey(keys)
            candidate = self._new_dialog(info.pid, before, SAVE_DIALOG_WAIT_S)
            if candidate is None:
                continue
            name_box = self._file_name_box(candidate.hwnd)
            if name_box is not None:
                dialog, used = candidate, "+".join(keys)
                break
            answered.append(f"'{candidate.title}' [{candidate.class_name}] for {'+'.join(keys)}")
            # Some other window answered the hotkey (Word's Styles pane on Ctrl+Shift+S): put it away and go on.
            before.add(candidate.hwnd)
            self.press_key("escape", 1)
            time.sleep(0.3)
            if win32.is_window(candidate.hwnd):
                win32.post_close(candidate.hwnd)
                time.sleep(0.3)
        if dialog is None or name_box is None:
            seen = f" (windows that opened instead: {'; '.join(answered)})" if answered else ""
            raise ToolError(ErrorCode.EXECUTION_FAILED,
                            f"'{info.title}' showed no Save As dialog for {', '.join(tried)}{seen}; use its File menu "
                            "(desktop.inspect_ui) or the app's own tools (office.* for Word/Excel/PowerPoint)",
                            details={"tried": tried, "answered": answered})
        chosen_type = self._select_file_type(dialog.hwnd, path.suffix.lower())
        name_box = self._file_name_box(dialog.hwnd) or name_box  # the list rewrites the name: look the box up again
        wanted = str(path)
        # after a type was picked the box ignores a value set through UI Automation (measured on Paint 11: the dialog
        # then saved 'Untitled.png' in its own folder): type the path instead
        content = None if chosen_type else self.write_element(name_box.element_id, wanted)
        if content is None or (content.value or "").strip().strip('"') != wanted:
            self.focus_element(name_box.element_id)
            self.hotkey(["ctrl", "a"])
            self.uia.TypeText(wanted, interval=0.0)
            time.sleep(0.2)
        save = self.find_element(ElementQuery(window_hwnd=dialog.hwnd, automation_id=SAVE_BUTTON_ID,
                                              control_type="ButtonControl", max_results=2))
        if save:
            x, y = save[0].rect.center
            self.click(x, y, "left")  # a real click: Invoke on a Win32 button blocks while a confirm box is up
        else:
            self.press_key("enter", 1)
        known = before | {dialog.hwnd}
        deadline = time.monotonic() + timeout_s
        confirmed = False
        while time.monotonic() < deadline:
            if not win32.is_window(dialog.hwnd):
                break
            extra = self._new_dialog(info.pid, known, 0.0)
            if extra is not None:
                known.add(extra.hwnd)
                buttons, texts = self.dialog_buttons(extra.hwnd)
                message = " | ".join(texts)
                asks_overwrite = any(w in message.lower() for w in OVERWRITE_WORDS)
                yes = next((b for b in buttons if b.automation_id == CONFIRM_YES_ID), None) or _pick(buttons, ("yes", "có"))
                if asks_overwrite and overwrite and yes is not None and not confirmed:
                    confirmed = True
                    x, y = yes.rect.center
                    self.click(x, y, "left")
                    continue
                self.press_key("escape", 1)
                time.sleep(0.3)
                if win32.is_window(dialog.hwnd):
                    self.press_key("escape", 1)
                raise ToolError(ErrorCode.EXECUTION_FAILED, f"the Save dialog answered: {message or extra.title}",
                                details={"dialog": extra.to_dict(), "buttons": [b.name for b in buttons]})
            time.sleep(WINDOW_POLL_S)
        else:
            raise ToolError(ErrorCode.TIMEOUT, f"the Save As dialog of '{info.title}' did not close", retryable=False)
        time.sleep(0.3)
        if not path.is_file():
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"the dialog closed but {path} does not exist")
        stat = path.stat()
        if stat.st_mtime < started - 2:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"{path} was not written (its modification time is old)")
        return {"path": str(path), "size": stat.st_size, "dialog_title": dialog.title, "hotkey": used,
                "window_title": win32gui.GetWindowText(hwnd), "overwritten": confirmed}

    # ------------------------------------------------------------------------------------------ closing

    def close_dialog_aware(self, hwnd: int, force: bool, discard: bool, timeout_s: float):
        """`close_app` plus the 'Save changes?' question: with `discard` the unsaved work is thrown away and the
        window closed; otherwise the question's buttons are reported so the planner can save first."""
        result = self.close_app(hwnd, force, timeout_s)
        if result.closed or force:
            return result
        dialog = result.blocking_dialog
        where = dialog.hwnd if dialog is not None else hwnd
        buttons, texts = self.dialog_buttons(where)
        if dialog is None and _pick(buttons, DISCARD_BUTTON_NAMES) is None:
            return result  # no question showing: the app simply refused to close
        names = [b.name for b in buttons if b.name]
        text = " | ".join(texts[:4])
        if discard:
            button = _pick(buttons, DISCARD_BUTTON_NAMES)
            if button is None:
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                f"the dialog offers {names}, none of which discards; answer it with desktop.click",
                                details={"buttons": names, "text": text})
            # Notepad asks once per unsaved tab (and a window with several tabs asks for each): keep answering
            # "Don't save" until the window is gone or nothing asks any more.
            presses = 0
            deadline = time.monotonic() + max(timeout_s, 3.0) + 2.0 * MAX_DISCARD_PRESSES
            while button is not None and presses < MAX_DISCARD_PRESSES:
                self.press_button(button)
                presses += 1
                settle = time.monotonic() + 1.5
                while time.monotonic() < settle:
                    if not win32.is_window(hwnd):
                        self._forget_target(hwnd)
                        return CloseResult(closed=True, blocking_dialog=None, dialog_buttons=names, dialog_text=text)
                    time.sleep(WINDOW_POLL_S)
                if time.monotonic() >= deadline:
                    break
                where = dialog.hwnd if dialog is not None and win32.is_window(dialog.hwnd) else hwnd
                again, _ = self.dialog_buttons(where)
                button = _pick(again, DISCARD_BUTTON_NAMES)
                if button is None and win32.is_window(hwnd):
                    # no question left but the window stays: ask it to close once more (the tab closed, not the window)
                    win32.post_close(hwnd)
                    time.sleep(0.6)
                    again, _ = self.dialog_buttons(hwnd)
                    button = _pick(again, DISCARD_BUTTON_NAMES)
            if not win32.is_window(hwnd):
                self._forget_target(hwnd)
                return CloseResult(closed=True, blocking_dialog=None, dialog_buttons=names, dialog_text=text)
            raise ToolError(ErrorCode.EXECUTION_FAILED,
                            f"answered 'Don't save' {presses} time(s) but '{win32gui.GetWindowText(hwnd)}' is still open",
                            retryable=True, details={"buttons": names, "text": text, "presses": presses})
        pseudo = dialog if dialog is not None else win32.window_info(hwnd)
        return CloseResult(closed=False, blocking_dialog=pseudo, dialog_buttons=names, dialog_text=text)

    # ------------------------------------------------------------------------------------------ observe

    def observe(self, path: Path, max_elements: int) -> dict[str, Any]:
        """What the planner needs after an action: the working window, what is in front, the focused control,
        the text on screen, the controls, and a screenshot of it all. `surprise` names a state the planner must
        deal with before anything else (the window closed, a dialog opened, another app is in front)."""
        target = self._target_hwnd if self._target_hwnd and win32.is_window(self._target_hwnd) else None
        fg = win32.foreground_hwnd()
        fg_root = (win32gui.GetAncestor(fg, win32con.GA_ROOT) or fg) if fg else None
        working = win32.window_info(target) if target else None
        foreground = win32.window_info(fg_root) if fg_root and win32.is_window(fg_root) else None
        surprise: str | None = None
        if self._target_hwnd and target is None:
            surprise = "the window you were working in has closed"
        elif working and foreground and foreground.hwnd != working.hwnd:
            if foreground.pid == working.pid:
                surprise = f"a dialog or second window of {working.process_name} is in front: '{foreground.title}' (hwnd={foreground.hwnd})"
            elif foreground.process_name.lower() not in QUIET_PROCESSES:
                surprise = f"another window is in front of the one you work in: '{foreground.title}' [{foreground.process_name}]"
        dialog = None
        if target and surprise is None:
            dialog = self.in_window_dialog(target)
            if dialog is not None:
                surprise = f"a dialog inside '{working.title}' asks: {dialog['text'] or '(no text)'} — buttons: {', '.join(dialog['buttons'])}"
        look_at = foreground.hwnd if foreground else target
        focused = self.focused_control_info()
        snapshot = self.get_ui_tree(look_at, max_elements) if look_at else None
        texts = snapshot.texts if snapshot is not None else []
        rect = None
        for w in (working, foreground):
            if w is not None and not w.rect.is_empty() and w.state != "minimized":
                rect = w.rect if rect is None else _union(rect, w.rect)
        shot = None
        if rect is not None:
            try:
                shot = self.take_screenshot(path, rect, None)
            except ToolError:
                shot = None
        result = {
            "working": working.to_dict() if working else None,
            "foreground": foreground.to_dict() if foreground else None,
            "surprise": surprise,
            "dialog_buttons": dialog["buttons"] if dialog else [],
            "focused": focused,
            "texts": texts,
            "elements": [e.to_dict() for e in snapshot.elements] if snapshot else [],
            "truncated": bool(snapshot.truncated) if snapshot else False,
            "screenshot": shot,
        }
        # Two identical observations in a row: the action in between probably did nothing (or needs time).
        signature = observation_signature(result)
        result["no_change"] = no_change_note(getattr(self, "_last_observe_signature", None), signature)
        self._last_observe_signature = signature
        return result

    def element_at(self, x: int, y: int) -> dict[str, Any] | None:
        """The control under a screen point (UI Automation hit test), or None when it cannot be told."""
        try:
            control = self.uia.ControlFromPoint(x, y)
        except Exception:
            return None
        if control is None:
            return None
        return {"name": control.Name or "", "control_type": control.ControlTypeName or "",
                "automation_id": control.AutomationId or ""}
