"""Drives Microsoft Paint (Windows 11 "Paint" app, English UI) for the drawing engine.

Everything here was measured on Paint 11.2605 (see docs/paint-engine.md); the facts it relies on:

* tools are ToggleButtons (TogglePattern) and the `image` element's Name says which one is active
  ("Using Rectangle tool on Canvas"), so a tool change is read back;
* a shape is drawn by a drag over its bounding box and stays *live* (selection handles) until committed;
  clicking anywhere outside the shape (a neutral point outside the canvas) commits it and creates nothing;
* outline colour = the selected colour slot (we keep slot 1 selected), fill colour = the other slot;
* the Size menu for shapes offers exactly 1/3/5/8 px (`Size1`/`Size3`/`Size5`/`Size8`); Brush/Pencil use a
  Size slider whose value is width - 1;
* Polygon: drag the first edge, click each further vertex, click the first vertex to close;
* the Edit colours dialog (hex field `HexTextBox`, OK = `PrimaryButton`) is modal: it must be opened with a real
  click, because UI Automation Invoke does not return while the dialog is open;
* the `image` rectangle can be one pixel above the real page, so the position is taken from a pixel scan of the
  blank canvas once, and the UI rectangle is used afterwards.

Runs on the desktop provider's STA thread. Never pointed at a window it did not open itself.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import win32con
import win32gui
from PIL import Image, ImageDraw, ImageGrab

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import calibration
from lga_worker.paint.calibration import CanvasTransform
from lga_worker.paint.model import RGB, PlannedObject
from lga_worker.windows import win32
from lga_worker.windows.provider import Rect
from lga_worker.windows.windows_use_provider import WindowsUseProvider

logger = logging.getLogger(__name__)

PAINT_PROCESS = "mspaint.exe"
LAUNCH_TIMEOUT_S = 30.0
UI_WAIT_S = 6.0
POLL_S = 0.1
SETTLE_S = 0.12
DRAG_S = 0.25
MIN_DRAG_PX = 2
SHAPE_TOOLS = {"rect": "Rectangle", "ellipse": "Oval", "triangle": "Triangle", "polygon": "Polygon", "line": "Line"}
SIZE_ITEM = {1: "Size1", 3: "Size3", 5: "Size5", 8: "Size8"}
SIZE_SLIDER_MAX = 90
CHECK_EVERY = 5
ACTIVE_NAME = {"Fill": "Fill with colour", "Colour picker": "Pick colour"}  # the `image` element names these differently


def _is_brush(active: str | None) -> bool:
    """The `image` name of a brush tool: 'Brush', 'Oil brush', 'Calligraphy brush 1', 'Marker', 'Natural pencil'..."""
    if not active:
        return False
    base = active.rstrip("0123456789 ").lower()
    return base in {b.lower() for b in BRUSH_TYPES}


BRUSH_TYPES = ("Brush", "Calligraphy brush", "Calligraphy pen", "Airbrush", "Oil brush", "Crayon", "Marker", "Natural pencil",
               "Watercolour brush")  # what Paint 11 lists in the Brushes gallery; discover() reads the real list
CLICK_GAP_S = 0.012
EDGE_BAND_PX = 3
STROKE_STEP_S = 0.012


def _rect(control: Any) -> Rect:
    r = control.BoundingRectangle
    return Rect(r.left, r.top, r.right, r.bottom)


class PaintDriver:
    def __init__(self, provider: WindowsUseProvider, should_stop: Callable[[], bool] = lambda: False) -> None:
        self._p = provider
        self._uia = provider.uia
        self._should_stop = should_stop
        self.hwnd: int | None = None
        self.transform: CanvasTransform | None = None
        self.scale: float = 1.0
        self._offset = (0, 0)  # real page position minus the UI rectangle, learned on the blank canvas
        self._controls: dict[tuple, Any] = {}
        self._tool: str | None = None
        self._outline: str | None = None  # "solid" | "none"
        self._fill: str | None = None
        self._size: int | None = None
        self._brush_size: int | None = None
        self._colours: dict[int, RGB] = {}
        self._palette: dict[RGB, Any] = {}
        self._ready_calls = 0
        self._slot_radios: dict[int, Any] = {}
        self._selected_slot: int | None = None
        self._neutral_cache: tuple[tuple[int, int, int, int], tuple[int, int]] | None = None
        self._brush_type: str | None = None
        self.check_every = CHECK_EVERY  # UI Automation checks (menu open, canvas moved/zoomed) every n-th object
        self.capabilities: dict[str, Any] | None = None
        self.gate: Any = None  # the ExecutionGate of the artwork being drawn (art/gate.py); nothing draws without it

    # ------------------------------------------------------------------------------------------ lifecycle

    def open_new(self) -> CanvasTransform:
        """Start a fresh Paint window (never reuses one that may hold the user's work) and calibrate."""
        before = {w.hwnd for w in win32.list_user_windows()}
        result = self._p.launch_app(PAINT_PROCESS, [], LAUNCH_TIMEOUT_S, None)
        window = result.window
        if window.hwnd in before:
            raise ToolError(ErrorCode.EXECUTION_FAILED, "Paint did not open a new window")
        self.hwnd = window.hwnd
        win32gui.ShowWindow(self.hwnd, win32con.SW_MAXIMIZE)
        self._p.bring_to_front(self.hwnd)
        self._wait_for(lambda: self._find(aid="image", refresh=True) is not None, "Paint's canvas to appear")
        time.sleep(0.5)
        return self.calibrate(blank=True)

    def attach(self, hwnd: int, offset: tuple[int, int]) -> CanvasTransform:
        """Re-use a window this driver family opened earlier (same worker process)."""
        if not win32.is_window(hwnd) or win32.process_name(win32.window_pid(hwnd)).lower() != PAINT_PROCESS:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"window {hwnd} is not an open Paint window")
        self.hwnd = hwnd
        self._offset = offset
        self._p.bring_to_front(hwnd)
        self._controls.clear()
        return self.calibrate(blank=False)

    @property
    def offset(self) -> tuple[int, int]:
        return self._offset

    # ------------------------------------------------------------------------------------------ UIA helpers

    def _root(self) -> Any:
        if self.hwnd is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "no Paint window is open")
        root = self._uia.ControlFromHandle(self.hwnd)
        if root is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, f"UI Automation has no element for window {self.hwnd}")
        return root

    def _find(self, *, aid: str | None = None, name: str | None = None, ctype: str | None = None,
              cls: str | None = None, refresh: bool = False, root: Any = None) -> Any | None:
        key = (aid, name, ctype, cls)
        if root is None and not refresh and key in self._controls:
            try:
                self._controls[key].BoundingRectangle  # still alive?
                return self._controls[key]
            except Exception:  # noqa: BLE001 - COM error: the element is gone, search again
                self._controls.pop(key, None)
        found: Any | None = None
        stack = [(root if root is not None else self._root(), 0)]
        while stack:
            control, depth = stack.pop(0)
            try:
                if ((aid is None or control.AutomationId == aid) and (name is None or control.Name == name)
                        and (ctype is None or control.ControlTypeName.removesuffix("Control") == ctype)
                        and (cls is None or control.ClassName == cls)):
                    found = control
                    break
                if depth < 16:
                    stack.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:  # noqa: BLE001 - a subtree vanished while walking
                continue
        if found is not None and root is None:
            self._controls[key] = found
        return found

    def _require(self, **kw: Any) -> Any:
        control = self._find(**kw)
        if control is None:
            raise ToolError(ErrorCode.ELEMENT_NOT_FOUND, f"Paint control {kw} not found (Paint version/language?)",
                            retryable=False)
        return control

    def _wait_for(self, predicate: Callable[[], bool], what: str, timeout_s: float = UI_WAIT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._should_stop():
                raise ToolError(ErrorCode.CANCELLED, "drawing cancelled")
            try:
                if predicate():
                    return
            except Exception:  # noqa: BLE001 - UIA is flaky while the UI changes; keep polling
                pass
            time.sleep(POLL_S)
        raise ToolError(ErrorCode.TIMEOUT, f"timed out waiting for {what}", retryable=True)

    def _popup_roots(self) -> list[Any]:
        roots = []
        for w in win32.list_user_windows():
            if w.process_name.lower() == PAINT_PROCESS and w.hwnd != self.hwnd and not w.title.endswith("Paint"):
                control = self._uia.ControlFromHandle(w.hwnd)
                if control is not None:
                    roots.append(control)
        return roots

    def _pattern(self, control: Any, name: str) -> Any:
        return control.GetPattern(getattr(self._uia.PatternId, name))

    # ------------------------------------------------------------------------------------------ calibration

    def _grab(self, region: Rect | None = None) -> tuple[Image.Image, tuple[int, int]]:
        left, top, _, _ = self._uia.GetVirtualScreenRect()
        image = ImageGrab.grab(all_screens=True)
        if region is None:
            return image, (left, top)
        box = (max(region.left - left, 0), max(region.top - top, 0),
               min(region.right - left, image.width), min(region.bottom - top, image.height))
        return image.crop(box), (box[0] + left, box[1] + top)

    def calibrate(self, *, blank: bool) -> CanvasTransform:
        canvas = calibration.parse_canvas_size(self._require(aid="CanvasSizeTextBlock", refresh=True).Name)
        shown = _rect(self._require(aid="image", refresh=True))
        t = CanvasTransform.from_rect(canvas, shown)
        if blank:
            shot, origin = self._grab()
            found = calibration.find_blank_canvas(shot, canvas, t.scale)
            if found is None:
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                "cannot find the blank page on screen to calibrate against (is Paint covered?)")
            real = (found[0] + origin[0], found[1] + origin[1])
            if abs(real[0] - shown.left) > 3 or abs(real[1] - shown.top) > 3:
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                f"canvas position disagrees: UI rectangle {shown.left},{shown.top} vs pixels {real}")
            self._offset = (real[0] - shown.left, real[1] - shown.top)
        t = CanvasTransform(canvas, shown.left + self._offset[0], shown.top + self._offset[1], t.scale)
        self._require_fully_visible(t)
        self.transform, self.scale = t, t.scale
        return t

    def _require_fully_visible(self, t: CanvasTransform) -> None:
        viewport = _rect(self._require(aid="scrollViewer", cls="ScrollViewer"))
        r = t.screen_rect
        if r.left < viewport.left or r.top < viewport.top or r.right > viewport.right or r.bottom > viewport.bottom:
            raise ToolError(ErrorCode.UNSUPPORTED,
                            "the canvas is larger than Paint's window at this zoom; use View > Fit to window first",
                            details={"canvas_on_screen": r.to_dict(), "viewport": viewport.to_dict()})

    def check_ready(self) -> None:
        """Before every object: Paint is in front, no dialog is open, the canvas has not moved or been zoomed."""
        if self._should_stop():
            raise ToolError(ErrorCode.CANCELLED, "drawing cancelled")
        if self.hwnd is None or not win32.is_window(self.hwnd):
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "the Paint window was closed")
        if not self._p.bring_to_front(self.hwnd):
            raise ToolError(ErrorCode.EXECUTION_FAILED, "Paint lost the foreground and could not get it back",
                            retryable=True, details={"recoverable": "focus"})
        # The window-level checks above run for every object; the UI Automation ones (a menu or dialog open, canvas
        # moved or zoomed) cost a few hundred ms, so a long drawing makes them every few objects.
        self._ready_calls += 1
        if self._ready_calls % max(1, self.check_every) == 1 or self.check_every <= 1:
            if self._popup_roots():
                raise ToolError(ErrorCode.EXECUTION_FAILED, "a menu or dialog is open in Paint", retryable=True,
                                details={"recoverable": "popup"})
            self.refresh_transform()

    def refresh_transform(self, settle: bool = False) -> CanvasTransform:
        """The canvas position now. It moves legitimately (the Brush/Fill tools open a side panel that re-centres
        the page), so the origin follows the UI; a different canvas size or zoom is an error."""
        old = self.transform
        previous: CanvasTransform | None = None
        for _ in range(8 if settle else 1):
            canvas = calibration.parse_canvas_size(self._require(aid="CanvasSizeTextBlock").Name)
            shown = _rect(self._require(aid="image"))
            raw = CanvasTransform.from_rect(canvas, shown)
            now = CanvasTransform(canvas, shown.left + self._offset[0], shown.top + self._offset[1], raw.scale)
            if not settle or (previous is not None and previous.same_place_as(now, 0.5)):
                break
            previous = now
            time.sleep(0.08)
        if old is not None and (old.canvas != now.canvas or abs(old.scale - now.scale) > 0.005):
            resized = old.canvas != now.canvas
            raise ToolError(ErrorCode.CONFLICT if resized else ErrorCode.EXECUTION_FAILED,
                            ("the Paint canvas was resized while drawing: the plan no longer fits it" if resized else
                             "the Paint zoom changed while drawing; recalibrating"),
                            retryable=not resized,
                            details={"before": [old.canvas.width, old.canvas.height, old.scale],
                                     "now": [now.canvas.width, now.canvas.height, now.scale],
                                     "recoverable": "canvas_resized" if resized else "zoom"})
        self.transform = now
        return now

    # ------------------------------------------------------------------------------------------ tools & style

    def _select_tool(self, tool: str) -> bool:
        """Make `tool` the active tool; True when it had to be changed (the page may then have moved)."""
        if self._tool == tool:
            return False
        active = self._active_tool()
        if active == ACTIVE_NAME.get(tool, tool) or (tool == "Brush" and _is_brush(active)):
            self._tool = tool
            return False
        if tool == "Brush":
            self._pattern(self._require(name="Brushes", ctype="RadioButton"), "InvokePattern").Invoke()
        else:
            button = self._require(name=tool, ctype="Button")
            toggle = self._pattern(button, "TogglePattern")
            if int(toggle.ToggleState) != 1:
                toggle.Toggle()
        self._wait_for(lambda: self._active_tool() == ACTIVE_NAME.get(tool, tool) or (tool == "Brush" and _is_brush(self._active_tool())),
                       f"the {tool} tool to become active", 3.0)
        self._tool = tool
        return True

    def _brush_gallery_button(self) -> Any:
        """The 'More Options' arrow next to the Brushes button (the one on its right)."""
        radio = _rect(self._require(name="Brushes", ctype="RadioButton", refresh=True))
        best: Any = None
        best_left = 10 ** 9
        stack = [(self._root(), 0)]
        while stack:
            control, depth = stack.pop(0)
            try:
                if control.AutomationId == "SecondaryButton" and control.Name == "More Options":
                    left = _rect(control).left
                    if left >= radio.left - 4 and left < best_left:
                        best, best_left = control, left
                elif depth < 16:
                    stack.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        if best is None:
            raise ToolError(ErrorCode.ELEMENT_NOT_FOUND, "Paint's brush gallery button was not found")
        return best

    def brush_types(self) -> list[str]:
        """The brush types this Paint offers (its gallery, read by opening it)."""
        button = self._brush_gallery_button()
        cx, cy = _rect(button).center
        self._p.click(cx, cy, "left")
        names: list[str] = []
        time.sleep(0.5)
        stack = [(self._root(), 0)]
        while stack:
            control, depth = stack.pop(0)
            try:
                if control.ClassName == "MenuFlyoutItem" and control.Name:
                    names.append(control.Name)
                elif depth < 18:
                    stack.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        self._uia.SendKeys("{Esc}")
        time.sleep(0.2)
        self._controls.clear()
        return names

    def _use_brush(self, brush: str | None) -> bool:
        """Make `brush` ('Oil brush', 'Pencil' the tool, ...) the active brush; True when something changed."""
        wanted = brush or "Brush"
        if wanted == "Pencil":
            return self._select_tool("Pencil")
        if self._tool == "Brush" and self._brush_type == wanted:
            return False
        changed = self._select_tool("Brush")
        if self._brush_type != wanted:
            if wanted != "Brush" or self._brush_type is not None:
                item: Any = None

                def present() -> bool:
                    nonlocal item
                    item = None
                    stack = [(self._root(), 0)]
                    while stack:
                        control, depth = stack.pop(0)
                        try:
                            if control.ClassName == "MenuFlyoutItem" and control.Name == wanted:
                                item = control
                                return True
                            if depth < 22:
                                stack.extend((child, depth + 1) for child in control.GetChildren())
                        except Exception:  # noqa: BLE001
                            continue
                    return False

                for attempt in (1, 2, 3):  # the first press after a ribbon action may only re-activate the window
                    button = self._brush_gallery_button()
                    cx, cy = _rect(button).center
                    self._p.click(cx, cy, "left")
                    try:
                        self._wait_for(present, f"the brush {wanted!r} in the gallery", 2.5)
                        break
                    except ToolError:
                        if attempt == 3:
                            raise
                        self._uia.SendKeys("{Esc}")
                        time.sleep(0.3)
                self._pattern(item, "InvokePattern").Invoke()
                time.sleep(SETTLE_S)
                self._controls.clear()
                self._tool = "Brush"
                changed = True
            self._brush_type = wanted
            self._brush_size = None  # each brush keeps its own size: set it again
            self._full_opacity()
        return changed

    def _full_opacity(self) -> None:
        """A brush must paint its colour, not a blend of it with the page: opacity to the slider's maximum."""
        try:
            slider = self._find(name="Opacity", ctype="Slider", refresh=True)
            if slider is None:
                return
            rv = self._pattern(slider, "RangeValuePattern")
            top = float(rv.Maximum)
            if float(rv.Value) < top:
                rv.SetValue(top)
                time.sleep(SETTLE_S)
        except Exception as exc:  # noqa: BLE001 - colour accuracy is verified afterwards anyway
            logger.debug("opacity not set: %s", exc)

    def _active_tool(self) -> str | None:
        text = self._require(aid="image", refresh=True).Name  # "Using Rectangle tool on Canvas"
        if text.startswith("Using ") and " tool" in text:
            return text[len("Using "):text.index(" tool")]
        return None

    def _dropdown(self, name: str, item: str) -> None:
        button = self._require(name=name, cls="Microsoft.UI.Xaml.Controls.DropDownButton", refresh=True)
        expand = self._pattern(button, "ExpandCollapsePattern")
        if int(expand.ExpandCollapseState) != 0:  # a leftover flyout: close it first, then open it again
            expand.Collapse()
            time.sleep(0.2)
        expand.Expand()
        entry: Any = None

        def present() -> bool:
            nonlocal entry
            for root in self._popup_roots():
                entry = self._find(root=root, name=item, ctype="MenuItem") or self._find(root=root, aid=item, ctype="MenuItem")
                if entry is not None:
                    return True
            return False

        self._wait_for(present, f"the {name} menu item {item!r}", 3.0)
        self._pattern(entry, "InvokePattern").Invoke()
        self._wait_for(lambda: not self._popup_roots(), f"the {name} menu to close", 3.0)
        time.sleep(SETTLE_S)

    def _set_outline(self, mode: str) -> None:
        if self._outline != mode:
            self._dropdown("Shape outline", "Solid outline" if mode == "solid" else "No outline")
            self._outline = mode

    def _set_fill(self, mode: str) -> None:
        if self._fill != mode:
            self._dropdown("Shape fill", "Solid fill" if mode == "solid" else "No fill")
            self._fill = mode

    def _set_shape_size(self, width: int) -> None:
        if self._size != width:
            self._dropdown("Size", SIZE_ITEM[width])
            self._size = width

    def _set_brush_size(self, width: int) -> None:
        if self._brush_size == width:
            return
        slider = self._require(name="Size", ctype="Slider", refresh=True)
        rv = self._pattern(slider, "RangeValuePattern")
        rv.SetValue(float(min(SIZE_SLIDER_MAX, max(0, width - 1))))
        time.sleep(SETTLE_S)
        self._brush_size = width

    # ------------------------------------------------------------------------------------------ colours

    def _slot_radio(self, slot: int) -> Any:
        cached = self._slot_radios.get(slot)
        if cached is not None:
            try:
                cached.BoundingRectangle  # still alive?
                return cached
            except Exception:  # noqa: BLE001 - COM error: find it again
                self._slot_radios.pop(slot, None)
        radios = []
        stack = [self._require(name="Colours", ctype="Group")]
        while stack:
            c = stack.pop(0)
            try:
                if c.ControlTypeName.removesuffix("Control") == "RadioButton" and c.ClassName == "RadioButton":
                    radios.append(c)
                else:
                    stack.extend(c.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        radios.sort(key=lambda c: _rect(c).top)
        if len(radios) < 2:
            raise ToolError(ErrorCode.ELEMENT_NOT_FOUND, "Paint's colour 1 / colour 2 selectors were not found")
        self._slot_radios = {1: radios[0], 2: radios[1]}
        return radios[slot - 1]

    def _read_slot(self, slot: int) -> RGB:
        """The colour of a slot, read from the pixels of its swatch (exact, unlike the name 'Turquoise')."""
        r = _rect(self._slot_radio(slot))
        cx, cy = r.center
        shot, origin = self._grab(Rect(cx - 2, cy - 2, cx + 3, cy + 3))
        return shot.convert("RGB").getpixel((2, 2))

    def _scan_palette(self) -> None:
        group = self._require(name="Colours", ctype="Group")
        items: list[Any] = []
        stack = [group]
        while stack:
            c = stack.pop(0)
            try:
                if c.ControlTypeName.removesuffix("Control") == "ListItem":
                    items.append(c)
                else:
                    stack.extend(c.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        shot, origin = self._grab()
        self._palette = {}
        for item in items:
            if item.Name == "Empty":
                continue
            cx, cy = _rect(item).center
            x, y = cx - origin[0], cy - origin[1]
            if 0 <= x < shot.width and 0 <= y < shot.height:
                self._palette.setdefault(shot.convert("RGB").getpixel((x, y)), item)

    def _edit_colour_hex(self, rgb: RGB) -> None:
        button = self._require(name="Edit colours", ctype="Button", refresh=True)
        cx, cy = _rect(button).center
        self._p.click(cx, cy, "left")  # a real click: Invoke would not return while the modal dialog is open
        hex_box: Any = None

        def dialog_open() -> bool:
            nonlocal hex_box
            hex_box = self._find(aid="HexTextBox", refresh=True)
            return hex_box is not None

        try:
            self._wait_for(dialog_open, "the Edit colours dialog", 4.0)
        except ToolError:  # the click was swallowed (first press after a ribbon action): once more
            self._uia.SendKeys("{Esc}")
            time.sleep(0.4)
            self._p.click(cx, cy, "left")
            self._wait_for(dialog_open, "the Edit colours dialog", 5.0)
        self._pattern(hex_box, "ValuePattern").SetValue("#%02X%02X%02X" % rgb)
        time.sleep(0.2)
        self._pattern(self._require(aid="PrimaryButton", name="OK", refresh=True), "InvokePattern").Invoke()
        self._wait_for(lambda: self._find(aid="HexTextBox", refresh=True) is None, "the Edit colours dialog to close", 4.0)
        self._controls.clear()
        self._slot_radios = {}
        self._palette = {}
        time.sleep(0.3)
        # The first press after the dialog only re-activates the canvas (a drag right after it draws nothing).
        self.refresh_transform(settle=True)
        self.commit()
        time.sleep(0.2)

    def _swatch_for(self, rgb: RGB) -> Any | None:
        if rgb not in self._palette:
            self._scan_palette()
        return self._palette.get(rgb)

    def _set_slot(self, slot: int, rgb: RGB) -> None:
        """Make colour `slot` (1 or 2) `rgb`. Slot 1 stays the selected one: a left click on a swatch sets it, a
        RIGHT click sets colour 2 without selecting it (a radio-button selection costs half a second each way).
        A colour that is not in the ribbon is added through the Edit colours dialog (which edits the selected
        slot), after which its swatch is there for every later use."""
        if self._colours.get(slot) == rgb:
            return
        self._select_slot_one()
        item = self._swatch_for(rgb)
        if item is None:
            self._edit_colour_hex(rgb)  # edits colour 1 (selected) and adds the swatch
            self._colours[1] = rgb
            item = self._swatch_for(rgb)
        if slot == 1:
            if self._colours.get(1) != rgb:
                self._pattern(item, "InvokePattern").Invoke() if item is not None else self._edit_colour_hex(rgb)
        elif item is not None:
            r = _rect(item)
            cx, cy = r.center
            self._uia.RightClick(cx, cy, waitTime=0.02)
            # A real click on the ribbon takes the focus away from the canvas, and the next press there only
            # re-activates it (that drag draws nothing): hand the focus back before anything is drawn.
            if self.transform is not None:
                nx, ny = self._neutral_point()
                self._fast_click(nx, ny)
                time.sleep(0.05)
        else:  # not in the ribbon even after adding it (custom row full): edit colour 2 directly
            self._pattern(self._slot_radio(2), "SelectionItemPattern").Select()
            self._selected_slot = 2
            self._edit_colour_hex(rgb)
            self._select_slot_one()
        time.sleep(SETTLE_S)
        got = self._read_slot(slot)
        if max(abs(a - b) for a, b in zip(got, rgb, strict=True)) > 2:
            raise ToolError(ErrorCode.VERIFICATION_FAILED, f"colour {slot} is {got}, wanted {rgb}", retryable=True)
        self._colours[slot] = rgb

    def _select_slot_one(self) -> None:
        if self._selected_slot == 1:
            return
        self._pattern(self._slot_radio(1), "SelectionItemPattern").Select()
        self._selected_slot = 1

    # ------------------------------------------------------------------------------------------ input

    def _screen(self, x: float, y: float) -> tuple[int, int]:
        assert self.transform is not None
        return self.transform.to_screen(x, y)

    def _neutral_point(self) -> tuple[int, int]:
        assert self.transform is not None
        r = self.transform.screen_rect
        key = (r.left, r.top, r.right, r.bottom)
        if self._neutral_cache is None or self._neutral_cache[0] != key:
            self._neutral_cache = (key, self._find_neutral_point())
        return self._neutral_cache[1]

    def _find_neutral_point(self) -> tuple[int, int]:
        """A point of the viewport well away from the canvas, its resize handles and the tool panel, where a click
        commits a live shape and draws nothing. (A click 30 px beside the canvas edge lands on a resize handle and
        widened the canvas by 30 px each time.)"""
        assert self.transform is not None
        viewport = _rect(self._require(aid="scrollViewer", cls="ScrollViewer"))
        r = self.transform.screen_rect
        keep_away = 60
        zone = Rect(r.left - keep_away, r.top - keep_away, r.right + keep_away, r.bottom + keep_away)
        inset_x, inset_y = 90, 40
        for x, y in ((viewport.left + inset_x, viewport.top + inset_y),
                     (viewport.right - inset_x, viewport.top + inset_y),
                     (viewport.left + inset_x, viewport.bottom - inset_y),
                     (viewport.right - inset_x, viewport.bottom - inset_y)):
            if not zone.contains(x, y):
                return x, y
        raise ToolError(ErrorCode.UNSUPPORTED, "no free spot away from the canvas to commit shapes with; "
                        "zoom out (View > Fit to window) first")

    def commit(self) -> None:
        """Commit the live shape by clicking a neutral point. Done twice, apart: a click that arrives while Paint is
        still busy with the drag is dropped, and the next colour change would then repaint the old shape (the status
        bar keeps showing the last object's size, so a commit cannot be read back from the UI)."""
        time.sleep(0.12)
        x, y = self._neutral_point()
        for _ in range(2):
            self._fast_click(x, y)
            time.sleep(0.15)

    def _fast_click(self, x: int, y: int) -> None:
        """A left click without the provider's settle pauses (a polygon takes one click per vertex)."""
        self._uia.PressMouse(x, y, CLICK_GAP_S)
        self._uia.ReleaseMouse(CLICK_GAP_S)

    def _drag(self, a: tuple[int, int], b: tuple[int, int]) -> None:
        if max(abs(a[0] - b[0]), abs(a[1] - b[1])) < MIN_DRAG_PX:
            b = (a[0] + MIN_DRAG_PX, a[1] + MIN_DRAG_PX)
        uia = self._uia
        uia.PressMouse(a[0], a[1], 0.02)
        try:
            steps = 8
            for i in range(1, steps + 1):
                uia.SetCursorPos(a[0] + (b[0] - a[0]) * i // steps, a[1] + (b[1] - a[1]) * i // steps)
                time.sleep(0.008)
        finally:
            uia.ReleaseMouse(0.03)

    def _stroke(self, points: list[tuple[int, int]]) -> None:
        """Press at the first point, move through the others as real mouse-move events, release (Brush path).
        SetCursorPos alone is coalesced into the start and end points; a curve needs every sample delivered."""
        uia = self._uia
        width, height = uia.GetScreenSize()
        flags = uia.MouseEventFlag.Move | uia.MouseEventFlag.Absolute | uia.MouseEventFlag.MoveNoCoalesce
        uia.SetCursorPos(points[0][0], points[0][1])  # arrive first: a press must never start where the cursor was left
        time.sleep(0.03)
        uia.PressMouse(points[0][0], points[0][1], 0.05)
        try:
            for x, y in points[1:]:
                uia.mouse_event(flags, x * 65535 // width, y * 65535 // height, 0, 0)
                time.sleep(STROKE_STEP_S)
        finally:
            time.sleep(0.03)
            uia.ReleaseMouse(0.05)

    # ------------------------------------------------------------------------------------------ drawing

    def draw(self, obj: PlannedObject) -> None:
        if self.gate is None:
            raise ToolError(ErrorCode.POLICY_DENIED, "drawing refused: no artwork specification authorised this drawing "
                            "(every drawing goes through lga_worker.art.gate.run_gated)")
        self.gate.assert_open()
        if self.transform is None:
            raise ToolError(ErrorCode.INTERNAL, "draw() before calibrate()")
        if obj.kind == "fill":
            self._draw_fill(obj)
        elif obj.kind == "curve":
            self._draw_curve(obj)
            self.commit()
        elif obj.kind == "path":
            self._draw_path(obj)  # a brush stroke is final when the button is released: nothing to commit
        elif obj.kind == "polyline":
            # one Line per segment, each committed: the next segment starts on the previous one's end handle,
            # and a drag from there would stretch it instead of starting a new line
            for a, b in zip(obj.points, obj.points[1:], strict=False):
                self._draw_shape("line", [a, b], obj)
                self.commit()
        else:
            self._draw_shape(obj.kind, list(obj.points), obj)
            self.commit()

    def _style_shape(self, obj: PlannedObject) -> None:
        # colour 1 is the selected slot = outline; colour 2 = fill
        if obj.stroke is not None:
            self._set_slot(1, obj.stroke)
        if obj.fill is not None:
            self._set_slot(2, obj.fill)
        self._select_slot_one()
        if obj.kind not in ("line", "polyline"):
            self._set_outline("solid" if obj.stroke is not None else "none")
            self._set_fill("solid" if obj.fill is not None else "none")
        if obj.stroke is not None:
            self._set_shape_size(obj.stroke_px)

    def _draw_shape(self, kind: str, pts: list[tuple[float, float]], obj: PlannedObject) -> None:
        changed = self._select_tool(SHAPE_TOOLS[kind])
        self._style_shape(obj)
        if changed or self.transform is None:
            self.refresh_transform(settle=True)  # a tool change can open or close a side panel and move the page
        screen = [self._screen(x, y) for x, y in pts]
        if kind == "polygon":
            screen = self._polygon_start_at_longest_edge(screen)
            self._drag(screen[0], screen[1])
            for x, y in screen[2:]:
                self._fast_click(x, y)
                time.sleep(CLICK_GAP_S)
            self._fast_click(screen[0][0], screen[0][1])  # closes the polygon
        else:
            self._drag(screen[0], screen[1])
        time.sleep(0.03)

    @staticmethod
    def _polygon_start_at_longest_edge(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Rotate (and if it helps, reverse) the vertex ring so the first edge, the one that is dragged, is the
        longest. Measured on Paint 11: a first drag shorter than about 12 px is taken for a click and the whole
        polygon is silently not drawn, whatever follows."""
        best: list[tuple[int, int]] | None = None
        best_len = -1.0
        for ring in (points, points[::-1]):
            n = len(ring)
            for i in range(n):
                length = math.dist(ring[i], ring[(i + 1) % n])
                if length > best_len:
                    best_len, best = length, ring[i:] + ring[:i]
        return best or points

    def _draw_path(self, obj: PlannedObject) -> None:
        """An open polyline as ONE brush stroke through its vertices (a sketch line)."""
        if obj.stroke is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"path {obj.id!r} needs a stroke colour")
        changed = self._use_brush(obj.brush)
        self._set_slot(1, obj.stroke)
        self._select_slot_one()
        self._set_brush_size(obj.stroke_px)
        if changed or self.transform is None:
            self.refresh_transform(settle=True)
        pts = list(obj.outline)
        if obj.closed:
            pts.append(pts[0])
        screen = [self._screen(x, y) for x, y in pts]
        dedup = [screen[0]] + [p for a, p in zip(screen, screen[1:], strict=False) if p != a]
        if len(dedup) < 2:
            return
        self._stroke(dedup)

    def _draw_fill(self, obj: PlannedObject) -> None:
        """One click of the Fill tool inside a region (the engine has already shown on the reference raster that the
        region is closed). The fill colour is colour 1, the one a left click applies."""
        if obj.fill is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"fill {obj.id!r} needs a colour")
        changed = self._select_tool("Fill")
        self._set_slot(1, obj.fill)
        self._select_slot_one()
        if changed:
            self.refresh_transform(settle=True)
        x, y = self._screen(*obj.points[0])
        self._fast_click(x, y)
        time.sleep(0.25)

    def undo(self) -> bool:
        """Ctrl+Z in the Paint window (it has the foreground: check_ready ran before the operation)."""
        if self.hwnd is None or not self._p.bring_to_front(self.hwnd):
            return False
        self._uia.SendKeys("{Ctrl}z")
        time.sleep(0.4)
        return True

    def _draw_curve(self, obj: PlannedObject) -> None:
        if obj.stroke is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"curve {obj.id!r} needs a stroke colour")
        changed = self._use_brush(obj.brush)
        self._set_slot(1, obj.stroke)
        self._select_slot_one()
        self._set_brush_size(obj.stroke_px)
        if changed:
            self.refresh_transform(settle=True)
        points = [self._screen(x, y) for x, y in obj.outline]
        dedup = [points[0]] + [p for a, p in zip(points, points[1:], strict=False) if p != a]
        if len(dedup) < 2:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"curve {obj.id!r} is too small to draw")
        self._stroke(dedup)

    # ------------------------------------------------------------------------------------------ capture

    def capture(self) -> Image.Image:
        """The canvas at canvas resolution, from a screenshot of exactly its on-screen rectangle."""
        if self.transform is None:
            raise ToolError(ErrorCode.INTERNAL, "capture() before calibrate()")
        time.sleep(0.15)
        self.refresh_transform(settle=True)
        region = self.transform.screen_rect
        shot, _ = self._grab(region)
        shot = shot.convert("RGB")
        canvas = self.transform.canvas
        if shot.size != (canvas.width, canvas.height):
            shot = shot.resize((canvas.width, canvas.height), Image.Resampling.BOX)
        # Paint draws the canvas's resize handles over the page edge; nothing is ever planned this close to it.
        ImageDraw.Draw(shot).rectangle((0, 0, shot.width - 1, shot.height - 1), outline=(255, 255, 255), width=EDGE_BAND_PX)
        return shot

    def probe(self) -> dict[str, Any]:
        """What the readiness check needs, read now: window, canvas size and zoom, tools, brushes, undo."""
        if self.capabilities is None:
            self.discover()
        caps = self.capabilities or {}
        t = self.refresh_transform(settle=True)
        r = t.screen_rect
        return {"window_ok": self.hwnd is not None and win32.is_window(self.hwnd), "canvas": (t.canvas.width, t.canvas.height),
                "scale": t.scale, "screen_rect": r.to_dict() if hasattr(r, "to_dict") else None,
                "tools": [n for n in caps.get("tools", [])] , "brushes": caps.get("brushes", []), "version": caps.get("version"),
                "undo": True, "foreground": win32.front_is(self.hwnd) if self.hwnd else False,
                "capabilities": {"tools": caps.get("tools"), "brushes": caps.get("brushes"), "version": caps.get("version")}}

    def recover(self, error: ToolError) -> bool:
        """Put the window back after a recoverable error (the engine retries the object afterwards)."""
        kind = (error.details or {}).get("recoverable")
        self._controls.clear()
        self._slot_radios = {}
        self._neutral_cache = None
        if self.hwnd is None or not win32.is_window(self.hwnd):
            return False
        if kind in ("focus", "popup"):
            if kind == "popup":
                self._uia.SendKeys("{Esc}")
                time.sleep(0.3)
            time.sleep(0.3)
            return bool(self._p.bring_to_front(self.hwnd))
        if kind == "zoom":
            self._p.bring_to_front(self.hwnd)
            fit = self._find(name="Fit to window", ctype="Button", refresh=True)
            if fit is not None:
                self._pattern(fit, "InvokePattern").Invoke()
                time.sleep(0.6)
            self.calibrate(blank=False)
            self._tool = None  # the page may have moved under the tool panel: reselect
            return True
        return False

    def discover(self) -> dict[str, Any]:
        """What this running Paint offers: version, the ribbon tools, shapes, the brush gallery, opacity."""
        info: dict[str, Any] = {"hwnd": self.hwnd}
        try:
            import psutil
            import win32api

            path = psutil.Process(win32.window_pid(self.hwnd)).exe()
            if path:
                fixed = win32api.GetFileVersionInfo(path, "\\")
                info["version"] = "%d.%d.%d.%d" % (fixed["FileVersionMS"] >> 16, fixed["FileVersionMS"] & 0xFFFF,
                                                   fixed["FileVersionLS"] >> 16, fixed["FileVersionLS"] & 0xFFFF)
        except Exception as exc:  # noqa: BLE001 - the version is informative only
            info["version_error"] = repr(exc)[:80]
        tools: dict[str, str] = {}
        sliders: list[str] = []
        stack = [(self._root(), 0)]
        while stack:
            control, depth = stack.pop(0)
            try:
                kind = control.ControlTypeName.removesuffix("Control")
                name = control.Name or ""
                if kind == "Button" and control.ClassName == "ToggleButton" and name:
                    tools[name] = "toggle"
                elif kind == "RadioButton" and name in ("Brushes", "Select"):
                    tools[name] = "split"
                elif kind == "Slider" and name:
                    sliders.append(name)
                if depth < 16:
                    stack.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        info["tools"] = sorted(tools)
        info["sliders"] = sorted(set(sliders))
        try:
            info["brushes"] = self.brush_types()
        except ToolError as exc:
            info["brushes"] = []
            info["brushes_error"] = exc.message
        self.capabilities = info
        return info

    def initial_state(self) -> None:
        """Read what the window starts with (colour slots) so unchanged colours are not set again."""
        self._colours = {1: self._read_slot(1), 2: self._read_slot(2)}
        self._tool = self._active_tool()
