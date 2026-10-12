"""Picture -> a drawing plan of filled polygons (what `paint.draw_image` executes).

Pipeline: the picture is cut into edge-following superpixels (SLIC), every superpixel takes its mean colour, the
colours are reduced to Paint's own palette (the 20 swatches that are always there, plus up to 10 custom colours
chosen to fit the picture: all are one click away in the ribbon), same-colour neighbours merge into regions, and
each region's outline is traced and simplified into a polygon. Polygons are layered by nesting depth, so painting
them in layer order (a region first, what lies inside it afterwards) reproduces the picture with nothing but
filled polygons: the one shape Paint draws exactly, fast and verifiably.

Pure computation (OpenCV, NumPy, SciPy, scikit-image: part of the worker's environment); nothing here touches
the screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint.model import Canvas, DrawingObject

MARGIN_FRACTION = 0.05  # a little more than the plan's own 4 %: traced points stay inside the drawable area
MIN_VERTEX_GAP_CANVAS_PX = 5.0  # closer clicks would read as a double click and end the polygon early
MIN_REGION_CANVAS_PX = 5.0  # a polygon smaller than this in both directions is not worth a drawing step
MIN_LONGEST_EDGE_CANVAS_PX = 16.0  # Paint ignores a polygon whose first dragged edge is shorter than ~12 px
OUTLINE_SIMPLIFY_WORK_PX = 0.9
UPSAMPLE = 4
OUTLINE_COLOR = "#322823"  # one dark brown outline colour for the whole picture: a colour change per region would cost more
MAX_VERTICES = 26  # each vertex is one click; a long polygon is also where Paint is most likely to misread one
CUSTOM_SLOTS = 10  # custom colours visible in Paint's ribbon at once
SLIC_COMPACTNESS = 14.0
GROW_THRESHOLD = 70  # of 255 on the blurred mask: below 127 grows a region by a fraction of a pixel

# The 20 swatches of Paint 11's ribbon (measured RGB values): free to use, no Edit colours dialog needed.
STANDARD_COLORS: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (127, 127, 127), (136, 0, 21), (237, 28, 36), (255, 127, 39), (255, 242, 0), (34, 177, 76),
    (0, 162, 232), (63, 72, 204), (163, 73, 164), (255, 255, 255), (195, 195, 195), (185, 122, 87),
    (255, 174, 201), (255, 201, 14), (239, 228, 176), (181, 230, 29), (153, 217, 234), (112, 146, 190),
    (200, 191, 231),
)

DEFAULT_DETAIL = "max"  # always the most detailed; a lower level only when the user asks for a quick or rough one

DETAIL = {
    # work size (long side), superpixel count, minimum region area (fraction of the picture), polygon budget
    "low": {"long": 240, "segments": 260, "min_area": 0.0030, "regions": 70, "smooth": 9.0},
    "normal": {"long": 360, "segments": 800, "min_area": 0.0012, "regions": 130, "smooth": 7.0},
    "high": {"long": 480, "segments": 1500, "min_area": 0.0006, "regions": 220, "smooth": 5.5},
    # the default: every region Paint can be asked for in a reasonable time (about 7 minutes for a busy photo)
    "max": {"long": 640, "segments": 2600, "min_area": 0.0002, "regions": 300, "smooth": 3.0},
    # for applications that draw a shape in milliseconds (office shapes, files): hundreds more regions
    "ultra": {"long": 900, "segments": 6500, "min_area": 0.00006, "regions": 1400, "smooth": 2.2},
}
MOUSE_DETAILS = ("low", "normal", "high", "max")  # the levels a mouse-driven backend (Paint) can be asked for


@dataclass
class Vectorized:
    objects: list[DrawingObject]
    palette: list[str]
    stats: dict[str, Any] = field(default_factory=dict)


def _imports() -> tuple[Any, Any, Any, Any]:
    try:
        import cv2
        import numpy as np
        from scipy import ndimage
        from skimage.segmentation import slic
    except ImportError as exc:  # pragma: no cover - the worker environment ships these
        raise ToolError(ErrorCode.UNSUPPORTED, f"image tracing needs OpenCV, NumPy, SciPy and scikit-image: {exc}") from exc
    return cv2, np, ndimage, slic


def fit_box(image_size: tuple[int, int], canvas: Canvas, margin: float = MARGIN_FRACTION) -> tuple[float, float, float, float]:
    """Where the picture lands on the canvas, in composition units: (left, top, width, height), centred."""
    iw, ih = image_size
    avail_w = 1000.0 * (1 - 2 * margin)
    avail_h = canvas.unit_height * (1 - 2 * margin)
    scale = min(avail_w / iw, avail_h / ih)
    w, h = iw * scale, ih * scale
    return (1000.0 - w) / 2, (canvas.unit_height - h) / 2, w, h


def _lab(cv2: Any, np: Any, rgb: Any) -> Any:
    return cv2.cvtColor(np.asarray(rgb, np.uint8).reshape(1, -1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)


def build_palette(cv2: Any, np: Any, points: Any, weights: Any, custom: int) -> tuple[Any, list[tuple[int, int, int]]]:
    """Palette in Lab: the fixed standard swatches plus `custom` free colours fitted to what the standard ones
    cover badly (k-means where only the free centres move). Returns (Nx3 Lab array, matching RGB tuples)."""
    fixed = _lab(cv2, np, STANDARD_COLORS)
    free = np.empty((0, 3), np.float32)
    if custom > 0:
        d = ((points[:, None, :] - fixed[None, :, :]) ** 2).sum(axis=2).min(axis=1)
        rng = np.random.default_rng(7)
        chosen: list[Any] = []
        cost = d * weights
        for _ in range(custom):  # k-means++ seeding against the standard colours
            if cost.sum() <= 0:
                break
            idx = int(rng.choice(len(points), p=cost / cost.sum()))
            chosen.append(points[idx])
            cost = np.minimum(cost, ((points - points[idx]) ** 2).sum(axis=1) * weights)
        free = np.array(chosen, np.float32).reshape(-1, 3)
    for _ in range(14):
        palette = np.vstack([fixed, free]) if len(free) else fixed
        assign = ((points[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        moved = False
        for j in range(len(free)):
            m = assign == len(fixed) + j
            if m.any():
                new = (points[m] * weights[m, None]).sum(axis=0) / weights[m].sum()
                if np.abs(new - free[j]).max() > 0.2:
                    moved = True
                free[j] = new
        if not moved:
            break
    palette = np.vstack([fixed, free]) if len(free) else fixed
    rgb_free = cv2.cvtColor(np.clip(free, 0, 255).astype(np.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    rgb = list(STANDARD_COLORS) + [(int(c[0]), int(c[1]), int(c[2])) for c in rgb_free]
    return palette, rgb


def free_palette(np: Any, points: Any, weights: Any, k: int) -> Any:
    """Weighted k-means in Lab with k-means++ seeding (deterministic): `k` colours that fit the picture, for
    applications that can use any RGB value."""
    k = max(2, min(k, len(points)))
    rng = np.random.default_rng(11)
    centers = [points[int(rng.choice(len(points), p=weights / weights.sum()))]]
    cost = ((points - centers[0]) ** 2).sum(axis=1) * weights
    for _ in range(k - 1):
        if cost.sum() <= 0:
            break
        idx = int(rng.choice(len(points), p=cost / cost.sum()))
        centers.append(points[idx])
        cost = np.minimum(cost, ((points - points[idx]) ** 2).sum(axis=1) * weights)
    palette = np.array(centers, np.float32)
    for _ in range(16):
        assign = ((points[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        moved = False
        for j in range(len(palette)):
            m = assign == j
            if m.any():
                new = (points[m] * weights[m, None]).sum(axis=0) / weights[m].sum()
                moved |= bool(np.abs(new - palette[j]).max() > 0.2)
                palette[j] = new
        if not moved:
            break
    return palette


def _smooth_assignment(np: Any, segments: Any, means: Any, palette_lab: Any, weight: float, rounds: int = 8) -> Any:
    """Palette index per superpixel: nearest colour, then iterated conditional modes with a Potts term, so
    neighbours with similar colours agree on one palette colour (no blotches across gradients) while a real edge
    (a large colour difference) still wins. `weight` is the price, in Lab units, of one unit of shared border."""
    count = means.shape[0]
    dist = np.sqrt(((means[:, None, :] - palette_lab[None, :, :]) ** 2).sum(axis=2))  # unary cost
    labels = dist.argmin(axis=1)
    if weight <= 0:
        return labels
    pairs: dict[tuple[int, int], float] = {}
    for a, b in ((segments[:, :-1], segments[:, 1:]), (segments[:-1, :], segments[1:, :])):
        m = a != b
        lo, hi = np.minimum(a[m], b[m]), np.maximum(a[m], b[m])
        keys, counts = np.unique(lo.astype(np.int64) * count + hi, return_counts=True)
        for key, n in zip(keys.tolist(), counts.tolist()):
            pair = (key // count, key % count)
            pairs[pair] = pairs.get(pair, 0.0) + float(n)
    neighbours: list[list[tuple[int, float]]] = [[] for _ in range(count)]
    mean_len = (sum(pairs.values()) / len(pairs)) if pairs else 1.0
    for (a, b), n in pairs.items():
        w = n / mean_len
        neighbours[a].append((b, w))
        neighbours[b].append((a, w))
    k = palette_lab.shape[0]
    for _ in range(rounds):
        changed = 0
        for node in range(count):
            if not neighbours[node]:
                continue
            cost = dist[node].copy()
            for other, w in neighbours[node]:
                penalty = np.full(k, weight * w, dtype=np.float32)
                penalty[labels[other]] = 0.0
                cost += penalty
            best = int(cost.argmin())
            if best != labels[node]:
                labels[node] = best
                changed += 1
        if not changed:
            break
    return labels


def _regions(np: Any, cv2: Any, labels: Any, k: int, min_area: int) -> list[tuple[int, Any, int]]:
    """(label, mask, area) for every 4-connected region with at least `min_area` pixels."""
    out: list[tuple[int, Any, int]] = []
    for label in range(k):
        mask = (labels == label).astype(np.uint8)
        if not mask.any():
            continue
        count, comp, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area >= min_area:
                out.append((label, comp == i, area))
    return out


def _absorb_small(np: Any, cv2: Any, ndimage: Any, labels: Any, k: int, min_area: int) -> Any:
    """Give regions smaller than `min_area` the label of the nearest bigger region."""
    for _ in range(3):
        keep = np.zeros(labels.shape, dtype=bool)
        for _label, mask, _area in _regions(np, cv2, labels, k, min_area):
            keep |= mask
        if keep.all():
            break
        _, (iy, ix) = ndimage.distance_transform_edt(~keep, return_indices=True)
        labels = labels[iy, ix]
    return labels


def _trace(np: Any, cv2: Any, mask: Any, max_vertices: int = MAX_VERTICES) -> Any | None:
    """The outer outline of a region as an Nx2 float array in work pixels (smoothed, simplified), or None."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    pad = 3
    y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + pad + 1, mask.shape[0])
    x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + pad + 1, mask.shape[1])
    crop = (mask[y0:y1, x0:x1].astype(np.uint8)) * 255
    big = cv2.resize(crop, (crop.shape[1] * UPSAMPLE, crop.shape[0] * UPSAMPLE), interpolation=cv2.INTER_LINEAR)
    big = cv2.GaussianBlur(big, (0, 0), UPSAMPLE * 0.55)
    _, binary = cv2.threshold(big, GROW_THRESHOLD, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    outline = max(contours, key=cv2.contourArea)
    eps = OUTLINE_SIMPLIFY_WORK_PX * UPSAMPLE
    while True:
        approx = cv2.approxPolyDP(outline, eps, True)
        if len(approx) <= max_vertices or eps > 12 * UPSAMPLE:
            break
        eps *= 1.35
    # contour coordinates are pixel indices of the upsampled grid: their centres, back in work-pixel space
    return (approx.reshape(-1, 2).astype(np.float64) + 0.5) / UPSAMPLE + np.array([x0, y0])


def _space_vertices(np: Any, pts: Any, min_gap_work: float) -> Any:
    kept = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, kept[-1]) >= min_gap_work:
            kept.append(p)
    if len(kept) > 2 and math.dist(kept[0], kept[-1]) < min_gap_work:
        kept.pop()
    return np.array(kept)


