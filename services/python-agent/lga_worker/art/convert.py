"""Sources -> Scene, and Scene -> what each kind of application can draw.

Into a Scene: a photograph or other raster picture (traced to colour regions, or to coloured pencil strokes), the
planner's own objects and templates (the Paint engine's `DrawingObject`s), SVG (`art.svg`).

Out of a Scene: Paint objects (palette-snapped, vertex-limited, layered by overlap so the mouse paints them back to
front), pencil strokes for a line-only CAD (hidden lines removed, fills shaded by hatching in the fill colour).
Office shapes are built straight from the Scene by `art.office`.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image

from lga_worker.art.scene import RGB, Point, Scene, Shape, hex_of, luminance, render, simplify
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import geometry, sketch as sketching, vectorize
from lga_worker.paint.model import Canvas, DrawingObject

SHEET = Canvas(1152, 648)  # only its 16:9 proportions matter: composition units 1000 x 562.5 = sheet units
FREE_COLORS = 64
OUTLINE_WIDTH = 0.7  # sheet units
PENCIL_WIDTH = 0.5
CAD_MAX_STROKES = 12_000
MIN_PIECE = 0.3  # sheet units: a visible line piece shorter than this is dropped
# luminance limit -> hatch spacing (sheet units) for an explicit fill; every fill is shaded, light ones openly
FILL_HATCH_RULES: tuple[tuple[float, float, bool], ...] = (
    (0.22, 2.6, True), (0.45, 3.4, False), (0.72, 5.0, False), (1.01, 7.0, False))
PAINT_MAX_VERTICES = vectorize.MAX_VERTICES
PAINT_MIN_VERTEX_GAP = vectorize.MIN_VERTEX_GAP_CANVAS_PX
PAINT_MIN_EDGE = vectorize.MIN_LONGEST_EDGE_CANVAS_PX


# --------------------------------------------------------------------------------------------------- into a Scene


def _rgb(value: str | None, what: str) -> RGB | None:
    return None if value is None else geometry.parse_color(value, what=what)


def scene_from_objects(objects: list[DrawingObject], canvas: Canvas = SHEET) -> Scene:
    """The planner's own objects (composition units of `canvas`) as a Scene; rect, ellipse and line keep a hint so
    a backend can draw them with its own primitive."""
    plan = geometry.plan_scene(objects, canvas, check_overlaps=False)
    k = canvas.px_per_unit
    shapes: list[Shape] = []
    for o in plan.objects:
        pts = tuple((x / k, y / k) for x, y in o.outline)
        closed = o.kind in geometry.CLOSED_KINDS
        shapes.append(Shape(pts, closed=closed, fill=o.fill, stroke=o.stroke, width=max(0.3, o.stroke_px / k),
                            hint=o.kind if o.kind in ("rect", "ellipse", "line") else None))
    return Scene(shapes, width=1000.0, height=canvas.unit_height, notes=list(plan.warnings))


def scene_from_template(name: str, options: dict[str, str] | None = None) -> Scene:
    from lga_worker.paint.templates import build_template

    return scene_from_objects(build_template(name, SHEET, **(options or {})), SHEET)


def _traced_to_scene(traced: vectorize.Vectorized, outline: bool) -> Scene:
    shapes: list[Shape] = []
    ordered = sorted(enumerate(traced.objects), key=lambda t: (t[1].layer, t[0]))
    for _, o in ordered:
        fill = _rgb(o.fill, "fill")
        line = _rgb(o.stroke, "stroke") if outline else None
        shapes.append(Shape(tuple(o.points), closed=True, fill=fill, stroke=line, width=OUTLINE_WIDTH))
    return Scene(shapes, notes=[])


def scene_from_image(image: Image.Image, *, style: str = "fill", detail: str = "ultra",
                     colors: int = FREE_COLORS, outline: bool = False, shading: bool = True,
                     max_strokes: int = 9000) -> Scene:
    """A raster picture as a Scene: `style="fill"` colour regions (free palette of `colors`; no outlines by
    default, they cost similarity when there are hundreds of regions), `style="sketch"` coloured pencil strokes
    (contours and shading, each in the colour of what it describes)."""
    if style == "sketch":
        sketch_detail = "max" if detail == "ultra" else detail
        traced = sketching.sketch(image, SHEET, detail=sketch_detail, colors=min(max(int(colors), 4), 48), shading=shading,
                                  max_strokes=max_strokes)
        shapes = [Shape(tuple(s.points), closed=s.closed, fill=None, stroke=sketching.hex_to_rgb(s.color), width=PENCIL_WIDTH)
                  for s in traced.strokes]
        scene = Scene(shapes, notes=[])
        scene.notes.append(f"pencil sketch: {traced.stats['contours']} contours, {traced.stats['hatch_lines']} shading lines")
        return scene
    traced = vectorize.vectorize(image, SHEET, detail=detail, palette="free", free_colors=int(colors), mouse=False,
                                 outline=outline)
    scene = _traced_to_scene(traced, outline)
    scene.notes.append(f"traced into {traced.stats['regions']} colour regions with {traced.stats['colors']} colours")
    return scene


# ------------------------------------------------------------------------------------------------------ geometry


def _polygon(points: tuple[Point, ...]) -> Any | None:
    from shapely.geometry import Polygon

    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 1e-9:
        return None
    return poly


def overlap_depths(scene: Scene) -> list[int]:
    """Layer per shape: 1 + the deepest earlier shape it really overlaps (touching edges do not count), so
    shapes of one layer never overlap and layers painted in order reproduce the painter's order."""
    from shapely import STRtree

    geoms: list[Any | None] = []
    for s in scene.shapes:
        if s.closed and len(s.points) >= 3:
            geoms.append(_polygon(s.points))
        else:
            from shapely.geometry import LineString

            geoms.append(LineString(s.points).buffer(max(s.width, 0.3) / 2) if len(s.points) >= 2 else None)
    valid = [g for g in geoms if g is not None]
    tree = STRtree(valid) if valid else None
    position = {id(g): i for i, g in enumerate(geoms) if g is not None}
    depth = [0] * len(geoms)
    for i, g in enumerate(geoms):
        if g is None or tree is None:
            continue
        best = 0
        for j in tree.query(g):
            other = valid[int(j)]
            jj = position[id(other)]
            if jj >= i:
                continue
            if g.intersection(other).area > 1e-6 or (g.geom_type != "Polygon" and g.intersects(other)):
                best = max(best, depth[jj] + 1)
        depth[i] = best
    return depth


