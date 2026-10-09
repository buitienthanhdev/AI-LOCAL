"""Floor plan layout (pure geometry, no AutoCAD needed)."""

from __future__ import annotations

import math

import pytest

from lga_worker.autocad import floorplan
from lga_worker.autocad.floorplan import opening_shapes, plan_house

SIZES = [dict(width=20, depth=20), dict(width=5, depth=20), dict(area=100), dict(width=10, depth=25, bedrooms=3),
         dict(width=12, depth=18), dict(width=8, depth=30, bedrooms=2), dict(width=30, depth=25, bedrooms=6)]


def overlap(a: floorplan.Room, b: floorplan.Room) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return w * h if w > 1e-6 and h > 1e-6 else 0.0


@pytest.mark.parametrize("size", SIZES)
def test_rooms_tile_the_footprint(size: dict) -> None:
    plan = plan_house(**size)
    for room in plan.rooms:
        assert 0 <= room.x0 < room.x1 <= plan.width + 1e-6, room
        assert plan.porch <= room.y0 < room.y1 <= plan.depth + 1e-6, room
    for i, a in enumerate(plan.rooms):
        for b in plan.rooms[i + 1:]:
            assert overlap(a, b) == 0.0, (a.name, b.name)
    covered = sum(r.area for r in plan.rooms) + plan.width * plan.porch
    assert math.isclose(covered, plan.area, rel_tol=1e-6)


@pytest.mark.parametrize("size", SIZES)
def test_every_room_is_reachable_and_lit(size: dict) -> None:
    plan = plan_house(**size)

    def on_edge(room: floorplan.Room, op: floorplan.Opening) -> bool:
        if op.orient == "v":
            return (abs(op.line - room.x0) < 1e-6 or abs(op.line - room.x1) < 1e-6) and room.y0 - 1e-6 <= op.a and op.b <= room.y1 + 1e-6
        return (abs(op.line - room.y0) < 1e-6 or abs(op.line - room.y1) < 1e-6) and room.x0 - 1e-6 <= op.a and op.b <= room.x1 + 1e-6

    for room in plan.rooms:
        if room.kind in ("corridor", "annex"):
            continue
        kinds = {op.kind for op in plan.openings if on_edge(room, op)}
        assert kinds & {"door", "double", "open"}, f"{room.name} has no way in"
    for room in plan.rooms:
        if room.kind == "bedroom":
            annex = [r for r in plan.rooms if r.kind == "annex" and r.name == room.name]
            parts = [room, *annex]
            assert any(op.kind == "window" and on_edge(p, op) for p in parts for op in plan.openings), f"{room.name} has no window"


def test_walls_leave_the_openings_free() -> None:
    plan = plan_house(width=20, depth=20)
    for op in plan.openings:
        mid = (op.a + op.b) / 2
        x, y = (op.line, mid) if op.orient == "v" else (mid, op.line)
        inside = [w for w in plan.walls if w[0] + 1e-6 < x < w[2] - 1e-6 and w[1] + 1e-6 < y < w[3] - 1e-6]
        assert not inside, (op, inside)
    assert plan.wall_thickness[("v", 0.0)] == floorplan.EXTERIOR_WALL


def test_door_arcs_sweep_a_quarter_turn_towards_the_room() -> None:
    door = floorplan.Opening("v", 5.0, 2.0, 2.9, "door", swing=1, hinge="a")
    leaf, arc = opening_shapes(door, 0.1)
    assert leaf.values == pytest.approx((5.0, 2.0, 5.9, 2.0))
    cx, cy, r, start, end = arc.values
    assert (cx, cy, r) == pytest.approx((5.0, 2.0, 0.9))
    assert math.isclose((end - start) % (2 * math.pi), math.pi / 2)
    double = floorplan.Opening("h", 2.0, 4.3, 5.7, "double", swing=1)
    assert len(opening_shapes(double, 0.2)) == 4
    assert len(opening_shapes(floorplan.Opening("h", 2.0, 1.0, 2.6, "window"), 0.2)) == 5


def test_size_resolution_and_limits() -> None:
    assert floorplan.resolve_size(None, None, 400) == (20.0, 20.0)
    assert floorplan.resolve_size(8, None, 160) == (8.0, 20.0)
    with pytest.raises(ValueError):
        floorplan.resolve_size(3, 10, None)
    with pytest.raises(ValueError):
        floorplan.resolve_size(None, None, None)


def test_program_follows_the_request() -> None:
    plan = plan_house(width=20, depth=20, bedrooms=3, altar=False)
    names = [r.name for r in plan.rooms]
    assert sum(1 for r in plan.rooms if r.kind == "bedroom") == 3
    assert "Phòng thờ" not in names
    assert {"Phòng khách", "Bếp + Phòng ăn", "WC chung"} <= set(names)
    assert "BẢNG DIỆN TÍCH" in plan.schedule.text
