"""`WindowsControlProvider`: the contract every Windows execution backend implements.

The tool layer (`lga_worker.tools.desktop`) talks only to this interface. `WindowsUseProvider` is the
first implementation. All methods are synchronous and must be called on the provider's own STA
thread (`provider.executor`); the tool layer does that.

Coordinates are physical screen pixels in the virtual-desktop coordinate space (monitors left of or
above the primary monitor have negative coordinates).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from lga_worker.sta import StaExecutor

MouseButton = Literal["left", "right", "middle"]
ScrollDirection = Literal["up", "down", "left", "right"]
WindowState = Literal["normal", "minimized", "maximized"]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def to_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom,
                "width": self.width, "height": self.height}


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str
    rect: Rect
    state: WindowState
    is_foreground: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rect"] = self.rect.to_dict()
        return data


@dataclass(frozen=True)
class ElementLocator:
    """Enough information to find an element again after its live reference went stale."""

    window_hwnd: int
    name: str
    control_type: str
    automation_id: str
    class_name: str


@dataclass
class UIElement:
    element_id: str
    name: str
    control_type: str
    automation_id: str
    class_name: str
    rect: Rect
    window_hwnd: int
    window_title: str
    is_enabled: bool
    is_offscreen: bool
    has_focus: bool
    value: str | None = None
    patterns: list[str] = field(default_factory=list)

    @property
    def locator(self) -> ElementLocator:
        return ElementLocator(self.window_hwnd, self.name, self.control_type, self.automation_id, self.class_name)

    def to_dict(self) -> dict[str, Any]:
        center_x, center_y = self.rect.center
        return {
            "element_id": self.element_id,
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "rect": self.rect.to_dict(),
            "center": {"x": center_x, "y": center_y},
            "window_hwnd": self.window_hwnd,
            "window_title": self.window_title,
            "is_enabled": self.is_enabled,
            "is_offscreen": self.is_offscreen,
            "has_focus": self.has_focus,
            "value": self.value,
            "patterns": self.patterns,
        }


@dataclass(frozen=True)
class ElementQuery:
    """Criteria for `find_element`. All given criteria must match."""

    window_hwnd: int | None = None
    name: str | None = None
    name_contains: str | None = None
    control_type: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    include_offscreen: bool = False
    max_results: int = 20


@dataclass(frozen=True)
class UISnapshot:
    snapshot_id: int
    window: WindowInfo | None
    elements: list[UIElement]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "window": self.window.to_dict() if self.window else None,
            "elements": [e.to_dict() for e in self.elements],
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LaunchResult:
    pid: int | None
    window: WindowInfo
    # "new_window": a window that did not exist before appeared;
    # "existing_window": the app was already running and its window came to the foreground.
    match_kind: Literal["new_window", "existing_window"]
    resolved_target: str


@dataclass(frozen=True)
class CloseResult:
    closed: bool
    # When the window refused to close (e.g. "Save changes?" dialog), the blocking dialog.
    blocking_dialog: WindowInfo | None


@dataclass(frozen=True)
class ScreenshotResult:
    path: Path
    width: int
    height: int
    # Screen coordinate of the image's top-left pixel (virtual desktop origin or region origin).
    origin_x: int
    origin_y: int


@dataclass(frozen=True)
class ElementContent:
    element: UIElement
    value: str | None
    text: str | None
    toggle_state: str | None
    is_read_only: bool | None


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int


@dataclass(frozen=True)
class FileContent:
    path: Path
    size: int
    encoding: str
    content: str
    truncated: bool
    sha256: str


@dataclass(frozen=True)
class WriteResult:
    path: Path
    size: int
    sha256: str
    created: bool


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    is_dir: bool
    size: int | None
    modified_unix: float


class WindowsControlProvider(ABC):
    """Windows execution backend. See module docstring for threading and coordinate rules."""

    executor: StaExecutor

    # --- applications and windows -------------------------------------------------------------
    @abstractmethod
    def launch_app(
        self, target: str, arguments: list[str], timeout_s: float, expect_title: str | None = None
    ) -> LaunchResult: ...

    @abstractmethod
    def close_app(self, hwnd: int, force: bool, timeout_s: float) -> CloseResult: ...

    @abstractmethod
    def focus_window(self, hwnd: int, timeout_s: float) -> WindowInfo: ...

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]: ...

    # --- mouse ---------------------------------------------------------------------------------
    @abstractmethod
    def click(self, x: int, y: int, button: MouseButton) -> None: ...

    @abstractmethod
    def double_click(self, x: int, y: int) -> None: ...

    @abstractmethod
    def right_click(self, x: int, y: int) -> None: ...

    @abstractmethod
    def move_mouse(self, x: int, y: int) -> tuple[int, int]: ...

    @abstractmethod
    def drag(self, start: tuple[int, int], end: tuple[int, int], duration_s: float) -> None: ...

    @abstractmethod
    def scroll(self, x: int | None, y: int | None, direction: ScrollDirection, amount: int) -> None: ...

    # --- keyboard ------------------------------------------------------------------------------
    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def press_key(self, key: str, repeat: int) -> None: ...

    @abstractmethod
    def hotkey(self, keys: list[str]) -> None: ...

    # --- perception ----------------------------------------------------------------------------
    @abstractmethod
    def take_screenshot(self, path: Path, region: Rect | None, window_hwnd: int | None) -> ScreenshotResult: ...

    @abstractmethod
    def get_ui_tree(self, window_hwnd: int | None, max_elements: int) -> UISnapshot: ...

    @abstractmethod
    def find_element(self, query: ElementQuery) -> list[UIElement]: ...

    @abstractmethod
    def read_element(self, element_id: str) -> ElementContent: ...

    @abstractmethod
    def write_element(self, element_id: str, value: str) -> ElementContent: ...

    @abstractmethod
    def focus_element(self, element_id: str) -> UIElement: ...

    @abstractmethod
    def element_center(self, element_id: str) -> tuple[int, int]: ...

    @abstractmethod
    def cursor_position(self) -> tuple[int, int]: ...

    @abstractmethod
    def foreground_window(self) -> WindowInfo | None: ...

    # --- shell and filesystem ------------------------------------------------------------------
    # These block for a long time and do not touch UI Automation; callers run them off the STA thread.
    @abstractmethod
    def run_powershell(
        self, script: str, cwd: Path | None, timeout_s: float, cancel_event: threading.Event | None = None
    ) -> ShellResult: ...

    @abstractmethod
    def read_file(self, path: Path, max_bytes: int, encoding: str | None) -> FileContent: ...

    @abstractmethod
    def write_file(self, path: Path, content: str, encoding: str, overwrite: bool, create_dirs: bool) -> WriteResult: ...

    @abstractmethod
    def list_directory(self, path: Path, max_entries: int) -> tuple[list[DirectoryEntry], bool]:
        """Return (entries, truncated)."""
