"""SVG -> Scene.

The planner can describe any picture as SVG (language models write it well), and every drawing backend then
draws it. Supported: path (M L H V C S Q T A Z, relative forms, implicit repeats), rect (with rx/ry), circle,
ellipse, line, polyline, polygon, g, use of simple defs is NOT followed; transform (matrix translate scale rotate
skewX skewY), fill / stroke / stroke-width / opacity / fill-opacity as attributes or in style="", colours as hex,
rgb(), hsl() or CSS names, and gradients (reduced to the mean colour of their stops). Text, filters, masks,
clip-paths and images are skipped and reported in `Scene.notes`.

Curves are flattened to polylines; the result is fitted into the sheet (aspect kept, centred, small margin).
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from PIL import ImageColor

from lga_worker.art.scene import MAX_SHAPES, MAX_VERTICES, RGB, SHEET_HEIGHT, SHEET_WIDTH, Point, Scene, Shape
from lga_worker.errors import ErrorCode, ToolError

FILL_BELOW = 0.6  # the subject spans less than this share of the frame: it is enlarged
FILL_TARGET = 0.92
MARGIN = 0.0  # the picture fills the sheet; each backend adds the margin it needs
FLATTEN_STEP = 3.0  # sheet units between flattened curve points (at the final scale)
MAX_CURVE_STEPS = 64
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
SKIPPED = {"text", "tspan", "image", "filter", "mask", "clipPath", "foreignObject", "pattern", "marker", "symbol", "style",
           "script", "title", "desc", "metadata"}
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """m applied after n."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _apply(m: Matrix, x: float, y: float) -> Point:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _parse_transform(text: str | None) -> Matrix:
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in _NUMBER.findall(args)]
        if name == "matrix" and len(v) == 6:
            t: Matrix = (v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            r: Matrix = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) >= 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), r), (1, 0, 0, 1, -v[1], -v[2]))
            else:
                t = r
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mul(m, t)
    return m


