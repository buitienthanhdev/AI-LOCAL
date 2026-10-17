"""Curve helpers for characters: cubic Bezier sampling at near-even spacing, rounded shapes, simplification.

Paint only receives mouse points, so a smooth curve has to arrive as a dense, evenly spaced list of points: a few
long segments polygonise it, thousands of points are slow. Everything here is pure arithmetic on (x, y) tuples in
whatever unit the caller uses (the spacing is given in the same unit).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]


def bezier_point(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]


def sample_bezier(p0: Point, p1: Point, p2: Point, p3: Point, spacing: float, *, include_start: bool = True) -> list[Point]:
    """Points along a cubic Bezier about `spacing` apart (arc-length, not parameter, spacing)."""
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    fine = [bezier_point(p0, p1, p2, p3, k / 400.0) for k in range(401)]
    lengths = [0.0]
    for a, b in zip(fine, fine[1:], strict=False):
        lengths.append(lengths[-1] + math.dist(a, b))
    total = lengths[-1]
    count = max(2, math.ceil(total / spacing))
    out: list[Point] = []
    j = 0
    for k in range(count + 1):
        target = total * k / count
        while j < len(lengths) - 2 and lengths[j + 1] < target:
            j += 1
        span = lengths[j + 1] - lengths[j]
        f = 0.0 if span <= 0 else (target - lengths[j]) / span
        out.append((fine[j][0] + f * (fine[j + 1][0] - fine[j][0]), fine[j][1] + f * (fine[j + 1][1] - fine[j][1])))
    return out if include_start else out[1:]


def sample_path(segments: Sequence[tuple[Point, Point, Point, Point]], spacing: float) -> list[Point]:
    """A chain of cubic Beziers (each starts where the previous one ended) as one point list."""
    out: list[Point] = []
    for i, seg in enumerate(segments):
        out.extend(sample_bezier(*seg, spacing, include_start=i == 0))
    return out


def bezier_midpoint(p0: Point, p1: Point, p2: Point, p3: Point) -> Point:
    return bezier_point(p0, p1, p2, p3, 0.5)


def superellipse(cx: float, cy: float, rx: float, ry: float, *, exponent: float = 2.0, count: int = 64) -> list[Point]:
    """Closed outline |x/rx|^n + |y/ry|^n = 1 (n = 2: ellipse, larger: squarer). Clockwise from the top."""
    pts: list[Point] = []
    for k in range(count):
        a = 2 * math.pi * k / count
        s, c = math.sin(a), math.cos(a)
        x = rx * math.copysign(abs(s) ** (2.0 / exponent), s)
        y = -ry * math.copysign(abs(c) ** (2.0 / exponent), c)
        pts.append((cx + x, cy + y))
    return pts


def capsule(x0: float, y0: float, x1: float, y1: float, *, per_cap: int = 10) -> list[Point]:
    """A rounded rectangle whose short side is fully rounded (pill) inside the box (x0, y0)-(x1, y1)."""
    w, h = x1 - x0, y1 - y0
    r = min(w, h) / 2.0
    pts: list[Point] = []
    if w >= h:  # horizontal pill: caps left and right
        cl, cr = (x0 + r, y0 + r), (x1 - r, y0 + r)
        for k in range(per_cap + 1):  # right cap, top to bottom
            a = -math.pi / 2 + math.pi * k / per_cap
            pts.append((cr[0] + r * math.cos(a), cr[1] + r * math.sin(a)))
        for k in range(per_cap + 1):  # left cap, bottom to top
            a = math.pi / 2 + math.pi * k / per_cap
            pts.append((cl[0] + r * math.cos(a), cl[1] + r * math.sin(a)))
    else:
        ct, cb = (x0 + r, y0 + r), (x0 + r, y1 - r)
        for k in range(per_cap + 1):
            a = math.pi * k / per_cap
            pts.append((cb[0] + r * math.cos(a), cb[1] + r * math.sin(a)))
        for k in range(per_cap + 1):
            a = math.pi + math.pi * k / per_cap
            pts.append((ct[0] + r * math.cos(a), ct[1] + r * math.sin(a)))
    return pts


def rotated_capsule(a: Point, b: Point, width: float, *, per_cap: int = 8) -> list[Point]:
    """A pill-shaped limb from centre `a` to centre `b` with the given width (fully round ends)."""
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    r = width / 2.0
    pts: list[Point] = []
    for k in range(per_cap + 1):  # cap around b, from the left of the axis to the right
        t = ang - math.pi / 2 + math.pi * k / per_cap
        pts.append((b[0] + r * math.cos(t), b[1] + r * math.sin(t)))
    for k in range(per_cap + 1):  # cap around a
        t = ang + math.pi / 2 + math.pi * k / per_cap
        pts.append((a[0] + r * math.cos(t), a[1] + r * math.sin(t)))
    return pts


def polygon_contains(poly: Sequence[Point], point: Point) -> bool:
    x, y = point
    inside = False
    n = len(poly)
    for i in range(n):
        (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
        if (ya <= y < yb) or (yb <= y < ya):
            if xa + (y - ya) * (xb - xa) / (yb - ya) > x:
                inside = not inside
    return inside


def distance_to_polyline(points: Sequence[Point], point: Point) -> float:
    """Shortest distance from `point` to the open polyline through `points`."""
    best = math.inf
    for a, b in zip(points, points[1:], strict=False):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / seg))
        best = min(best, math.dist(point, (a[0] + t * dx, a[1] + t * dy)))
    return best


def distance_to_polygon_edge(poly: Sequence[Point], point: Point) -> float:
    best = math.inf
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / seg))
        best = min(best, math.dist(point, (a[0] + t * dx, a[1] + t * dy)))
    return best


def simplify(points: Sequence[Point], *, min_gap: float, flat_deg: float = 1.0, closed: bool = False) -> list[Point]:
    """Drop repeated points, points closer than `min_gap` to the last kept one, and (almost) collinear points whose
    turn is below `flat_deg`. The first and last point are always kept. Never reduces a curve to a few long chords:
    a point is only dropped when both neighbours are within `min_gap` of it or the turn is negligible."""
    if len(points) < 3:
        return list(points)
    kept: list[Point] = [points[0]]
    for i in range(1, len(points) - 1):
        p = points[i]
        if math.dist(p, kept[-1]) < min_gap:
            continue
        a, b = kept[-1], points[i + 1]
        v1, v2 = (p[0] - a[0], p[1] - a[1]), (b[0] - p[0], b[1] - p[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 > 0 and n2 > 0:
            cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            if math.degrees(math.acos(cos)) < flat_deg and n1 + n2 < 6 * min_gap:
                continue
        kept.append(p)
    last = points[-1]
    if closed and math.dist(last, kept[0]) < min_gap:
        return kept
    kept.append(last)
    return kept


def polyline_length(points: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))


def max_chord_error(points: Sequence[Point], reference: Sequence[Point]) -> float:
    """Largest distance from any `reference` sample to the polyline `points` (how far a coarse path strays)."""
    worst = 0.0
    for r in reference:
        best = math.inf
        for a, b in zip(points, points[1:], strict=False):
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg = dx * dx + dy * dy
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((r[0] - a[0]) * dx + (r[1] - a[1]) * dy) / seg))
            best = min(best, math.dist(r, (a[0] + t * dx, a[1] + t * dy)))
        worst = max(worst, best)
    return worst
