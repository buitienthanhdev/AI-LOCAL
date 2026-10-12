"""A reference picture rendered in a chosen mode (pure computation; the result is a `Scene`).

One function per rendering mode. They differ in what they keep of the picture (tone, edges, colour regions, strokes)
and in what they refuse to produce: a pencil drawing contains no colour, an ink drawing no tone but black, a
watercolour leaves the paper white. The result is verified against that promise (`evaluate.mode_compliance`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from lga_worker.art import painterly
from lga_worker.art.modes import SPECS, Mode
from lga_worker.art.scene import RGB, Point, Scene, Shape, luminance
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import sketch as sketching
from lga_worker.paint import vectorize
from lga_worker.paint.model import Canvas

SHEET = Canvas(1152, 648)  # proportions only: composition units 1000 x 562.5
PAPER: RGB = (255, 255, 255)


@dataclass
class Styled:
    scene: Scene
    mode: Mode
    stroke_based: bool
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _detail(detail: str) -> str:
    return "max" if detail == "ultra" else detail


def _grey(image: Image.Image) -> Image.Image:
    return image.convert("L").convert("RGB")


# ------------------------------------------------------------------------------------------------- shared: regions


def regions(image: Image.Image, colors: int, detail: str = "max", *, smooth: float | None = None
            ) -> list[dict[str, Any]]:
    """Colour regions of the picture: [{points (sheet units), rgb, area}], largest first (disjoint, 4-connected)."""
    cv2, np_, ndimage, slic = vectorize._imports()
    cfg = vectorize.DETAIL[_detail(detail)]
    rgb_img = image.convert("RGB")
    left, top, box_w, box_h = vectorize.fit_box(rgb_img.size, SHEET)
    scale = cfg["long"] / max(rgb_img.size)
    ww, wh = max(8, round(rgb_img.width * scale)), max(8, round(rgb_img.height * scale))
    small = np_.asarray(rgb_img.resize((ww, wh), Image.Resampling.LANCZOS))
    segments = slic(small, n_segments=cfg["segments"], compactness=vectorize.SLIC_COMPACTNESS, start_label=0, convert2lab=True)
    count = int(segments.max()) + 1
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np_.float32)
    flat = segments.reshape(-1)
    sizes = np_.bincount(flat, minlength=count).astype(np_.float32)
    means = np_.stack([np_.bincount(flat, weights=lab.reshape(-1, 3)[:, c], minlength=count) for c in range(3)], axis=1)
    means = (means / np_.maximum(sizes, 1)[:, None]).astype(np_.float32)
    present = sizes > 0
    pal_lab = vectorize.free_palette(np_, means[present], sizes[present], max(2, min(int(colors), 48)))
    labels = vectorize._smooth_assignment(np_, segments, means, pal_lab, cfg["smooth"] if smooth is None else smooth)[segments]
    labels = labels.astype(np_.int32)
    k = len(pal_lab)
    pal_rgb = cv2.cvtColor(np_.clip(pal_lab, 0, 255).astype(np_.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    min_area = max(6, int(ww * wh * cfg["min_area"]))
    for _ in range(12):
        cleaned = vectorize._absorb_small(np_, cv2, ndimage, labels, k, min_area)
        found = vectorize._regions(np_, cv2, cleaned, k, min_area)
        if len(found) <= cfg["regions"] * 1.6:
            break
        min_area = int(min_area * 1.3) + 1
    ux, uy = box_w / ww, box_h / wh
    out: list[dict[str, Any]] = []
    for label, mask, area in found:
        pts = vectorize._trace(np_, cv2, mask)
        if pts is None or len(pts) < 3:
            continue
        out.append({"points": [(left + float(x) * ux, top + float(y) * uy) for x, y in pts],
                    "rgb": (int(pal_rgb[label][0]), int(pal_rgb[label][1]), int(pal_rgb[label][2])), "area": area})
    if not out:
        raise ToolError(ErrorCode.EXECUTION_FAILED, "no regions could be found in this picture")
    out.sort(key=lambda r: -r["area"])
    return out


def _polygon(points: list[Point]) -> Any | None:
    from shapely.geometry import Polygon

    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.geom_type != "Polygon" or poly.area < 1.0:
        return None
    return poly


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def _grey_of(v: float) -> RGB:
    g = int(max(0, min(255, round(v))))
    return (g, g, g)


# ------------------------------------------------------------------------------------------------- tonal drawings


def _hatching(polygon: Any, spacing: float, angle: float, rng: np.random.Generator, jitter: float) -> list[tuple[Point, Point]]:
    lines = sketching._hatch(polygon, spacing, angle)
    out = []
    for (x0, y0), (x1, y1) in lines:
        j = jitter
        out.append(((x0 + rng.uniform(-j, j), y0 + rng.uniform(-j, j)), (x1 + rng.uniform(-j, j), y1 + rng.uniform(-j, j))))
    return out


def _pencil(image: Image.Image, detail: str, max_strokes: int, charcoal: bool) -> Styled:
    from shapely.ops import unary_union

    rng = np.random.default_rng(5)
    levels = 4
    regs = regions(_grey(image), levels + 1, detail, smooth=6.0)
    # tones (0 darkest .. levels): regions of one grey are merged, so a hatch line runs through all of them
    greys = sorted({r["rgb"][0] for r in regs})
    groups: dict[int, list[Any]] = {}
    for r in regs:
        poly = _polygon(r["points"])
        if poly is not None:
            groups.setdefault(greys.index(r["rgb"][0]), []).append(poly)
    n = len(greys)
    rules = {  # tone rank (0 = darkest) -> (spacing in units, cross-hatch)
        True: [(3.0, False), (4.2, False), (6.4, False), (10.0, False), None],
        False: [(2.4, True), (3.2, False), (4.8, False), (8.0, False), None],
    }[charcoal]
    width = 3.0 if charcoal else 0.55
    shapes: list[Shape] = []
    factor = 1.0
    while True:
        shapes = []
        for rank in sorted(groups):
            rule = rules[min(len(rules) - 1, round(rank * (len(rules) - 1) / max(1, n - 1)))] if n > 1 else rules[1]
            merged = unary_union([g.buffer(0.4) for g in groups[rank]])
            parts = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
            lum = luminance((greys[rank],) * 3)
            tone = _grey_of((14 + 70 * lum) if charcoal else (20 + 110 * lum))
            for poly in parts:
                if poly.is_empty or poly.geom_type != "Polygon":
                    continue
                if not charcoal and rank <= n // 2:  # a soft outline where tone changes
                    shapes.append(Shape(tuple(poly.exterior.coords)[:-1], True, None, _grey_of(60 + 90 * lum), 0.45))
                if rule is None:
                    continue
                spacing = rule[0] * factor
                if poly.area < spacing * spacing * 2.5:
                    continue
                angle = 40.0 + rng.uniform(-5, 5) + 12.0 * rank
                for a_, b_ in _hatching(poly, spacing, angle, rng, 0.3 if not charcoal else 0.7):
                    shapes.append(Shape((a_, b_), False, None, tone, width))
                if rule[1]:
                    for a_, b_ in _hatching(poly, spacing * 1.2, angle + 75.0, rng, 0.3):
                        shapes.append(Shape((a_, b_), False, None, tone, width))
        if len(shapes) <= max_strokes or factor > 6:
            break
        factor *= 1.18
    mode = Mode.CHARCOAL_SKETCH if charcoal else Mode.PENCIL_SKETCH
    return Styled(Scene(shapes, background=PAPER), mode, True,
                  stats={"strokes": len(shapes), "tones": n, "hatch_spacing_factor": round(factor, 2)})


def _ink(image: Image.Image, detail: str, max_strokes: int) -> Styled:
    rng = np.random.default_rng(3)
    regs = regions(_grey(image), 6, detail)
    black: RGB = (14, 14, 16)
    shapes: list[Shape] = []
    factor = 1.0
    while True:
        shapes = []
        for r in regs:
            lum = luminance(r["rgb"])
            poly = _polygon(r["points"])
            if poly is None:
                continue
            if lum < 0.2:
                shapes.append(Shape(tuple(r["points"]), True, black, black, 0.8))  # solid black mass
            else:
                shapes.append(Shape(tuple(r["points"]), True, None, black, 0.9))  # contour only
                if lum < 0.45 and poly.area > (6.0 * factor) ** 2 * 2:
                    for a, b in _hatching(poly, 6.0 * factor, 45.0, rng, 0.3):
                        shapes.append(Shape((a, b), False, None, black, 0.45))
        if len(shapes) <= max_strokes or factor > 5:
            break
        factor *= 1.25
    return Styled(Scene(shapes, background=PAPER), Mode.INK_LINE_ART, True, stats={"strokes": len(shapes), "regions": len(regs)})


def _black_white(image: Image.Image, detail: str) -> Styled:
    import cv2

    img = image.convert("L")
    left, top, box_w, box_h = vectorize.fit_box(img.size, SHEET)
    w = 640
    h = max(8, round(w * img.height / img.width))
    arr = np.asarray(img.resize((w, h), Image.Resampling.LANCZOS))
    arr = cv2.GaussianBlur(arr, (0, 0), 1.0)
    _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)  # ink = dark
    contours, hier = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    ux, uy = box_w / w, box_h / h
    black: RGB = (12, 12, 12)
    shapes = [Shape(((left, top), (left + box_w, top), (left + box_w, top + box_h), (left, top + box_h)), True, PAPER, None, 1.0, "rect")]
    order = sorted(range(len(contours)), key=lambda i: -cv2.contourArea(contours[i]))
    min_area = 5.0 if detail in ("max", "ultra") else 14.0
    depth: dict[int, int] = {}
    for i in order:
        parent = hier[0][i][3]
        depth[i] = 0 if parent < 0 else depth.get(parent, 0) + 1
    drawn = 0
    for i in sorted(order, key=lambda j: depth[j]):
        if cv2.contourArea(contours[i]) < min_area:
            continue
        ap = cv2.approxPolyDP(contours[i], 0.9, True).reshape(-1, 2)
        if len(ap) < 3:
            continue
        pts = tuple((left + (float(x) + 0.5) * ux, top + (float(y) + 0.5) * uy) for x, y in ap)
        shapes.append(Shape(pts, True, black if depth[i] % 2 == 0 else PAPER, None, 1.0))
        drawn += 1
    return Styled(Scene(shapes, background=PAPER), Mode.BLACK_WHITE, False,
                  stats={"shapes": drawn, "threshold": "otsu"})


def _grayscale(image: Image.Image, detail: str) -> Styled:
    traced = vectorize.vectorize(_grey(image), SHEET, detail=_detail(detail), palette="free", free_colors=8, mouse=False,
                                 outline=False)
    shapes = []
    for o in sorted(traced.objects, key=lambda o: o.layer):
        r = int(o.fill[1:3], 16)
        shapes.append(Shape(tuple(o.points), True, _grey_of(r), None, 1.0))
    return Styled(Scene(shapes, background=PAPER), Mode.GRAYSCALE, False, stats={"regions": len(shapes), "greys": 8})


# ------------------------------------------------------------------------------------------------- colour regions


def _flat(image: Image.Image, detail: str, colors: int, outline: bool, mode: Mode = Mode.FLAT_COLOR) -> Styled:
    traced = vectorize.vectorize(image, SHEET, detail=_detail(detail), palette="free", free_colors=colors, mouse=False,
                                 outline=outline)
    shapes = []
    for o in sorted(traced.objects, key=lambda o: o.layer):
        fill = tuple(int(o.fill[i:i + 2], 16) for i in (1, 3, 5))
        stroke = (40, 40, 40) if outline else None
        shapes.append(Shape(tuple(o.points), True, fill, stroke, 0.7))  # type: ignore[arg-type]
    return Styled(Scene(shapes, background=PAPER), mode, False, stats={"regions": len(shapes), "colors": colors})


def _watercolor(image: Image.Image, detail: str) -> Styled:
    from shapely.geometry import Polygon

    regs = regions(image, 16, detail, smooth=5.0)
    halos: list[Shape] = []
    washes: list[Shape] = []
    glazes: list[Shape] = []
    skipped = 0
    for r in regs:
        rgb = r["rgb"]
        lum = luminance(rgb)
        if lum > 0.9:
            skipped += 1
            continue  # paper left white
        wash = _mix(rgb, PAPER, 0.42)
        poly = _polygon(r["points"])
        if poly is None:
            continue
        halo = poly.buffer(1.4, join_style=1)
        if halo.geom_type == "Polygon" and not halo.is_empty:
            halos.append(Shape(tuple(halo.exterior.coords)[:-1], True, _mix(wash, PAPER, 0.6), None, 1.0))  # soft bleed
        washes.append(Shape(tuple(r["points"]), True, wash, _mix(wash, (0, 0, 0), 0.2), 0.5))  # wash, darker wet edge
        if lum < 0.5:
            inner = poly.buffer(-2.4)
            if not inner.is_empty and isinstance(inner, Polygon):
                glazes.append(Shape(tuple(inner.exterior.coords)[:-1], True, _mix(rgb, PAPER, 0.2), None, 1.0))  # deeper glaze
    shapes = [*halos, *washes, *glazes]
    return Styled(Scene(shapes, background=PAPER), Mode.WATERCOLOR_SIMULATION, False,
                  stats={"washes": len(washes), "halos": len(halos), "glazes": len(glazes), "paper_regions": skipped})


LACQUER = {"black": (23, 16, 12), "red_dark": (112, 24, 18), "red": (178, 40, 28), "gold": (201, 162, 39),
           "gold_light": (232, 200, 96), "eggshell": (240, 232, 210)}


def _lacquer(image: Image.Image, detail: str) -> Styled:
    rng = np.random.default_rng(11)
    regs = regions(image, 12, detail, smooth=6.0)
    left, top, box_w, box_h = vectorize.fit_box(image.size, SHEET)
    shapes = [Shape(((left, top), (left + box_w, top), (left + box_w, top + box_h), (left, top + box_h)), True, LACQUER["black"],
                    None, 1.0, "rect")]
    for r in regs:
        rr, gg, bb = r["rgb"]
        lum = luminance(r["rgb"])
        yellow = rr > 140 and gg > 110 and bb < 0.75 * gg
        if lum < 0.2:
            colour = LACQUER["black"]
        elif yellow and lum > 0.45:
            colour = LACQUER["gold_light"] if lum > 0.7 else LACQUER["gold"]
        elif lum < 0.38:
            colour = LACQUER["red_dark"]
        elif lum < 0.58:
            colour = LACQUER["red"]
        elif lum < 0.78:
            colour = LACQUER["gold"]
        else:
            colour = LACQUER["eggshell"] if lum > 0.88 else LACQUER["gold_light"]
        shapes.append(Shape(tuple(r["points"]), True, colour, LACQUER["gold"] if colour != LACQUER["black"] else None, 0.8))
        # polished highlights: eggshell flecks in the light regions
        poly = _polygon(r["points"])
        if poly is not None and colour in (LACQUER["gold_light"], LACQUER["eggshell"], LACQUER["gold"]) and poly.area > 60:
            minx, miny, maxx, maxy = poly.bounds
            for _ in range(min(10, int(poly.area / 160) + 1)):
                x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
                from shapely.geometry import Point as ShPoint

                if poly.contains(ShPoint(x, y)):
                    s = rng.uniform(1.6, 3.4)
                    a = rng.uniform(0, math.pi)
                    quad = tuple((x + s * math.cos(a + k * math.pi / 2 + rng.uniform(-0.4, 0.4)),
                                  y + s * math.sin(a + k * math.pi / 2 + rng.uniform(-0.4, 0.4))) for k in range(4))
                    shapes.append(Shape(quad, True, LACQUER["eggshell"], None, 1.0))
    return Styled(Scene(shapes, background=LACQUER["black"]), Mode.LACQUER_ART_SIMULATION, False,
                  stats={"regions": len(regs), "shapes": len(shapes), "palette": list(LACQUER)})


def _technical(image: Image.Image, detail: str, max_strokes: int) -> Styled:
    regs = regions(_grey(image), 4, detail, smooth=7.0)
    tints = (238, 214, 186, 150)
    levels = sorted({r["rgb"][0] for r in regs})
    shapes = []
    for r in regs:
        tint = tints[min(len(tints) - 1, levels.index(r["rgb"][0]) * len(tints) // max(1, len(levels)))]
        poly = _polygon(r["points"])
        if poly is None:
            continue
        simple = poly.simplify(0.9, preserve_topology=True)
        pts = tuple(simple.exterior.coords)[:-1] if simple.geom_type == "Polygon" else tuple(r["points"])
        shapes.append(Shape(pts, True, _grey_of(tint), (20, 20, 20), 0.8))
    return Styled(Scene(shapes[:max_strokes], background=PAPER), Mode.TECHNICAL_ILLUSTRATION, False,
                  stats={"regions": len(shapes)})


def _pixel(image: Image.Image, detail: str, max_shapes: int = 4000) -> Styled:
    cv2, np_, _, _ = vectorize._imports()
    left, top, box_w, box_h = vectorize.fit_box(image.size, SHEET)
    cols_try = [96, 80, 64, 56, 48, 40, 32, 24] if detail in ("max", "ultra") else [64, 48, 40, 32, 24]
    best: Styled | None = None
    for cols in cols_try:
        rows = max(4, round(cols * box_h / box_w))
        cell = box_w / cols
        small = np_.asarray(image.convert("RGB").resize((cols, rows), Image.Resampling.BOX))
        lab = vectorize._lab(cv2, np_, small.reshape(-1, 3))
        pal_lab = vectorize.free_palette(np_, lab, np_.ones(len(lab), np_.float32), 16)
        pal = cv2.cvtColor(np_.clip(pal_lab, 0, 255).astype(np_.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
        idx = ((lab[:, None, :] - pal_lab[None, :, :]) ** 2).sum(axis=2).argmin(axis=1).reshape(rows, cols)
        base = int(np_.bincount(idx.reshape(-1)).argmax())
        shapes = [Shape(((left, top), (left + cols * cell, top), (left + cols * cell, top + rows * cell),
                         (left, top + rows * cell)), True, (int(pal[base][0]), int(pal[base][1]), int(pal[base][2])), None, 1.0, "rect")]
        for y in range(rows):
            x = 0
            while x < cols:
                c = int(idx[y, x])
                x2 = x
                while x2 + 1 < cols and int(idx[y, x2 + 1]) == c:
                    x2 += 1
                if c != base:
                    x0, y0 = left + x * cell, top + y * cell
                    x1, y1 = left + (x2 + 1) * cell, top + (y + 1) * cell
                    shapes.append(Shape(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), True,
                                        (int(pal[c][0]), int(pal[c][1]), int(pal[c][2])), None, 1.0, "rect"))
                x = x2 + 1
        best = Styled(Scene(shapes, background=PAPER), Mode.PIXEL_ART, False,
                      stats={"grid": [cols, rows], "cell_units": round(cell, 2), "runs": len(shapes) - 1, "colors": 16})
        if len(shapes) <= max_shapes:
            break
    assert best is not None
    return best


# ------------------------------------------------------------------------------------------------- entry point


def stylize(image: Image.Image, mode: Mode, *, detail: str = "max", max_strokes: int = 1100, colors: int = 24,
            outline: bool | None = None) -> Styled:
    """The reference picture as a Scene in `mode`. `max_strokes` bounds brush-stroke modes (what the target can take)."""
    if mode == Mode.PENCIL_SKETCH:
        out = _pencil(image, detail, max_strokes, charcoal=False)
    elif mode == Mode.CHARCOAL_SKETCH:
        out = _pencil(image, detail, max_strokes, charcoal=True)
    elif mode == Mode.INK_LINE_ART:
        out = _ink(image, detail, max_strokes)
    elif mode == Mode.BLACK_WHITE:
        out = _black_white(image, detail)
    elif mode == Mode.GRAYSCALE:
        out = _grayscale(image, detail)
    elif mode == Mode.FLAT_COLOR:
        out = _flat(image, detail, colors, True if outline is None else outline)
    elif mode == Mode.HYBRID_RENDERING:
        out = _flat(image, detail, min(colors, 18), True, Mode.HYBRID_RENDERING)
    elif mode in (Mode.PAINTERLY, Mode.OIL_PAINT_SIMULATION):
        style = painterly.OIL if mode == Mode.OIL_PAINT_SIMULATION else painterly.PAINTERLY
        shapes, stats = painterly.paint(image, style, max_strokes=max_strokes)
        out = Styled(Scene(shapes, background=PAPER), mode, True, stats=stats)
    elif mode == Mode.ACRYLIC_SIMULATION:
        flat = _flat(image, detail, 18, False, Mode.ACRYLIC_SIMULATION)
        budget = max(100, max_strokes - len(flat.scene.shapes))
        shapes, stats = painterly.paint(image, painterly.ACRYLIC, max_strokes=min(budget, 500))
        # blocks first, then a coat of broad strokes over them (the base polygon of the stroke pass is dropped)
        out = Styled(Scene([*flat.scene.shapes, *shapes[1:]], background=PAPER), Mode.ACRYLIC_SIMULATION, False,
                     stats={**flat.stats, "strokes": stats["strokes"]})
    elif mode == Mode.WATERCOLOR_SIMULATION:
        out = _watercolor(image, detail)
    elif mode == Mode.LACQUER_ART_SIMULATION:
        out = _lacquer(image, detail)
    elif mode == Mode.TECHNICAL_ILLUSTRATION:
        out = _technical(image, detail, max_strokes)
    elif mode == Mode.PIXEL_ART:
        out = _pixel(image, detail, max_strokes)
    else:  # pragma: no cover - every Mode is listed above
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"unsupported rendering mode {mode}")
    out.notes.append(SPECS[mode].honesty)
    return out
