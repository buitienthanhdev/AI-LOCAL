"""Entity normalisation and tolerance-based comparison for AutoCAD read-back verification.

Adapted from AutoCAD-MCP (https://github.com/beiming183-cloud/AutoCAD-MCP, commit 11f7c47, MIT,
(c) 2024 AutoCAD MCP Server Contributors): the per-type COM property read-back of `_com_entity_to_dict`
(`backends/file_ipc.py`) and the compare-requested-vs-actual idea of `verify_created_entity` (`backends/base.py`).

`entity_to_dict` only reads properties through attribute access, so it works with a live COM entity on the
AutoCAD STA thread and with plain fakes in unit tests. Points are returned as {"x", "y", "z"} dicts, the same
shape the tool contract accepts. Nothing here swallows errors: a property that cannot be read fails the read.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from lga_worker.errors import ErrorCode, ToolError

OBJECT_NAME_PREFIX = "acdb"
FULL_TURN_DEG = 360.0
XY_STRIDE = 2
XYZ_STRIDE = 3

LINE = "AcDbLine"
CIRCLE = "AcDbCircle"
ARC = "AcDbArc"
LWPOLYLINE = "AcDbPolyline"
POLYLINE_2D = "AcDb2dPolyline"
POLYLINE_3D = "AcDb3dPolyline"
TEXT = "AcDbText"
MTEXT = "AcDbMText"
BLOCK_REFERENCE = "AcDbBlockReference"

POINT_FIELDS = ("start", "end", "center", "insertion")
NUMBER_FIELDS = ("radius", "height")
ANGLE_FIELDS = ("rotation_deg",)


def point_dict(value: Sequence[float]) -> dict[str, float]:
    """A COM point (2 or 3 doubles) as {"x", "y", "z"}."""
    coords = [float(v) for v in value]
    if len(coords) not in (XY_STRIDE, XYZ_STRIDE):
        raise ValueError(f"a point needs 2 or 3 coordinates, got {len(coords)}")
    return {"x": coords[0], "y": coords[1], "z": coords[2] if len(coords) == XYZ_STRIDE else 0.0}


def expected_point(point: Mapping[str, Any]) -> dict[str, float]:
    return {"x": float(point["x"]), "y": float(point["y"]), "z": float(point.get("z", 0.0) or 0.0)}


def short_object_name(name: str) -> str:
    """'AcDbCircle' / 'circle' / 'CIRCLE' -> 'circle'."""
    lowered = name.strip().lower()
    return lowered.removeprefix(OBJECT_NAME_PREFIX)


def object_name_matches(wanted: str, actual: str) -> bool:
    return short_object_name(wanted) == short_object_name(actual)


def polyline_elevation(points: Sequence[Mapping[str, Any]]) -> float:
    """The single z shared by all points of a 2D lightweight polyline (its elevation)."""
    heights = {float(p.get("z", 0.0) or 0.0) for p in points}
    if len(heights) != 1:
        raise ToolError(ErrorCode.INVALID_ARGUMENT,
                        f"a lightweight polyline is planar: all points need the same z (got {sorted(heights)})")
    return heights.pop()


def _vertices(coordinates: Sequence[float], stride: int, elevation: float = 0.0) -> list[dict[str, float]]:
    flat = [float(v) for v in coordinates]
    if len(flat) % stride:
        raise ValueError(f"{len(flat)} coordinates are not a multiple of {stride}")
    if stride == XY_STRIDE:
        return [{"x": flat[i], "y": flat[i + 1], "z": elevation} for i in range(0, len(flat), stride)]
    return [{"x": flat[i], "y": flat[i + 1], "z": flat[i + 2]} for i in range(0, len(flat), stride)]


def entity_to_dict(entity: Any) -> dict[str, Any]:
    """Handle, object name, layer and the geometry fields of the supported entity types."""
    object_name = str(entity.ObjectName)
    result: dict[str, Any] = {"handle": str(entity.Handle), "object_name": object_name, "layer": str(entity.Layer)}
    if object_name == LINE:
        result.update(start=point_dict(entity.StartPoint), end=point_dict(entity.EndPoint), length=float(entity.Length))
    elif object_name == CIRCLE:
        result.update(center=point_dict(entity.Center), radius=float(entity.Radius))
    elif object_name == ARC:
        result.update(center=point_dict(entity.Center), radius=float(entity.Radius),
                      start_angle_deg=math.degrees(float(entity.StartAngle)),
                      end_angle_deg=math.degrees(float(entity.EndAngle)),
                      start=point_dict(entity.StartPoint), end=point_dict(entity.EndPoint))
    elif object_name == LWPOLYLINE:
        points = _vertices(entity.Coordinates, XY_STRIDE, float(entity.Elevation))
        result.update(points=points, closed=bool(entity.Closed), vertex_count=len(points))
    elif object_name in (POLYLINE_2D, POLYLINE_3D):
        points = _vertices(entity.Coordinates, XYZ_STRIDE)
        result.update(points=points, closed=bool(entity.Closed), vertex_count=len(points))
    elif object_name in (TEXT, MTEXT):
        result.update(insertion=point_dict(entity.InsertionPoint), text=str(entity.TextString),
                      height=float(entity.Height), rotation_deg=math.degrees(float(entity.Rotation)))
    elif object_name == BLOCK_REFERENCE:
        result.update(insertion=point_dict(entity.InsertionPoint), block_name=str(entity.EffectiveName),
                      rotation_deg=math.degrees(float(entity.Rotation)))
    return result


def close(actual: float, expected: float, tolerance: float) -> bool:
    """|actual - expected| within `tolerance`, scaled for magnitudes above 1 (doubles keep relative precision)."""
    return abs(float(actual) - float(expected)) <= tolerance * max(1.0, abs(float(expected)))


def angle_close(actual_deg: float, expected_deg: float, tolerance: float) -> bool:
    diff = (float(actual_deg) - float(expected_deg)) % FULL_TURN_DEG
    return min(diff, FULL_TURN_DEG - diff) <= tolerance * max(1.0, abs(float(expected_deg)))


def points_close(actual: Mapping[str, float], expected: Mapping[str, Any], tolerance: float) -> bool:
    want = expected_point(expected)
    return all(close(actual[axis], want[axis], tolerance) for axis in ("x", "y", "z"))


def _mismatch(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"field": field, "expected": expected, "actual": actual}


def compare(expect: Mapping[str, Any], actual: Mapping[str, Any], tolerance: float) -> list[dict[str, Any]]:
    """Every expected field that the actual entity does not satisfy. Empty list = match."""
    mismatches: list[dict[str, Any]] = []
    for field, wanted in expect.items():
        have = actual.get(field)
        if field == "object_name":
            ok = have is not None and object_name_matches(str(wanted), str(have))
        elif field == "layer":
            ok = have is not None and str(have).casefold() == str(wanted).casefold()
        elif field in POINT_FIELDS:
            ok = have is not None and points_close(have, wanted, tolerance)
            wanted = expected_point(wanted)
        elif field in NUMBER_FIELDS:
            ok = have is not None and close(have, wanted, tolerance)
        elif field in ANGLE_FIELDS:
            ok = have is not None and angle_close(have, wanted, tolerance)
        elif field == "points":
            wanted = [expected_point(p) for p in wanted]
            ok = (have is not None and len(have) == len(wanted)
                  and all(points_close(a, w, tolerance) for a, w in zip(have, wanted, strict=True)))
        elif field == "closed":
            ok = have is not None and bool(have) == bool(wanted)
        elif field == "vertex_count":
            ok = have is not None and int(have) == int(wanted)
        elif field == "text":
            ok = have is not None and str(have) == str(wanted)
        else:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"cannot compare unknown field {field!r}")
        if not ok:
            mismatches.append(_mismatch(field, wanted, have))
    return mismatches


def describe_mismatches(mismatches: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{m['field']}: expected {m['expected']!r}, actual {m['actual']!r}" for m in mismatches)
