# Work tracking (activity.*)

Opt-in, local. `activity.control start` (or the 👁 button in the chat window) creates `memory/activity/enabled.flag` and
starts `python -m lga_worker.activity`, which every 3 s records the program and window title in front and whether the
person is idle (no input for 2 min) into `memory/activity/activity.db` (sessions: start, end, process, title, state).

* Never recorded: keystrokes, screenshots, clipboard, file contents. Titles matching password managers, private
  browsing, banking, sign-in / OTP patterns (and `memory/activity/hide.txt`) are stored as "(ẩn)".
* Kept 30 days; the tracker stops by itself when the flag file disappears.
* Tools: `activity.now` (live; add `desktop.observe` / `vision.ocr` to read the window), `activity.summary`,
  `activity.timeline`, `activity.status`, `activity.control` (confirm in normal and trusted mode).
* Chat window: 👁 panel = time per program (adds up to 100 % of active time), most used windows, today / 7 days.
