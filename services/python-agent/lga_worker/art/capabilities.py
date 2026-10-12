"""What each drawing application can do, and how to find the profile for the name a person used.

Backends: `paint` (mouse-driven filled shapes, 30 reachable colours), `autocad` (line entities only, true colour),
`powerpoint` / `word` / `excel` (freeform shapes through COM, any RGB), `file` (an SVG and PNG written to disk, for
any other application that can open or import a picture).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class AppProfile:
    key: str
    title: str
    backend: str  # paint | autocad | office | file
    fills: bool  # can pour a colour into a closed region
    true_colour: bool  # any RGB value, not a small palette
    max_shapes: int  # practical limit for one drawing
    aliases: tuple[str, ...]
    note: str = ""


PROFILES: tuple[AppProfile, ...] = (
    AppProfile("paint", "Microsoft Paint", "paint", True, False, 330,
               ("paint", "mspaint", "ms paint", "microsoft paint")),
    AppProfile("autocad", "AutoCAD", "autocad", False, True, 12000,
               ("autocad", "auto cad", "cad", "dwg", "acad", "autodesk")),
    AppProfile("powerpoint", "Microsoft PowerPoint", "office", True, True, 4000,
               ("powerpoint", "power point", "ppt", "pptx", "slide", "slides", "presentation")),
    AppProfile("word", "Microsoft Word", "office", True, True, 1500,
               ("word", "winword", "docx", "document")),
    AppProfile("excel", "Microsoft Excel", "office", True, True, 1500,
               ("excel", "xlsx", "spreadsheet", "workbook")),
    AppProfile("file", "SVG and PNG files", "file", True, True, 100000,
               ("file", "svg", "png", "image", "export", "inkscape", "illustrator", "figma", "canva", "photoshop",
                "gimp", "krita", "browser", "web")),
)
DEFAULT_APP = "paint"


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def resolve(name: str | None) -> AppProfile | None:
    """The profile for an application name ("mspaint", "Microsoft Paint", "AutoCAD 2023"...), None when unknown."""
    text = _norm(name or DEFAULT_APP)
    if not text:
        return None
    for profile in PROFILES:
        if text == profile.key or text in profile.aliases:
            return profile
    for profile in PROFILES:
        if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in profile.aliases):
            return profile
    return None


def style_for(profile: AppProfile, requested: str | None) -> tuple[str, str | None]:
    """(style, note): a picture is drawn filled where the application can fill, as a coloured pencil sketch where
    it can only draw lines; a sketch is drawn anywhere when asked for."""
    wanted = (requested or "auto").lower()
    if wanted == "sketch":
        return "sketch", None
    if profile.fills:
        return "fill", None
    note = None
    if wanted == "fill":
        note = f"{profile.title} cannot fill regions with colour: drawn as a coloured pencil sketch instead"
    return "sketch", note


def supported() -> str:
    return ", ".join(p.key for p in PROFILES)