def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _length(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    found = _NUMBER.search(value)
    return float(found.group()) if found else default


class _Painter:
    """Resolved presentation attributes of one element (inherited down the tree)."""

    def __init__(self, fill: str | None = "black", stroke: str | None = None, stroke_width: float = 1.0,
                 opacity: float = 1.0, fill_opacity: float = 1.0, stroke_opacity: float = 1.0) -> None:
        self.fill, self.stroke, self.stroke_width = fill, stroke, stroke_width
        self.opacity, self.fill_opacity, self.stroke_opacity = opacity, fill_opacity, stroke_opacity

    def child(self, element: ET.Element) -> _Painter:
        attrs = dict(element.attrib)
        for decl in (attrs.get("style") or "").split(";"):
            if ":" in decl:
                key, value = decl.split(":", 1)
                attrs[key.strip()] = value.strip()
        p = _Painter(self.fill, self.stroke, self.stroke_width, self.opacity, self.fill_opacity, self.stroke_opacity)
        if "fill" in attrs:
            p.fill = attrs["fill"].strip()
        if "stroke" in attrs:
            p.stroke = attrs["stroke"].strip()
        if "stroke-width" in attrs:
            p.stroke_width = _length(attrs["stroke-width"], self.stroke_width)
        for key, field_name in (("opacity", "opacity"), ("fill-opacity", "fill_opacity"), ("stroke-opacity", "stroke_opacity")):
            if key in attrs:
                try:
                    setattr(p, field_name, max(0.0, min(1.0, float(attrs[key]))))
                except ValueError:
                    pass
        return p


class _Parser:
    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self.ids = {el.attrib["id"]: el for el in root.iter() if "id" in el.attrib}
        self.shapes: list[Shape] = []
        self.notes: list[str] = []
        self.skipped: set[str] = set()
        self._step = FLATTEN_STEP

    # ------------------------------------------------------------------ colours
    def _gradient_color(self, ref: str, depth: int = 0) -> RGB | None:
        element = self.ids.get(ref)
        if element is None or depth > 4:
            return None
        stops = [s for s in element.iter() if _strip(s.tag) == "stop"]
        if not stops:
            href = element.attrib.get("{http://www.w3.org/1999/xlink}href") or element.attrib.get("href")
            return self._gradient_color(href[1:], depth + 1) if href and href.startswith("#") else None
        colours: list[RGB] = []
        for stop in stops:
            style = dict(d.split(":", 1) for d in (stop.attrib.get("style") or "").split(";") if ":" in d)
            value = (style.get("stop-color") or stop.attrib.get("stop-color") or "black").strip()
            rgb = self._color(value)
            if rgb is not None:
                colours.append(rgb)
        if not colours:
            return None
        return tuple(round(sum(c[i] for c in colours) / len(colours)) for i in range(3))  # type: ignore[return-value]

    def _color(self, value: str | None) -> RGB | None:
        if value is None:
            return None
        text = value.strip()
        low = text.lower()
        if low in ("none", "transparent", ""):
            return None
        if low.startswith("url("):
            ref = re.search(r"url\(\s*['\"]?#([^'\")\s]+)", text)
            return self._gradient_color(ref.group(1)) if ref else None
        if low == "currentcolor":
            return (0, 0, 0)
        try:
            r, g, b = ImageColor.getrgb(text)[:3]
            return int(r), int(g), int(b)
        except ValueError:
            self.notes.append(f"colour {value!r} not understood (skipped)")
            return None

    @staticmethod
    def _blend(rgb: RGB, opacity: float) -> RGB:
        if opacity >= 0.999:
            return rgb
        return tuple(round(255 + (c - 255) * opacity) for c in rgb)  # type: ignore[return-value]

    # ------------------------------------------------------------------ geometry
    def _emit(self, rings: Iterable[tuple[list[Point], bool]], painter: _Painter, matrix: Matrix,
              hint: str | None = None) -> None:
        fill = self._color(painter.fill)
        stroke = self._color(painter.stroke)
        if fill is not None:
            fill = self._blend(fill, painter.opacity * painter.fill_opacity)
        if stroke is not None:
            stroke = self._blend(stroke, painter.opacity * painter.stroke_opacity)
        scale = math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])) or 1.0
        width = max(painter.stroke_width * scale, 0.0)
        for points, closed in rings:
            pts = [_apply(matrix, x, y) for x, y in points]
            if len(pts) < 2 or (fill is None and stroke is None):
                continue
            self.shapes.append(Shape(tuple(pts), closed=closed, fill=fill if (closed or len(pts) > 2) else None,
                                     stroke=stroke, width=width, hint=hint))

    def _flatten_cubic(self, p0: Point, p1: Point, p2: Point, p3: Point) -> list[Point]:
        chord = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
        n = max(4, min(MAX_CURVE_STEPS, int(chord / self._step) + 1))
        out: list[Point] = []
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                        u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
        return out

    def _flatten_arc(self, p0: Point, rx: float, ry: float, phi_deg: float, large: bool, sweep: bool, p1: Point) -> list[Point]:
        if rx == 0 or ry == 0 or p0 == p1:
            return [p1]
        rx, ry = abs(rx), abs(ry)
        phi = math.radians(phi_deg)
        cp, sp = math.cos(phi), math.sin(phi)
        dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
        x1 = cp * dx + sp * dy
        y1 = -sp * dx + cp * dy
        lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
        if lam > 1:
            s = math.sqrt(lam)
            rx, ry = rx * s, ry * s
        num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
        den = rx * rx * y1 * y1 + ry * ry * x1 * x1
        coef = math.sqrt(max(0.0, num / den)) if den else 0.0
        if large == sweep:
            coef = -coef
        cxp, cyp = coef * rx * y1 / ry, -coef * ry * x1 / rx
        cx = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2
        cy = sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2

        def angle(ux: float, uy: float, vx: float, vy: float) -> float:
            a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
            return a

        th1 = angle(1, 0, (x1 - cxp) / rx, (y1 - cyp) / ry)
        dth = angle((x1 - cxp) / rx, (y1 - cyp) / ry, (-x1 - cxp) / rx, (-y1 - cyp) / ry)
        if not sweep and dth > 0:
            dth -= 2 * math.pi
        elif sweep and dth < 0:
            dth += 2 * math.pi
        arc_len = abs(dth) * max(rx, ry)
        n = max(6, min(MAX_CURVE_STEPS, int(arc_len / self._step) + 1))
        out: list[Point] = []
        for i in range(1, n + 1):
            a = th1 + dth * i / n
            x = rx * math.cos(a)
            y = ry * math.sin(a)
            out.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
        out[-1] = p1
        return out

    def _path(self, d: str) -> list[tuple[list[Point], bool]]:
        tokens = [(m.group(1), m.group(2)) for m in _TOKEN.finditer(d)]
        rings: list[tuple[list[Point], bool]] = []
        current: list[Point] = []
        pos: Point = (0.0, 0.0)
        start: Point = (0.0, 0.0)
        last_cubic: Point | None = None
        last_quad: Point | None = None
        cmd = ""
        i = 0

        def numbers(count: int) -> list[float] | None:
            nonlocal i
            if i + count > len(tokens) or any(tokens[i + k][1] is None for k in range(count)):
                return None
            vals = [float(tokens[i + k][1]) for k in range(count)]  # type: ignore[arg-type]
            i += count
            return vals

        def flush(closed: bool) -> None:
            nonlocal current
            if len(current) >= 2:
                rings.append((current, closed))
            current = []

        while i < len(tokens):
            letter, number = tokens[i]
            if letter:
                cmd = letter
                i += 1
                if cmd in "Zz":
                    if current:
                        flush(True)
                    pos = start
                    last_cubic = last_quad = None
                    continue
            elif not cmd:
                i += 1
                continue
            rel = cmd.islower()
            c = cmd.upper()
            ox, oy = pos if rel else (0.0, 0.0)
            if c == "M":
                v = numbers(2)
                if v is None:
                    break
                if current:
                    flush(False)
                pos = (ox + v[0], oy + v[1])
                start = pos
                current = [pos]
                cmd = "l" if rel else "L"  # extra pairs are implicit lineto
                last_cubic = last_quad = None
            elif c == "L":
                v = numbers(2)
                if v is None:
                    break
                pos = (ox + v[0], oy + v[1])
                current.append(pos)
                last_cubic = last_quad = None
            elif c == "H":
                v = numbers(1)
                if v is None:
                    break
                pos = (ox + v[0], pos[1])
                current.append(pos)
                last_cubic = last_quad = None
            elif c == "V":
                v = numbers(1)
                if v is None:
                    break
                pos = (pos[0], oy + v[0])
                current.append(pos)
                last_cubic = last_quad = None
            elif c == "C":
                v = numbers(6)
                if v is None:
                    break
                p1, p2, p3 = (ox + v[0], oy + v[1]), (ox + v[2], oy + v[3]), (ox + v[4], oy + v[5])
                current.extend(self._flatten_cubic(pos, p1, p2, p3))
                last_cubic, last_quad, pos = p2, None, p3
            elif c == "S":
                v = numbers(4)
                if v is None:
                    break
                p1 = (2 * pos[0] - last_cubic[0], 2 * pos[1] - last_cubic[1]) if last_cubic else pos
                p2, p3 = (ox + v[0], oy + v[1]), (ox + v[2], oy + v[3])
                current.extend(self._flatten_cubic(pos, p1, p2, p3))
                last_cubic, last_quad, pos = p2, None, p3
            elif c == "Q":
                v = numbers(4)
                if v is None:
                    break
                q, p3 = (ox + v[0], oy + v[1]), (ox + v[2], oy + v[3])
                p1 = (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1]))
                p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
                current.extend(self._flatten_cubic(pos, p1, p2, p3))
                last_quad, last_cubic, pos = q, None, p3
            elif c == "T":
                v = numbers(2)
                if v is None:
                    break
                q = (2 * pos[0] - last_quad[0], 2 * pos[1] - last_quad[1]) if last_quad else pos
                p3 = (ox + v[0], oy + v[1])
                p1 = (pos[0] + 2 / 3 * (q[0] - pos[0]), pos[1] + 2 / 3 * (q[1] - pos[1]))
                p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
                current.extend(self._flatten_cubic(pos, p1, p2, p3))
                last_quad, last_cubic, pos = q, None, p3
            elif c == "A":
                v = numbers(7)
                if v is None:
                    break
                p3 = (ox + v[5], oy + v[6])
                current.extend(self._flatten_arc(pos, v[0], v[1], v[2], v[3] != 0, v[4] != 0, p3))
                last_cubic = last_quad = None
                pos = p3
            else:
                i += 1
        if current:
            flush(False)
        return rings

    @staticmethod
    def _ellipse_ring(cx: float, cy: float, rx: float, ry: float, steps: int) -> list[Point]:
        return [(cx + rx * math.cos(2 * math.pi * k / steps), cy + ry * math.sin(2 * math.pi * k / steps)) for k in range(steps)]

    def _shape_rings(self, element: ET.Element, tag: str) -> list[tuple[list[Point], bool]]:
        a = element.attrib
        n = lambda key, default=0.0: _length(a.get(key), default)  # noqa: E731
        if tag == "path":
            return self._path(a.get("d", ""))
        if tag == "rect":
            x, y, w, h = n("x"), n("y"), n("width"), n("height")
            if w <= 0 or h <= 0:
                return []
            rx, ry = n("rx", -1), n("ry", -1)
            rx = ry if rx < 0 <= ry else rx
            ry = rx if ry < 0 <= rx else ry
            rx, ry = max(0.0, min(rx, w / 2)), max(0.0, min(ry, h / 2))
            if rx <= 0 or ry <= 0:
                return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
            ring: list[Point] = []
            for cx, cy, a0 in ((x + w - rx, y + ry, -90), (x + w - rx, y + h - ry, 0), (x + rx, y + h - ry, 90), (x + rx, y + ry, 180)):
                for k in range(0, 10):
                    ang = math.radians(a0 + 90 * k / 9)
                    ring.append((cx + rx * math.cos(ang), cy + ry * math.sin(ang)))
            return [(ring, True)]
        if tag == "circle":
            r = n("r")
            return [(self._ellipse_ring(n("cx"), n("cy"), r, r, 48), True)] if r > 0 else []
        if tag == "ellipse":
            rx, ry = n("rx"), n("ry")
            return [(self._ellipse_ring(n("cx"), n("cy"), rx, ry, 48), True)] if rx > 0 and ry > 0 else []
        if tag == "line":
            return [([(n("x1"), n("y1")), (n("x2"), n("y2"))], False)]
        if tag in ("polyline", "polygon"):
            v = [float(x) for x in _NUMBER.findall(a.get("points", ""))]
            pts = [(v[k], v[k + 1]) for k in range(0, len(v) - 1, 2)]
            return [(pts, tag == "polygon")] if len(pts) >= 2 else []
        return []

    def walk(self, element: ET.Element, painter: _Painter, matrix: Matrix) -> None:
        tag = _strip(element.tag)
        if tag in SKIPPED or tag in ("defs", "linearGradient", "radialGradient"):
            if tag in SKIPPED and tag not in ("style", "script", "title", "desc", "metadata"):
                self.skipped.add(tag)
            return
        if element.attrib.get("display") == "none" or element.attrib.get("visibility") == "hidden":
            return
        own = painter.child(element)
        m = _mul(matrix, _parse_transform(element.attrib.get("transform")))
        if tag in ("svg", "g", "a", "switch"):
            for child in element:
                self.walk(child, own, m)
            return
        if tag == "use":
            self.skipped.add("use")
            return
        rings = self._shape_rings(element, tag)
        if rings:
            upright = abs(m[1]) < 1e-9 and abs(m[2]) < 1e-9 and m[0] > 0 and m[3] > 0
            plain_rect = tag == "rect" and _length(element.attrib.get("rx")) == 0 and _length(element.attrib.get("ry")) == 0
            hint = "rect" if plain_rect else "ellipse" if tag in ("circle", "ellipse") else "line" if tag == "line" else None
            self._emit(rings, own, m, hint if upright else None)
        elif tag not in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
            self.skipped.add(tag)


