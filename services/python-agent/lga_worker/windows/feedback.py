"""Feedback the planner gets about a screen action, computed from plain data (no Windows calls, so unit-testable).

Two things a model cannot see for itself after a click or a key press:
  * whether anything on the screen changed at all (a click that hit nothing looks exactly like one that worked);
  * what actually lay under a coordinate it took from a screenshot (vision grounding can be off by a few pixels).
Both are advice, never a verdict: a wait legitimately changes nothing, and drawing on a canvas clicks unnamed panes.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Container-ish control types that carry no meaning of their own: a click that lands on one of them, with no name,
# hit empty space rather than a control.
GENERIC_CONTROL_TYPES = frozenset({"pane", "window", "custom", "group", "document", "image", "text", "unknown"})


def _norm(control_type: Any) -> str:
    return str(control_type or "").removesuffix("Control").strip().lower()


def observation_signature(data: dict[str, Any]) -> str:
    """A short digest of what a person would see: the windows, the focused control, the text and the controls
    (name, value, state). Positions are left out so a window that merely moved does not count as a change."""
    def window(w: dict[str, Any] | None) -> list[Any]:
        return [w.get("hwnd"), w.get("title"), w.get("state")] if w else []

    focused = data.get("focused") or {}
    parts: list[Any] = [
        window(data.get("working")),
        window(data.get("foreground")),
        [focused.get("control_type"), focused.get("name"), focused.get("text"), focused.get("value")],
        list(data.get("texts") or []),
        [[e.get("control_type"), e.get("name"), e.get("value"), e.get("state")] for e in data.get("elements") or []],
        data.get("surprise") or "",
    ]
    return hashlib.sha1(repr(parts).encode("utf-8", "replace")).hexdigest()[:16]


def no_change_note(previous: str | None, current: str) -> str | None:
    """The line to show when two consecutive observations are identical, or None."""
    if previous is None or previous != current:
        return None
    return ("NOTE: nothing visible changed since your previous observation. The last action may have had no effect "
            "(or it needs time): check before assuming it worked, and do not repeat it unchanged.")


def pointer_target(element: dict[str, Any] | None) -> tuple[str, bool]:
    """Describe the control under a screen point. Returns (text, weak): weak means no named control was there."""
    if not element:
        return "no control (nothing UI Automation can see)", True
    kind = _norm(element.get("control_type"))
    name = str(element.get("name") or "").strip()
    ident = str(element.get("automation_id") or "").strip()
    label = f"{kind or 'control'}" + (f" '{name[:60]}'" if name else "") + (f" #{ident[:30]}" if ident else "")
    weak = not name and (not kind or kind in GENERIC_CONTROL_TYPES)
    return label, weak


def pointer_line(x: int, y: int, element: dict[str, Any] | None) -> str:
    """The sentence added to a coordinate click's observation."""
    text, weak = pointer_target(element)
    line = f"Under the pointer at ({x},{y}): {text}."
    if weak:
        line += (" No named control is there: if you took these coordinates from a screenshot or vision.locate to press a "
                 "button or menu item, locate it again (desktop.find_element first); clicking empty canvas or text is fine.")
    return line
