"""Picture -> a coloured pencil sketch: strokes only, each stroke in the colour of the thing it describes.

For applications that cannot fill a region with colour (a line-drawing CAD, a plotter) or when a sketch is asked
for. The picture is cut into colour regions exactly as for the filled drawing (`vectorize`), and then drawn the
way a person sketches:

* a contour: the outline of every region, in the region's own colour;
* shading: parallel hatching inside the regions that are not light, denser where the colour is darker
  (cross-hatched where it is very dark), again in the region's colour; light regions are left to the paper.

Pure computation; the strokes are drawn by whichever application the caller drives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import vectorize
from lga_worker.paint.model import Canvas, Point

DEFAULT_COLORS = 24
HATCH_ANGLE_DEG = 45.0
# luminance (0 dark .. 1 light) -> hatch spacing in composition units (None: no shading). 1 unit = 1/1000 of the width.
HATCH_RULES: tuple[tuple[float, float | None, bool], ...] = (
    (0.22, 2.6, True),    # very dark: tight cross-hatching
    (0.45, 3.4, False),   # dark: tight hatching
    (0.72, 6.0, False),   # medium: open hatching
    (1.01, None, False),  # light: paper
)
MIN_HATCH_SEGMENT = 3.2  # units: shorter slivers are dropped
INSET = 0.5  # units: hatching stays this far inside the contour


@dataclass(frozen=True)
class Stroke:
    points: tuple[Point, ...]  # composition units, y down (like the picture)
    color: str  # '#RRGGBB'
    closed: bool = False
    kind: str = "contour"  # "contour" | "hatch"


@dataclass
class Sketch:
    strokes: list[Stroke]
    palette: list[str]
    stats: dict[str, Any] = field(default_factory=dict)


def _luminance(rgb: tuple[int, int, int]) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255.0


def _hatch(polygon: Any, spacing: float, angle_deg: float) -> list[tuple[Point, Point]]:
    """Parallel segments `spacing` apart at `angle_deg`, clipped to the inside of `polygon`."""
    from shapely import affinity
    from shapely.geometry import LineString

    inner = polygon.buffer(-INSET)
    if inner.is_empty:
        return []
    centre = polygon.centroid
    rotated = affinity.rotate(inner, -angle_deg, origin=centre)
    minx, miny, maxx, maxy = rotated.bounds
    segments: list[tuple[Point, Point]] = []
    y = miny + spacing / 2
    while y < maxy:
        clipped = rotated.intersection(LineString([(minx - 1, y), (maxx + 1, y)]))
        pieces = [clipped] if clipped.geom_type == "LineString" else list(getattr(clipped, "geoms", []))
        for piece in pieces:
            if piece.geom_type != "LineString" or piece.length < MIN_HATCH_SEGMENT:
                continue
            back = affinity.rotate(piece, angle_deg, origin=centre)
            (x0, y0), (x1, y1) = back.coords[0], back.coords[-1]
            segments.append(((x0, y0), (x1, y1)))
        y += spacing
    return segments


def sketch(image: Image.Image, canvas: Canvas, *, detail: str = vectorize.DEFAULT_DETAIL, colors: int = DEFAULT_COLORS,
           shading: bool = True, max_strokes: int = 9000) -> Sketch:
    cv2, np, ndimage, slic = vectorize._imports()
    from shapely.geometry import Polygon

    if detail not in vectorize.DETAIL:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"detail must be one of {', '.join(vectorize.DETAIL)}")
    cfg = vectorize.DETAIL[detail]
    colors = max(4, min(int(colors), 48))
    rgb_img = image.convert("RGB")
    left, top, box_w, box_h = vectorize.fit_box(rgb_img.size, canvas)
    scale = cfg["long"] / max(rgb_img.size)
    ww, wh = max(8, round(rgb_img.width * scale)), max(8, round(rgb_img.height * scale))
    small = np.asarray(rgb_img.resize((ww, wh), Image.Resampling.LANCZOS))
    segments = slic(small, n_segments=cfg["segments"], compactness=vectorize.SLIC_COMPACTNESS, start_label=0,
                    convert2lab=True)
    count = int(segments.max()) + 1
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np.float32)
    flat = segments.reshape(-1)
    sizes = np.bincount(flat, minlength=count).astype(np.float32)
    means = np.stack([np.bincount(flat, weights=lab.reshape(-1, 3)[:, c], minlength=count) for c in range(3)], axis=1)
    means = (means / np.maximum(sizes, 1)[:, None]).astype(np.float32)
    present = sizes > 0
    palette_lab = vectorize.free_palette(np, means[present], sizes[present], colors)
    assignment = vectorize._smooth_assignment(np, segments, means, palette_lab, cfg["smooth"])
    labels = assignment[segments].astype(np.int32)
    k = len(palette_lab)
    rgb_pal = cv2.cvtColor(np.clip(palette_lab, 0, 255).astype(np.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    palette_rgb = [(int(c[0]), int(c[1]), int(c[2])) for c in rgb_pal]

    min_area = max(6, int(ww * wh * cfg["min_area"]))
    while True:
        cleaned = vectorize._absorb_small(np, cv2, ndimage, labels, k, min_area)
        regions = vectorize._regions(np, cv2, cleaned, k, min_area)
        if len(regions) <= cfg["regions"] * 1.6:
            break
        min_area = int(min_area * 1.3) + 1

    ux, uy = box_w / ww, box_h / wh

    def to_units(pts: Any) -> list[Point]:
        return [(left + float(x) * ux, top + float(y) * uy) for x, y in pts]

    contours: list[Stroke] = []
    shapes: list[tuple[Any, tuple[int, int, int]]] = []
    for label, mask, _area in regions:
        pts = vectorize._trace(np, cv2, mask)
        if pts is None or len(pts) < 3:
            continue
        units = to_units(pts)
        rgb = palette_rgb[label]
        color = "#%02X%02X%02X" % rgb
        contours.append(Stroke(tuple(units), color, closed=True, kind="contour"))
        polygon = Polygon(units)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty and polygon.geom_type == "Polygon":
            shapes.append((polygon, rgb))
    if not contours:
        raise ToolError(ErrorCode.EXECUTION_FAILED, "no regions could be traced from this picture")

    hatches: list[Stroke] = []
    factor = 1.0
    while shading:
        hatches = []
        for polygon, rgb in shapes:
            lum = _luminance(rgb)
            spacing, cross = next((s, c) for limit, s, c in HATCH_RULES if lum < limit)
            if spacing is None or polygon.area < (spacing * factor) ** 2 * 3:
                continue
            color = "#%02X%02X%02X" % rgb
            angles = (HATCH_ANGLE_DEG, HATCH_ANGLE_DEG + 90.0) if cross else (HATCH_ANGLE_DEG,)
            for angle in angles:
                for a, b in _hatch(polygon, spacing * factor, angle):
                    hatches.append(Stroke((a, b), color, closed=False, kind="hatch"))
        if len(contours) + len(hatches) <= max_strokes or factor > 6:
            break
        factor *= 1.25

    strokes = contours + hatches
    palette = sorted({s.color for s in strokes})
    return Sketch(strokes=strokes, palette=palette,
                  stats={"contours": len(contours), "hatch_lines": len(hatches), "colors": len(palette),
                         "strokes": len(strokes), "hatch_spacing_factor": round(factor, 2),
                         "box_units": [round(left, 1), round(top, 1), round(box_w, 1), round(box_h, 1)]})


def render(strokes: list[Stroke], canvas: Canvas, paper: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """The sketch as pixels on `paper`, at canvas resolution (for similarity and previews)."""
    image = Image.new("RGB", (canvas.width, canvas.height), paper)
    draw = ImageDraw.Draw(image)
    k = canvas.px_per_unit
    for s in strokes:
        pts = [(x * k, y * k) for x, y in s.points]
        if s.closed:
            pts.append(pts[0])
        color = tuple(int(s.color[i:i + 2], 16) for i in (1, 3, 5))
        draw.line(pts, fill=color, width=1)
    return image


def aci_nearest(rgb: tuple[int, int, int]) -> int:
    """The AutoCAD Color Index closest to an RGB colour (fallback when true colour is not available)."""
    best, best_d = 7, 10 ** 9
    for index, (r, g, b) in _aci_table():
        d = (r - rgb[0]) ** 2 + (g - rgb[1]) ** 2 + (b - rgb[2]) ** 2
        if d < best_d:
            best, best_d = index, d
    return best


_ACI: list[tuple[int, tuple[int, int, int]]] | None = None


def _aci_table() -> list[tuple[int, tuple[int, int, int]]]:
    global _ACI
    if _ACI is None:
        import colorsys

        table: list[tuple[int, tuple[int, int, int]]] = [
            (1, (255, 0, 0)), (2, (255, 255, 0)), (3, (0, 255, 0)), (4, (0, 255, 255)), (5, (0, 0, 255)),
            (6, (255, 0, 255)), (7, (255, 255, 255)), (8, (65, 65, 65)), (9, (128, 128, 128))]
        values = (1.0, 1.0, 0.65, 0.65, 0.5, 0.5, 0.3, 0.3, 0.15, 0.15)
        for index in range(10, 250):
            hue = ((index - 10) // 10) * 15 / 360.0
            j = (index - 10) % 10
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0 if j % 2 == 0 else 0.5, values[j])
            table.append((index, (round(r * 255), round(g * 255), round(b * 255))))
        for step, index in enumerate(range(250, 256)):
            level = round(51 + step * 33.0)
            table.append((index, (level, level, level)))
        _ACI = table
    return _ACI


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def stroke_length(s: Stroke) -> float:
    pts = list(s.points) + ([s.points[0]] if s.closed else [])
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))
