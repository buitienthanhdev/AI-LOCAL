# Desktop control: how the agent sees and operates Windows applications

The planner never moves the mouse itself. It names tools; the Python worker (`services/python-agent/lga_worker/
windows/`) executes them on one STA thread and reads the result back from Windows. This page describes the
guarantees that layer gives after the 2026-10-10 upgrade, and why they exist.

## The working window

`desktop.launch_app` (and `desktop.focus_window`, a click into a window) sets the *working window*. All keyboard
input (`desktop.type`, `press_key`, `hotkey`, `save_as`) goes to that window or to a dialog it owns, never
anywhere else: before every key the worker brings the working window to the front (`win32.force_foreground`,
which attaches input queues and falls back to the Alt+Tab path) and refuses with `CONFLICT` when Windows still
keeps another window in front. The old behaviour typed into whatever was focused (a YouTube tab once received
a whole note and a Ctrl+S).

`launch_app` recognises the window of packaged apps shown through `ApplicationFrameHost` (Calculator), of stubs
that exit at once (`calc.exe`, `notepad.exe`), and of Windows 11 Notepad when it reopens the previous session's
tabs; in that case it opens a fresh *Untitled* tab (Ctrl+N) so nothing is typed into another document, and says
so in `data.note`.

## Observation after every action

After every hands-on step (launch, focus, click, type, keys, drag, scroll, save, close) the core calls
`desktop.observe` and gives the planner:

* the working window and whatever is in front of it;
* `ATTENTION:` when a dialog opened (a separate window of the same process, or a dialog drawn inside the
  window such as Notepad's *Save changes?*), when the working window closed, or when another application took
  the foreground. The rest of the planner's batch is **not run** in that case: it was planned for a screen that
  no longer exists;
* the focused control and its text;
* the text the window shows (`window_texts`: labels, status bars, document contents, a calculator's display);
* the interactive controls in reading order with state (`[focused]`, `[on]`, `[expanded]`, `[selected]`);
* a screenshot of the working and foreground windows, attached to the next planner call when the brain can
  see images (the cloud Gemma 4 and the Gemini API can; a local text-only model gets text only).

The observation is written to the audit log (`kind: "observe"`), so a failed GUI task can be replayed.

## Verified operations

| Tool | What it proves |
|---|---|
| `desktop.type` | the typed text is present in the named element, or in the focused control when it exposes text (editors do); otherwise `deferred` |
| `desktop.save_as` | drives the application's own *Save As* dialog (Ctrl+Shift+S, F12, Ctrl+S), fills the full path into the file-name box (`AutomationId 1001`), confirms, answers the overwrite question only with `overwrite=true`, and fails unless the file exists afterwards with a fresh modification time |
| `desktop.close_app` | the window is gone; a *Save changes?* question is reported with its buttons, or answered with *Don't save* when `discard=true` |
| `desktop.read_window_text` | the text a window displays (no screenshot); the way to read a result off the screen |
| `desktop.launch_app` | a window of the launched app appeared and became the working window |

## Measured on Windows 11 (2026-10)

* Notepad 11: Save As dialog is a classic `#32770` owned by the main window; file name = `Edit 1001`, Save =
  `Button 1`; the *Save changes?* prompt is drawn inside the window (XAML) with buttons *Save* / *Don't save* /
  *Cancel*; with session restore on, closing a window with unsaved tabs may not prompt at all (the tabs come
  back next time).
* Calculator: `calc.exe` exits at once; the window belongs to `ApplicationFrameHost.exe` / `CalculatorApp.exe`.
* `SetForegroundWindow` from the worker is refused unless its thread is attached to the foreground thread.

## Tests

* Pure logic: `services/python-agent/tests/` (`uv run pytest`).
* Live, on this desktop (opens its own windows, never touches the user's): `logs/dev/probe_saveas2.py`,
  `logs/dev/probe_close3.py` (dev helpers, git-ignored) and the GUI category of the benchmark:
  `python tests/bench/run_bench.py --profile product --categories gui --repeat 2`.
