"""Rendering modes: HOW a picture is made, chosen before anything is drawn.

A mode is not a filter name. It fixes what the finished canvas may contain (colour or not), which of Paint's real
tools carry it, and what the simulation honestly is not. Paint has no oil, no watercolour paper, no lacquer: every
mode below is a visual simulation made of strokes and regions, and `honesty` says so in the report.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    PENCIL_SKETCH = "PENCIL_SKETCH"
    CHARCOAL_SKETCH = "CHARCOAL_SKETCH"
    INK_LINE_ART = "INK_LINE_ART"
    BLACK_WHITE = "BLACK_WHITE"
    GRAYSCALE = "GRAYSCALE"
    FLAT_COLOR = "FLAT_COLOR"
    PAINTERLY = "PAINTERLY"
    WATERCOLOR_SIMULATION = "WATERCOLOR_SIMULATION"
    OIL_PAINT_SIMULATION = "OIL_PAINT_SIMULATION"
    ACRYLIC_SIMULATION = "ACRYLIC_SIMULATION"
    LACQUER_ART_SIMULATION = "LACQUER_ART_SIMULATION"
    TECHNICAL_ILLUSTRATION = "TECHNICAL_ILLUSTRATION"
    PIXEL_ART = "PIXEL_ART"
    HYBRID_RENDERING = "HYBRID_RENDERING"


@dataclass(frozen=True)
class ModeSpec:
    mode: Mode
    label: str  # Vietnamese name shown to the user
    colour: str  # "none" (ink on white only) | "grey" (neutral tones only) | "limited" | "full"
    strokes: bool  # made of brush strokes (True) or of filled regions (False)
    brushes: tuple[str, ...]  # Paint brush types in order of preference; the first one the running Paint offers is used
    honesty: str  # what the simulation is not
    patterns: tuple[str, ...]  # regexes over accent-free lower-case text


SPECS: dict[Mode, ModeSpec] = {s.mode: s for s in (
    ModeSpec(Mode.PENCIL_SKETCH, "Ký họa bút chì", "grey", True, ("Natural pencil", "Pencil"),
             "graphite tones are simulated with thin grey strokes and hatching; there is no real graphite grain",
             (r"\bbut chi\b", r"\bpencil\b", r"\bgraphite\b", r"\bky hoa\b", r"\bphac hoa\b", r"\bphac thao\b", r"\bsketch\b")),
    ModeSpec(Mode.CHARCOAL_SKETCH, "Ký họa than", "grey", True, ("Crayon", "Natural pencil", "Brush"),
             "Paint has no charcoal: broad dark strokes of the crayon brush stand in for it",
             (r"\bcharcoal\b", r"\bthan chi\b", r"\bbang than\b", r"\bve than\b")),
    ModeSpec(Mode.INK_LINE_ART, "Tranh nét mực", "none", True, ("Calligraphy pen", "Marker", "Pencil"),
             "ink is simulated by solid black strokes; line weight comes from the stroke size, not from a nib",
             (r"\bink\b", r"\bbut muc\b", r"\bbut kim\b", r"\bnet muc\b", r"\bline ?art\b", r"\bpen and ink\b")),
    ModeSpec(Mode.BLACK_WHITE, "Tranh trắng đen", "none", False, ("Brush",),
             "pure black shapes on white, two tones only",
             (r"\btrang den\b", r"\bden trang\b", r"\bblack and white\b", r"\bblack & white\b", r"\bb&w\b", r"\bmonochrome\b")),
    ModeSpec(Mode.GRAYSCALE, "Tranh thang độ xám", "grey", False, ("Brush",),
             "a limited set of neutral greys, flat regions",
             (r"\bthang do xam\b", r"\bgr[ae]yscale\b", r"\bmau xam\b", r"\bdo xam\b")),
    ModeSpec(Mode.FLAT_COLOR, "Tranh mảng màu phẳng", "full", False, ("Brush",),
             "flat colour regions; no brush texture",
             (r"\blogo\b", r"\bicon\b", r"\bflat\b", r"\bvector\b", r"\bbieu tuong\b", r"\bsticker\b", r"\bhoat hinh\b", r"\bcartoon\b")),
    ModeSpec(Mode.PAINTERLY, "Tranh nét cọ", "full", True, ("Brush", "Oil brush"),
             "directional brush strokes; no paint thickness",
             (r"\bpainterly\b", r"\bnet co\b", r"\bbrush ?strokes?\b", r"\bimpasto\b")),
    ModeSpec(Mode.WATERCOLOR_SIMULATION, "Mô phỏng màu nước", "full", False, ("Watercolour brush", "Brush"),
             "pale washes, paper left white and soft darker edges; Paint cannot make pigment bleed or granulate",
             (r"\bmau nuoc\b", r"\bwatercolou?r\b", r"\baquarelle\b")),
    ModeSpec(Mode.OIL_PAINT_SIMULATION, "Mô phỏng sơn dầu", "full", True, ("Oil brush", "Brush"),
             "overlapping directional strokes; no real impasto, glazing or wet-on-wet mixing",
             (r"\bson dau\b", r"\boil paint(ing)?\b", r"\boil on canvas\b", r"\btranh dau\b")),
    ModeSpec(Mode.ACRYLIC_SIMULATION, "Mô phỏng acrylic", "full", False, ("Brush", "Marker"),
             "opaque colour blocks with a few visible strokes",
             (r"\bacrylic\b",)),
    ModeSpec(Mode.LACQUER_ART_SIMULATION, "Mô phỏng tranh sơn mài", "limited", False, ("Brush", "Marker"),
             "a dark ground, red and gold with eggshell flecks and glossy highlights as painted shapes; the real "
             "craft (layers of resin, polishing, inlay) cannot be done digitally",
             (r"\bson mai\b", r"\blacquer\b")),
    ModeSpec(Mode.TECHNICAL_ILLUSTRATION, "Minh họa kỹ thuật", "limited", False, ("Pencil", "Brush"),
             "clean uniform outlines and light flat tints",
             (r"\bban ve ky thuat\b", r"\bminh hoa ky thuat\b", r"\btechnical (drawing|illustration)\b", r"\bblueprint\b")),
    ModeSpec(Mode.PIXEL_ART, "Pixel art", "limited", False, ("Pencil",),
             "a coarse grid of square cells and a small palette",
             (r"\bpixel ?art\b", r"\bpixel\b", r"\b8 ?bit\b", r"\b16 ?bit\b")),
    ModeSpec(Mode.HYBRID_RENDERING, "Kết hợp có chủ đích", "full", False, ("Brush",),
             "colour regions with ink outlines (an illustration)",
             ()),
)}

# Order matters: a named medium beats a tonal word ("bút chì trắng đen" is a pencil drawing).
_PRIORITY = (Mode.PENCIL_SKETCH, Mode.CHARCOAL_SKETCH, Mode.INK_LINE_ART, Mode.WATERCOLOR_SIMULATION, Mode.OIL_PAINT_SIMULATION,
             Mode.ACRYLIC_SIMULATION, Mode.LACQUER_ART_SIMULATION, Mode.PIXEL_ART, Mode.TECHNICAL_ILLUSTRATION,
             Mode.BLACK_WHITE, Mode.GRAYSCALE, Mode.PAINTERLY, Mode.FLAT_COLOR)

_NO_COLOUR = re.compile(r"\b(khong to mau|khong mau|khong can mau|no colou?rs?|without colou?r|uncolou?red|chi phac|"
                        r"trang den|den trang|black and white|monochrome)\b")
_OUTLINE_ONLY = re.compile(r"\b(chi phac net|chi ve net|chi phac|khong to|khong dien mau|outline only|lines? only|no fill)\b")


def normalize(text: str | None) -> str:
    """Lower case, no accents, single spaces: the form every pattern in this package is written for."""
    folded = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    ascii_text = folded.encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&+ ]+", " ", ascii_text)).strip()


def modes_in(text: str) -> list[Mode]:
    """Every mode whose words appear in `text`, most specific first."""
    t = normalize(text)
    return [m for m in _PRIORITY if any(re.search(p, t) for p in SPECS[m].patterns)]


def wants_no_colour(text: str) -> bool:
    return bool(_NO_COLOUR.search(normalize(text)))


def wants_outline_only(text: str) -> bool:
    return bool(_OUTLINE_ONLY.search(normalize(text)))


def parse_mode(value: str | None) -> Mode | None:
    if not value:
        return None
    key = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return Mode(key)
    except ValueError:
        return None
