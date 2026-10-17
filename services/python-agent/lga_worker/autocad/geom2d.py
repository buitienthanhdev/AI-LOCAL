"""Small 2D geometry helpers for the validation engine (pure Python, tolerance passed in every call).

Geometry in the intermediate model (plain dicts, drawing units):
  {"type": "line", "start": [x, y], "end": [x, y]}
  {"type": "polyline", "points": [[x, y], ...], "closed": bool}
  {"type": "segments", "segments": [[[x, y], [x, y]], ...]}      (a boundary given as loose lines)
  {"type": "arc", "center": [x, y], "radius": r, "start_angle": deg, "end_angle": deg}
  {"type": "circle", "center": [x, y], "radius": r}
  {"type": "point", "point": [x, y]}
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

Point = tuple[float, float]


def pt(raw: Sequence[float]) -> Point:
    return float(raw[0]), float(raw[1])


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def geometry_segments(geometry: Mapping[str, Any] | None) -> list[tuple[Point, Point]]:
    """Straight segments of a line / polyline (closing segment included when closed) / segments geometry."""
    if not geometry:
        return []
    kind = geometry.get("type")
    if kind == "line":
        return [(pt(geometry["start"]), pt(geometry["end"]))]
    if kind == "polyline":
        points = [pt(p) for p in geometry.get("points", [])]
        segments = [(points[i], points[i + 1]) for i in range(len(points) - 1)]
        if geometry.get("closed") and len(points) > 2:
            segments.append((points[-1], points[0]))
        return segments
    if kind == "segments":
        return [(pt(s[0]), pt(s[1])) for s in geometry.get("segments", [])]
    return []


def polygon_points(geometry: Mapping[str, Any] | None) -> list[Point]:
    """Vertices of a closed polyline (closing duplicate vertex dropped); [] when the geometry is not one."""
    if not geometry or geometry.get("type") != "polyline":
        return []
    points = [pt(p) for p in geometry.get("points", [])]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return points


def polygon_area(points: Sequence[Point]) -> float:
    """Absolute shoelace area."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def centroid(points: Sequence[Point]) -> Point:
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(s1: tuple[Point, Point], s2: tuple[Point, Point], tol: float) -> bool:
    """True when the segments intersect at a point that is interior to both (a crossing), beyond `tol`."""
    a, b = s1
    c, d = s2
    len1, len2 = dist(a, b), dist(c, d)
    if len1 <= tol or len2 <= tol:
        return False
    # distances of c,d from line ab and of a,b from line cd (signed, in length units)
    d1, d2 = _orient(a, b, c) / len1, _orient(a, b, d) / len1
    d3, d4 = _orient(c, d, a) / len2, _orient(c, d, b) / len2
    if (d1 > tol and d2 < -tol or d1 < -tol and d2 > tol) and (d3 > tol and d4 < -tol or d3 < -tol and d4 > tol):
        return True
    return False


def segments_overlap(s1: tuple[Point, Point], s2: tuple[Point, Point], tol: float) -> bool:
    """Collinear segments sharing a stretch longer than `tol` (non-adjacent overlap of a polyline with itself)."""
    a, b = s1
    c, d = s2
    len1 = dist(a, b)
    if len1 <= tol or dist(c, d) <= tol:
        return False
    if abs(_orient(a, b, c)) / len1 > tol or abs(_orient(a, b, d)) / len1 > tol:
        return False
    ux, uy = (b[0] - a[0]) / len1, (b[1] - a[1]) / len1
    t_c = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    t_d = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(0.0, min(t_c, t_d)), min(len1, max(t_c, t_d))
    return hi - lo > tol


def self_intersections(geometry: Mapping[str, Any], tol: float) -> list[tuple[int, int]]:
    """Index pairs of non-adjacent segments of one polyline that cross or overlap."""
    segments = geometry_segments(geometry)
    count = len(segments)
    closed = bool(geometry.get("closed")) or geometry.get("type") == "segments"
    found: list[tuple[int, int]] = []
    for i in range(count):
        for j in range(i + 2, count):
            if closed and i == 0 and j == count - 1:
                continue  # first and last segment of a closed ring are neighbours
            if segments_cross(segments[i], segments[j], tol) or segments_overlap(segments[i], segments[j], tol):
                found.append((i, j))
    return found


def point_in_polygon(point: Point, polygon: Sequence[Point], tol: float) -> bool:
    """Strictly inside (points within `tol` of the boundary are NOT inside)."""
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        if point_segment_distance(point, a, b) <= tol:
            return False
    inside = False
    x, y = point
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    length_sq = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if length_sq == 0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / length_sq))
    return dist(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def polygons_overlap(p1: Sequence[Point], p2: Sequence[Point], tol: float) -> bool:
    """Interiors overlap: boundaries properly cross, or a vertex/centroid of one lies strictly inside the other."""
    if len(p1) < 3 or len(p2) < 3:
        return False
    segs1 = [(p1[i], p1[(i + 1) % len(p1)]) for i in range(len(p1))]
    segs2 = [(p2[i], p2[(i + 1) % len(p2)]) for i in range(len(p2))]
    if any(segments_cross(a, b, tol) for a in segs1 for b in segs2):
        return True
    if any(point_in_polygon(v, p2, tol) for v in p1) or any(point_in_polygon(v, p1, tol) for v in p2):
        return True
    return point_in_polygon(centroid(p1), p2, tol) or point_in_polygon(centroid(p2), p1, tol)


def chain_closed(segments: Sequence[tuple[Point, Point]], tol: float) -> tuple[bool, str]:
    """Do loose segments form one closed ring? Endpoints closer than `tol` are the same node."""
    if len(segments) < 3:
        return False, f"only {len(segments)} segment(s): a boundary needs at least 3"
    nodes: list[Point] = []

    def node(p: Point) -> int:
        for i, q in enumerate(nodes):
            if dist(p, q) <= tol:
                return i
        nodes.append(p)
        return len(nodes) - 1

    degree: dict[int, int] = {}
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in segments:
        ia, ib = node(a), node(b)
        degree[ia] = degree.get(ia, 0) + 1
        degree[ib] = degree.get(ib, 0) + 1
        parent[find(ia)] = find(ib)
    loose = sorted(i for i, d in degree.items() if d != 2)
    if loose:
        gaps = ", ".join(f"({nodes[i][0]:g},{nodes[i][1]:g}) degree {degree[i]}" for i in loose[:4])
        return False, f"open boundary: loose or branching endpoint(s) {gaps}"
    if len({find(i) for i in degree}) != 1:
        return False, "segments form more than one separate ring"
    return True, "closed ring"


def ring_from_segments(segments: Sequence[tuple[Point, Point]], tol: float) -> list[Point] | None:
    """Vertices of a closed ring made of loose segments, in walking order (None if they do not form one ring)."""
    ok, _ = chain_closed(segments, tol)
    if not ok:
        return None
    remaining = list(segments)
    first = remaining.pop(0)
    ring = [first[0]]
    current = first[1]
    while remaining:
        for i, (a, b) in enumerate(remaining):
            if dist(a, current) <= tol:
                ring.append(a)
                current = b
                remaining.pop(i)
                break
            if dist(b, current) <= tol:
                ring.append(b)
                current = a
                remaining.pop(i)
                break
        else:
            return None
    return ring