def vectorize(image: Image.Image, canvas: Canvas, *, detail: str = DEFAULT_DETAIL, custom_colors: int = CUSTOM_SLOTS,
              max_regions: int | None = None, outline: bool = True, palette: str = "paint", free_colors: int = 40,
              mouse: bool = True) -> Vectorized:
    """`palette="paint"`: Paint's 20 swatches plus `custom_colors` fitted ones; `"free"`: `free_colors` fitted colours
    (any RGB). `mouse=False` drops the limits that exist only because a person-like mouse draws each vertex."""
    cv2, np, ndimage, slic = _imports()
    if detail not in DETAIL:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"detail must be one of {', '.join(DETAIL)}")
    cfg = DETAIL[detail]
    budget = max_regions or cfg["regions"]
    custom_colors = max(0, min(int(custom_colors), CUSTOM_SLOTS))
    rgb_img = image.convert("RGB")
    left, top, box_w, box_h = fit_box(rgb_img.size, canvas)

    scale = cfg["long"] / max(rgb_img.size)
    ww, wh = max(8, round(rgb_img.width * scale)), max(8, round(rgb_img.height * scale))
    small = np.asarray(rgb_img.resize((ww, wh), Image.Resampling.LANCZOS))
    segments = slic(small, n_segments=cfg["segments"], compactness=SLIC_COMPACTNESS, start_label=0, convert2lab=True)
    count = int(segments.max()) + 1
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).astype(np.float32)
    flat_seg = segments.reshape(-1)
    sizes = np.bincount(flat_seg, minlength=count).astype(np.float32)
    means = np.stack([np.bincount(flat_seg, weights=lab.reshape(-1, 3)[:, c], minlength=count) for c in range(3)], axis=1)
    means = (means / np.maximum(sizes, 1)[:, None]).astype(np.float32)
    present = sizes > 0
    if palette == "free":
        palette_lab = free_palette(np, means[present], sizes[present], max(4, min(int(free_colors), 96)))
        rgb_free = cv2.cvtColor(np.clip(palette_lab, 0, 255).astype(np.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB)
        palette_rgb = [(int(c[0]), int(c[1]), int(c[2])) for c in rgb_free.reshape(-1, 3)]
    else:
        palette_lab, palette_rgb = build_palette(cv2, np, means[present], sizes[present], custom_colors)
    nearest = _smooth_assignment(np, segments, means, palette_lab, cfg["smooth"])
    labels = nearest[segments].astype(np.int32)
    k = len(palette_rgb)

    min_area = max(6, int(ww * wh * cfg["min_area"]))
    while True:
        cleaned = _absorb_small(np, cv2, ndimage, labels, k, min_area)
        regions = _regions(np, cv2, cleaned, k, min_area)
        if len(regions) <= budget:
            break
        min_area = int(min_area * 1.3) + 1

    px_per_work = box_w * canvas.px_per_unit / ww  # canvas pixels per work pixel
    min_gap_work = MIN_VERTEX_GAP_CANVAS_PX / px_per_work if mouse else 0.0
    vertex_cap = MAX_VERTICES if mouse else 80
    traced: list[dict[str, Any]] = []
    for label, mask, _area in regions:
        pts = _trace(np, cv2, mask, vertex_cap)
        if pts is None or len(pts) < 3:
            continue
        pts = _space_vertices(np, pts, min_gap_work) if min_gap_work > 0 else pts
        if len(pts) < 3:
            continue
        w_c = (pts[:, 0].max() - pts[:, 0].min()) * px_per_work
        h_c = (pts[:, 1].max() - pts[:, 1].min()) * px_per_work
        if max(w_c, h_c) < MIN_REGION_CANVAS_PX or min(w_c, h_c) < 1.5:
            continue
        ring = np.vstack([pts, pts[:1]])
        longest = float(np.max(np.hypot(*(ring[1:] - ring[:-1]).T))) * px_per_work
        if mouse and longest < MIN_LONGEST_EDGE_CANVAS_PX:
            continue
        dist = ndimage.distance_transform_edt(mask)
        iy, ix = np.unravel_index(int(np.argmax(dist)), dist.shape)  # deepest interior pixel decides nesting
        traced.append({"label": label, "pts": pts, "area": float(cv2.contourArea(pts.astype(np.float32))),
                       "inside": (float(ix) + 0.5, float(iy) + 0.5)})

    traced.sort(key=lambda r: -r["area"])
    contours = [r["pts"].astype(np.float32).reshape(-1, 1, 2) for r in traced]
    for i, r in enumerate(traced):
        r["depth"] = sum(1 for j in range(i) if cv2.pointPolygonTest(contours[j], r["inside"], False) >= 0)

    objects: list[DrawingObject] = []
    area_by_label: dict[int, int] = {}
    for label_id, _mask, area in regions:
        area_by_label[label_id] = area_by_label.get(label_id, 0) + area
    if area_by_label:
        dominant = max(area_by_label, key=area_by_label.get)
        r0, g0, b0 = palette_rgb[dominant]
        x1, y1 = round(left + box_w, 2), round(top + box_h, 2)
        objects.append(DrawingObject(id="base", kind="polygon", points=((round(left, 2), round(top, 2)), (x1, round(top, 2)),
                                                                        (x1, y1), (round(left, 2), y1)),
                                     fill=f"#{r0:02X}{g0:02X}{b0:02X}", stroke=OUTLINE_COLOR if outline else None,
                                     weight="detail", layer=0))
    for n, r in enumerate(traced):
        points = []
        for x, y in r["pts"]:
            ux = min(max(left + x * (box_w / ww), left), left + box_w)
            uy = min(max(top + y * (box_h / wh), top), top + box_h)
            points.append((round(ux, 2), round(uy, 2)))
        red, green, blue = palette_rgb[r["label"]]
        objects.append(DrawingObject(id=f"r{n:03d}", kind="polygon", points=tuple(points),
                                     fill=f"#{red:02X}{green:02X}{blue:02X}", stroke=OUTLINE_COLOR if outline else None,
                                     weight="detail", layer=r["depth"] + 1))
    if not objects:
        raise ToolError(ErrorCode.EXECUTION_FAILED, "no regions could be traced from this picture")
    used = sorted({o.fill for o in objects if o.fill})
    standard = {"#%02X%02X%02X" % c for c in STANDARD_COLORS}
    return Vectorized(objects=objects, palette=used,
                      stats={"regions": len(objects), "colors": len(used),
                             "custom_colors": len([c for c in used if c not in standard]),
                             "work_size": [ww, wh], "max_depth": max(o.layer for o in objects),
                             "min_area_px": min_area, "superpixels": count,
                             "box_units": [round(left, 1), round(top, 1), round(box_w, 1), round(box_h, 1)]})


def fidelity(source: Image.Image, rendered: Image.Image, canvas: Canvas) -> dict[str, float]:
    """How close the planned picture is to the source over the area it occupies: structural similarity of the grey
    images (1 = identical) and the mean colour error (0 = identical)."""
    cv2, np, _, _ = _imports()
    from skimage.metrics import structural_similarity

    left, top, box_w, box_h = fit_box(source.size, canvas)
    k = canvas.px_per_unit
    box = (round(left * k), round(top * k), round((left + box_w) * k), round((top + box_h) * k))
    target = (box[2] - box[0], box[3] - box[1])
    want = np.asarray(source.convert("RGB").resize(target, Image.Resampling.LANCZOS))
    got = np.asarray(rendered.convert("RGB").crop(box))
    if got.shape != want.shape:
        got = np.asarray(Image.fromarray(got).resize(target))
    ssim = float(structural_similarity(cv2.cvtColor(want, cv2.COLOR_RGB2GRAY), cv2.cvtColor(got, cv2.COLOR_RGB2GRAY),
                                       data_range=255))
    mae = float(np.abs(want.astype(np.int16) - got.astype(np.int16)).mean() / 255.0)
    return {"ssim": round(ssim, 3), "color_error": round(mae, 3)}