def _clip_to_frame(shapes: list[Shape], frame: tuple[float, float, float, float]) -> list[Shape]:
    """An SVG shows only what is inside its frame: shapes that stick out are cut at the frame (a cut filled shape
    keeps its outline only along its own edges, not along the cut)."""
    from shapely.geometry import LineString, Polygon, box

    x0, y0, x1, y1 = frame
    window = box(x0, y0, x1, y1)
    eps = 1e-6
    out: list[Shape] = []

    def pieces(geom: object) -> list[list[Point]]:
        gtype = getattr(geom, "geom_type", "")
        if gtype == "LineString":
            return [list(geom.coords)]  # type: ignore[attr-defined]
        if gtype == "Polygon":
            return [list(geom.exterior.coords)[:-1]]  # type: ignore[attr-defined]
        if gtype in ("MultiLineString", "MultiPolygon", "GeometryCollection"):
            return [piece for g in geom.geoms for piece in pieces(g)]  # type: ignore[attr-defined]
        return []

    for s in shapes:
        bx0, by0, bx1, by1 = s.bounds()
        if bx0 >= x0 - eps and by0 >= y0 - eps and bx1 <= x1 + eps and by1 <= y1 + eps:
            out.append(s)
            continue
        if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
            continue
        if s.closed and len(s.points) >= 3:
            poly = Polygon(s.points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if s.fill is not None:
                for ring in pieces(poly.intersection(window)):
                    if len(ring) >= 3:
                        out.append(Shape(tuple(ring), True, s.fill, None, s.width, None))
            if s.stroke is not None:
                for line in pieces(LineString(list(s.points) + [s.points[0]]).intersection(window)):
                    if len(line) >= 2:
                        out.append(Shape(tuple(line), False, None, s.stroke, s.width, None))
        elif s.stroke is not None and len(s.points) >= 2:
            for line in pieces(LineString(s.points).intersection(window)):
                if len(line) >= 2:
                    out.append(Shape(tuple(line), False, None, s.stroke, s.width, None))
    return out


def parse_svg(text: str, *, width: float = SHEET_WIDTH, height: float = SHEET_HEIGHT,
              margin: float = MARGIN, fit_content: bool = True) -> Scene:
    """Parse SVG source and fit it into the sheet. Raises ToolError(INVALID_ARGUMENT) with the reason."""
    source = text.strip()
    if "<svg" not in source:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, "svg must be a complete <svg ...>...</svg> document")
    source = re.sub(r"<!DOCTYPE[^>]*>", "", source)
    if "<!ENTITY" in source:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, "svg with entity declarations is not accepted")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"svg is not well-formed XML: {exc}") from exc
    # the viewBox (or width/height) is the drawing's own coordinate frame
    vb = [float(x) for x in _NUMBER.findall(root.attrib.get("viewBox", ""))]
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        vx, vy, vw, vh = vb
    else:
        vx = vy = 0.0
        vw = _length(root.attrib.get("width"), 0.0) or 1000.0
        vh = _length(root.attrib.get("height"), 0.0) or 562.5
    parser = _Parser(root)
    # Parse at the drawing's own scale, flatten with steps proportional to its size.
    parser._step = max(0.5, FLATTEN_STEP * vw / width)
    parser.walk(root, _Painter(), IDENTITY)
    shapes = parser.shapes
    notes = list(dict.fromkeys(parser.notes))
    if parser.skipped:
        notes.append("skipped (not drawable as shapes): " + ", ".join(sorted(parser.skipped)))
    if not shapes:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        "the svg contains nothing that can be drawn (need path/rect/circle/ellipse/polygon/line elements "
                        "with a fill or stroke)" + (f"; {notes[-1]}" if notes else ""))
    if len(shapes) > MAX_SHAPES or sum(len(s.points) for s in shapes) > MAX_VERTICES:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"svg is too big ({len(shapes)} shapes); at most {MAX_SHAPES} shapes")
    k = min(width * (1 - 2 * margin) / vw, height * (1 - 2 * margin) / vh)
    ox = (width - vw * k) / 2 - vx * k
    oy = (height - vh * k) / 2 - vy * k
    fitted = [Shape(tuple((x * k + ox, y * k + oy) for x, y in s.points), s.closed, s.fill, s.stroke, max(0.3, s.width * k),
                    s.hint) for s in shapes]
    frame = (ox, oy, ox + vw * k, oy + vh * k)
    fitted = _clip_to_frame(fitted, frame)
    scene = Scene(fitted, width=width, height=height, notes=notes)
    # A filled rectangle that covers the whole drawing frame first is the picture's background: it becomes the
    # sheet's background (a backend paints it as paper, not as a shape to trace).
    first = fitted[0]
    if first.fill is not None and first.closed and len(first.points) == 4:
        x0, y0, x1, y1 = first.bounds()
        if (x1 - x0) >= vw * k * 0.98 and (y1 - y0) >= vh * k * 0.98 and len(fitted) > 1:
            scene.background = first.fill
            scene.shapes = fitted[1:]
    if fit_content:
        _fill_frame(scene, frame)
    return scene


def _fill_frame(scene: Scene, frame: tuple[float, float, float, float]) -> None:
    """A subject drawn small in a corner of the frame is enlarged and centred so it uses the sheet (the picture
    keeps its proportions); a picture that already spans the frame is left alone."""
    shapes = scene.shapes
    if not shapes:
        return
    boxes = [s.bounds() for s in shapes]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    fx0, fy0, fx1, fy1 = frame
    w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
    frac = max(w / (fx1 - fx0), h / (fy1 - fy0))
    if frac >= FILL_BELOW:
        return
    k = min(FILL_TARGET * (fx1 - fx0) / w, FILL_TARGET * (fy1 - fy0) / h)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tx, ty = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    scene.shapes = [Shape(tuple((tx + (x - cx) * k, ty + (y - cy) * k) for x, y in s.points), s.closed, s.fill, s.stroke,
                          s.width * k, s.hint) for s in shapes]
    scene.notes.append(f"the subject covered only {frac:.0%} of the frame: enlarged x{k:.1f} and centred")
