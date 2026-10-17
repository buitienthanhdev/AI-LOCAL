"""Saving a Paint picture and proving it was saved (docs/specs/SPEC_PAINT.txt, section 14).

Opening the Save dialog is not saving. A save counts only when the file exists, opens as an image of the expected
format and size, and (for lossless formats) shows the canvas that was on screen. Overwriting an existing file needs
`overwrite` and is preceded by a backup copy next to it.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from lga_worker.errors import ErrorCode, ToolError

FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".bmp": "BMP", ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF"}
LOSSLESS = {"PNG", "BMP", "TIFF"}
MEAN_DIFF_LOSSLESS = 1.0  # mean absolute channel difference allowed between the canvas and the saved file
MEAN_DIFF_LOSSY = 12.0


def format_of(path: Path) -> str:
    fmt = FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        f"{path.suffix or '(no extension)'} is not a picture format Paint saves: use one of {', '.join(sorted(FORMATS))}")
    return fmt


def backup_existing(path: Path) -> Path | None:
    """Copy an existing file to `<name>.bak-<time><ext>` before it is replaced; None when there is nothing to protect."""
    if not path.is_file():
        return None
    target = path.with_name(f"{path.stem}.bak-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    shutil.copy2(path, target)
    return target


@dataclass
class SavedCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SavedReport:
    path: str
    checks: list[SavedCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks]}

    def failed(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]


def verify_saved_file(path: Path, canvas_size: tuple[int, int], canvas_image: Image.Image | None = None, *,
                      started_at: float | None = None) -> SavedReport:
    """Read the file back and compare it with what the canvas showed. Never trusts the save command."""
    report = SavedReport(str(path))
    add = report.checks.append
    exists = path.is_file()
    add(SavedCheck("file_exists", exists, str(path) if exists else "no such file"))
    if not exists:
        return report
    size = path.stat().st_size
    add(SavedCheck("not_empty", size > 0, f"{size} bytes"))
    if started_at is not None:
        fresh = path.stat().st_mtime >= started_at - 2.0
        add(SavedCheck("written_by_this_save", fresh, "modified before this save started: an old file was left in place" if not fresh else ""))
    try:
        with Image.open(path) as probe:
            probe.load()
            image = probe.convert("RGB")
            actual_format = probe.format or ""
    except (OSError, ValueError) as exc:
        add(SavedCheck("opens_as_image", False, f"{type(exc).__name__}: {exc}"))
        return report
    add(SavedCheck("opens_as_image", True, actual_format))
    expected = FORMATS.get(path.suffix.lower())
    add(SavedCheck("format_matches_extension", expected == actual_format or (expected == "JPEG" and actual_format == "MPO"),
                   f"extension says {expected}, the file is {actual_format}"))
    add(SavedCheck("pixel_size", image.size == tuple(canvas_size), f"file {image.size[0]}x{image.size[1]}, canvas {canvas_size[0]}x{canvas_size[1]}"))
    if canvas_image is not None and image.size == canvas_image.size:
        diff = ImageChops.difference(image, canvas_image.convert("RGB"))
        hist = diff.histogram()
        total = sum(i * c for i, c in enumerate(hist[:256])) + sum(i * c for i, c in enumerate(hist[256:512])) \
            + sum(i * c for i, c in enumerate(hist[512:768]))
        mean = total / float(image.size[0] * image.size[1] * 3)
        limit = MEAN_DIFF_LOSSLESS if actual_format in LOSSLESS else MEAN_DIFF_LOSSY
        add(SavedCheck("content_matches_canvas", mean <= limit, f"mean difference {mean:.2f} (limit {limit})"))
    return report