# ---------------------------------------------------------------------------------------------------- to Paint


def _weight_for(canvas: Canvas, desired_px: float) -> str:
    """The weight class whose snapped stroke width is closest to `desired_px`."""
    from lga_worker.paint.model import WEIGHT_FACTORS

    return min(WEIGHT_FACTORS, key=lambda w: (abs(geometry.stroke_px(canvas, w) - desired_px), WEIGHT_FACTORS[w]))


def _limit_vertices(pts: list[Point], closed: bool, max_vertices: int, min_gap: float) -> list[Point]:
    """Douglas-Peucker with a growing tolerance until the vertex count fits, then neighbours closer than `min_gap`
    (read as a double click by the mouse) are merged."""
    tolerance = 0.6
    out = simplify(pts, tolerance, closed)
    while len(out) > max_vertices and tolerance < 400:
        tolerance *= 1.35
        out = simplify(pts, tolerance, closed)
    kept = [out[0]]
    for p in out[1:]:
        if math.dist(p, kept[-1]) >= min_gap:
            kept.append(p)
    if closed and len(kept) > 2 and math.dist(kept[0], kept[-1]) < min_gap:
        kept.pop()
    return kept


def paint_palette(scene: Scene) -> tuple[dict[RGB, RGB], list[RGB]]:
    """Map every colour of the scene to one Paint can have at hand (its 20 swatches or up to 10 fitted custom
    colours). Returns (mapping, custom colours used)."""
    import cv2
    import numpy as np

    colours = scene.colors()
    if not colours:
        return {}, []
    index = {c: i for i, c in enumerate(colours)}
    weights = np.ones(len(colours), np.float32)
    for s in scene.shapes:  # a colour matters in proportion to the area it covers (strokes: a little)
        if s.fill is not None and len(s.points) >= 3:
            xs = np.array([p[0] for p in s.points])
            ys = np.array([p[1] for p in s.points])
            area = 0.5 * abs(float(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))))
            weights[index[s.fill]] += area / 100.0
        if s.stroke is not None:
            weights[index[s.stroke]] += 1.0
    points = vectorize._lab(cv2, np, colours)
    lab, rgb = vectorize.build_palette(cv2, np, points, weights, vectorize.CUSTOM_SLOTS)
    nearest = ((points[:, None, :] - lab[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
    mapping = {c: tuple(rgb[int(i)]) for c, i in zip(colours, nearest, strict=True)}  # type: ignore[misc]
    standard = set(vectorize.STANDARD_COLORS)
    return mapping, sorted({m for m in mapping.values() if m not in standard})


def to_paint_objects(scene: Scene, canvas: Canvas, *, brush: str | None = None, exact_width: bool = False
                     ) -> tuple[list[DrawingObject], dict[str, Any]]:
    """The Scene as Paint objects fitted into `canvas` (composition units). Returns (objects, report)."""
    left, top, box_w, box_h = vectorize.fit_box((int(scene.width * 10), int(scene.height * 10)), canvas)
    unit = box_w / scene.width  # composition units per sheet unit
    px = canvas.px_per_unit
    mapping, custom = paint_palette(scene)

    def hexc(c: RGB | None) -> str | None:
        return None if c is None else hex_of(mapping.get(c, c))

    def at(p: Point) -> Point:
        return (round(left + p[0] * unit, 2), round(top + p[1] * unit, 2))

    depths = overlap_depths(scene)
    objects: list[DrawingObject] = []
    dropped = 0
    if scene.background is not None and mapping.get(scene.background, scene.background) != (255, 255, 255):
        bg = hexc(scene.background)
        corners = ((round(left, 2), round(top, 2)), (round(left + box_w, 2), round(top + box_h, 2)))
        objects.append(DrawingObject(id="background", kind="rect", points=corners, fill=bg, stroke=bg,
                                     weight="detail", layer=0))
    base = 1 if objects else 0
    for index, (shape, depth) in enumerate(zip(scene.shapes, depths, strict=True)):
        fill = hexc(shape.fill)
        stroke = hexc(shape.stroke)
        if fill is not None and stroke is None:
            stroke = fill  # Paint draws the outline in the other colour slot: keep it the fill's own colour
        weight = _weight_for(canvas, shape.width * unit * px)
        pts_u = [at(p) for p in shape.points]
        pts_px = [(x * px, y * px) for x, y in pts_u]
        xs = [p[0] for p in pts_px]
        ys = [p[1] for p in pts_px]
        w_px, h_px = max(xs) - min(xs), max(ys) - min(ys)
        oid = f"s{index:04d}"
        layer = depth + 1 + base
        if max(w_px, h_px) < 3.0:
            dropped += 1
            continue
        if shape.hint in ("rect", "ellipse") and min(w_px, h_px) >= 3.0 and shape.closed:
            box = ((min(p[0] for p in pts_u), min(p[1] for p in pts_u)), (max(p[0] for p in pts_u), max(p[1] for p in pts_u)))
            objects.append(DrawingObject(id=oid, kind=shape.hint, points=box, fill=fill, stroke=stroke, weight=weight,
                                         layer=layer))
            continue
        if shape.closed and len(pts_u) >= 3:
            limited = _limit_vertices(pts_px, True, PAINT_MAX_VERTICES, PAINT_MIN_VERTEX_GAP)
            if len(limited) < 3 or max(math.dist(a, b) for a, b in zip(limited, limited[1:] + limited[:1])) < PAINT_MIN_EDGE:
                dropped += 1  # too small for the mouse-drawn polygon: Paint ignores a first edge shorter than ~12 px
                continue
            points = tuple((round(x / px, 2), round(y / px, 2)) for x, y in limited)
            objects.append(DrawingObject(id=oid, kind="polygon", points=points, fill=fill, stroke=stroke, weight=weight,
                                         layer=layer))
        else:
            if stroke is None:
                continue
            limited = _limit_vertices(pts_px, False, 160, 2.0)
            if len(limited) < 2:
                dropped += 1
                continue
            points = tuple((round(x / px, 2), round(y / px, 2)) for x, y in limited)
            kind = "line" if len(points) == 2 and brush is None else "path"
            width_px = max(1, min(90, round(shape.width * unit * px))) if exact_width else None
            objects.append(DrawingObject(id=oid, kind=kind, points=points, fill=None, stroke=stroke, weight=weight,
                                         layer=layer, width_px=width_px, brush=brush if kind == "path" else None))
    if not objects:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, "nothing in the picture is large enough to draw in Paint")
    report = {"objects": len(objects), "dropped_small": dropped, "custom_colors": [hex_of(c) for c in custom],
              "colors": len({o.fill for o in objects if o.fill} | {o.stroke for o in objects if o.stroke}),
              "box_units": [round(left, 1), round(top, 1), round(box_w, 1), round(box_h, 1)]}
    return objects, report


# --------------------------------------------------------------------------------------------------- to CAD


def _line_pieces(geometry_: Any) -> list[list[Point]]:
    if geometry_.is_empty:
        return []
    if geometry_.geom_type == "LineString":
        return [list(geometry_.coords)]
    if geometry_.geom_type in ("MultiLineString", "GeometryCollection"):
        out: list[list[Point]] = []
        for g in geometry_.geoms:
            out.extend(_line_pieces(g))
        return out
    return []


def to_pencil_strokes(scene: Scene, *, shading: bool = True, max_strokes: int = CAD_MAX_STROKES
                      ) -> list[tuple[list[Point], RGB, bool]]:
    """Strokes (points in sheet units, y down; colour; closed) for an application that can only draw lines.

    Painter's order is honoured by hidden-line removal: what a later filled shape covers is not drawn. A filled
    shape becomes its visible outline plus hatching in its own colour (denser where darker), so the sketch keeps
    the colour of each thing; a stroke-only shape is drawn as it is."""
    from shapely import STRtree
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    polys = [(_polygon(s.points) if s.fill is not None and s.closed else None) for s in scene.shapes]
    solid = [(i, p) for i, p in enumerate(polys) if p is not None]
    tree = STRtree([p for _, p in solid]) if solid else None
    owner = [i for i, _ in solid]

    def cover_of(i: int, geom: Any) -> Any | None:
        if tree is None:
            return None
        parts = [solid[int(j)][1] for j in tree.query(geom) if owner[int(j)] > i]
        return unary_union(parts) if parts else None

    factor = 1.0
    while True:
        strokes: list[tuple[list[Point], RGB, bool]] = []
        for i, s in enumerate(scene.shapes):
            ring = list(s.points) + ([s.points[0]] if s.closed and len(s.points) > 2 else [])
            if len(ring) < 2:
                continue
            outline = LineString(ring)
            cover = cover_of(i, outline.buffer(0.01) if polys[i] is None else polys[i])
            colour = s.stroke or s.fill
            if colour is None:
                continue
            if s.stroke is not None or polys[i] is not None:
                visible = outline if cover is None else outline.difference(cover)
                for piece in _line_pieces(visible):
                    if LineString(piece).length >= MIN_PIECE:
                        closed = cover is None and s.closed and len(piece) > 2 and piece[0] == piece[-1]
                        strokes.append((piece[:-1] if closed else piece, s.stroke or s.fill, closed))
            if polys[i] is not None and shading and s.fill is not None:
                lum = luminance(s.fill)
                spacing, cross = next((sp, c) for limit, sp, c in FILL_HATCH_RULES if lum < limit)
                region = polys[i] if cover is None else polys[i].difference(cover)
                if region.is_empty:
                    continue
                parts = [region] if region.geom_type == "Polygon" else [g for g in region.geoms if g.geom_type == "Polygon"]
                for part in parts:
                    if part.area < (spacing * factor) ** 2 * 1.5:
                        continue
                    angles = (45.0, 135.0) if cross else (45.0,)
                    for angle in angles:
                        for a, b in sketching._hatch(part, spacing * factor, angle):
                            strokes.append(([a, b], s.fill, False))
        if len(strokes) <= max_strokes or factor > 8:
            return strokes
        factor *= 1.3


# --------------------------------------------------------------------------------------------------- critique

THICK_STROKE = 6.0  # sheet units: a stroke at least this wide is a part of the subject, not a line detail
MIN_SHAPES = {"ultra": 40, "max": 40, "high": 28, "normal": 14, "low": 0}


def _components(scene: Scene) -> tuple[list[Any], list[Any | None]]:
    """(connected components of the filled shapes, polygon per shape), components largest first."""
    from shapely.ops import unary_union

    from shapely.geometry import LineString

    polys: list[Any | None] = []
    for sh in scene.shapes:
        if sh.fill is not None and sh.closed:
            polys.append(_polygon(sh.points))
        elif sh.stroke is not None and sh.width >= THICK_STROKE and len(sh.points) >= 2:
            polys.append(LineString(sh.points).buffer(sh.width / 2, cap_style=2))  # a thick stroke is a body part (tail, limb)
        else:
            polys.append(None)
    solid = [p for p in polys if p is not None]
    if not solid:
        return [], polys
    merged = unary_union([p.buffer(0.8) for p in solid])
    parts = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
    parts.sort(key=lambda g: -g.area)
    return parts, polys


def detached_groups(scene: Scene, *, near: float = 0.35, max_gap: float = 140.0) -> list[dict[str, Any]]:
    """Groups of filled shapes that float apart from the main body of the picture but only just (a head above a body,
    a tail beside it): the gap is below `near` times the group's own size. Each entry: {"box", "gap", "vector"}.
    Far-away groups are separate objects (a sun, a tree) and are not reported."""
    from shapely.ops import nearest_points

    parts, polys = _components(scene)
    if len(parts) < 2 or sum(1 for p in polys if p is not None) < 8:
        return []
    main = parts[0]
    total = sum(p.area for p in parts)
    found: list[dict[str, Any]] = []
    for part in parts[1:]:
        if part.area < 0.004 * total:
            continue
        gap = part.distance(main)
        x0, y0, x1, y1 = part.bounds
        size = max(x1 - x0, y1 - y0)
        if 0 < gap <= min(near * size, max_gap):
            a, b = nearest_points(part, main)
            found.append({"box": [round(v) for v in part.bounds], "gap": round(gap, 1),
                          "vector": (b.x - a.x, b.y - a.y), "shape_part": part})
    return found


def attach_detached(scene: Scene, overlap: float = 3.0) -> int:
    """Move every near-miss group so that it touches the main body (a floating ear or limb is a drawing mistake,
    not a design). Returns how many groups were moved."""
    from shapely.geometry import Point as ShPoint

    moved = 0
    for _ in range(3):
        groups = detached_groups(scene)
        if not groups:
            break
        shapes = list(scene.shapes)
        for group in groups:
            dx, dy = group["vector"]
            norm = math.hypot(dx, dy) or 1.0
            dx, dy = dx + dx / norm * overlap, dy + dy / norm * overlap
            part = group["shape_part"].buffer(1.0)
            for i, sh in enumerate(shapes):
                poly = _polygon(sh.points) if sh.closed and len(sh.points) >= 3 else None
                probe = poly.representative_point() if poly is not None else ShPoint(sh.points[0])
                if part.contains(probe):
                    shapes[i] = Shape(tuple((x + dx, y + dy) for x, y in sh.points), sh.closed, sh.fill, sh.stroke,
                                      sh.width, sh.hint)
            moved += 1
        scene.shapes = shapes
    return moved
