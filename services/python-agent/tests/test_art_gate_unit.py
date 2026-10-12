"""Artwork specification gate: building, validation, state machine, execution gate (the 20 required cases)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from lga_worker.art import brief as brief_mod
from lga_worker.art import gate as gate_mod
from lga_worker.art import spec as spec_mod
from lga_worker.art.gate import ExecutionGate, GateClosed, Level, State, run_gated, validate_spec
from lga_worker.art.knowledge import ArtKnowledgeStore
from lga_worker.art.modes import Mode
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import engine, geometry
from lga_worker.paint.driver import PaintDriver
from lga_worker.paint.model import Canvas, DrawingObject
from lga_worker.paint.templates import build_template

CANVAS = Canvas(800, 450)
CAPS = {"tools": ["Rectangle", "Oval", "Triangle", "Polygon", "Line", "Pencil", "Fill", "Brushes"],
        "brushes": ["Brush", "Oil brush", "Crayon", "Marker", "Natural pencil"], "version": "11.2605.81.0"}


def obj(oid, kind, pts, fill=None, stroke="#000000", layer=0, **kw):
    return DrawingObject(oid, kind, tuple(pts), fill, stroke, "structure", layer, **kw)


def plan_of(objects, canvas=CANVAS):
    return geometry.plan_scene(objects, canvas, margin_fraction=0.01, check_overlaps=False)


class FakeDriver:
    """A PIL canvas that behaves like Paint for the engine; `probe` is what the readiness check reads."""

    def __init__(self, plan, *, scale=1.0, fail_on=None, caps=CAPS, ready_error=None):
        self.plan, self.scale, self.caps = plan, scale, caps
        self.image = Image.new("RGB", (plan.canvas.width, plan.canvas.height), (255, 255, 255))
        self.drawn: list[str] = []
        self.gate = None
        self.fail_on, self.ready_error = fail_on, ready_error
        self.probes = 0
        self.history: list[Image.Image] = []
        self.probe_scales: list[float] = []

    def probe(self):
        self.probes += 1
        scale = self.probe_scales.pop(0) if self.probe_scales else self.scale
        return {"window_ok": True, "canvas": (self.plan.canvas.width, self.plan.canvas.height), "scale": scale, "undo": True,
                "tools": self.caps["tools"], "brushes": self.caps["brushes"], "version": self.caps["version"],
                "capabilities": self.caps}

    def check_ready(self):
        if self.ready_error:
            raise self.ready_error

    def recover(self, error):
        return True

    def capture(self):
        return self.image.copy()

    def undo(self):
        self.image = self.history.pop()
        return True

    def draw(self, o):
        if self.gate is None:
            raise ToolError(ErrorCode.POLICY_DENIED, "no gate")
        self.gate.assert_open()
        if self.fail_on and o.id == self.fail_on:
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"mouse failure on {o.id}", retryable=False)
        self.history.append(self.image.copy())
        self.drawn.append(o.id)
        if o.kind == "fill":
            ImageDraw.floodfill(self.image, (round(o.points[0][0]), round(o.points[0][1])), o.fill, thresh=0)
            return
        d = ImageDraw.Draw(self.image)
        pts = list(o.outline)
        if o.fill is not None and o.closed:
            d.polygon(pts, fill=o.fill)
        if o.stroke is not None:
            d.line([*pts, pts[0]] if o.closed else pts, fill=o.stroke, width=o.stroke_px)


def simple_objects():
    return [obj("sun", "ellipse", [(500, 40), (620, 150)], fill="#f1c40f", stroke="#c0392b", layer=0),
            obj("house", "rect", [(100, 200), (300, 380)], fill="#e67e22", stroke="#000000", layer=1),
            obj("roof", "triangle", [(80, 120), (320, 200)], fill="#c0392b", stroke="#000000", layer=2),
            obj("path", "polyline", [(350, 400), (500, 360), (700, 390)], stroke="#27ae60", layer=3)]


def spec_for(objects=None, **kw):
    plan = plan_of(objects or simple_objects())
    ordered = engine.draw_order(plan.objects)
    return spec_mod.build_spec(request="a house under a sun", plan=plan, draw_order=ordered, stages=engine._stages(ordered),
                               caps=CAPS, canvas_state={"scale": 1.0}, **kw), plan, ordered


# 1 / 2 ------------------------------------------------------------------------------------------------------


def test_new_simple_picture_passes_the_whole_gate(tmp_path: Path) -> None:
    plan = plan_of(simple_objects())
    driver = FakeDriver(plan)
    result, gate = run_gated(plan, driver, request="a house under a sun", directory=tmp_path, source={"kind": "objects", "name": "house"})
    assert result.ok and gate.run.state == State.COMPLETED and gate.validated and gate.ready
    spec = gate.spec
    assert spec["admin"]["specification_status"] == "VALIDATED" and spec["execution"]["completion_status"] == "verified"
    assert spec["admin"]["evidence"] and Path(spec["admin"]["evidence"][0]["path"]).is_file()
    states = [h["to"] for h in gate.run.history]
    assert states == ["ANALYZING_REQUEST", "ANALYZING_REFERENCE", "BUILDING_SPECIFICATION", "VALIDATING_SPECIFICATION",
                      "BUILDING_DRAWING_PLAN", "CHECKING_READINESS", "READY_TO_DRAW", "DRAWING", "VALIDATING_RESULT", "COMPLETED"]
    assert Path(gate.spec_path).is_file()
    # nothing was drawn before the gate opened: the first drawing call came after READY_TO_DRAW
    assert driver.drawn and spec["metadata"]["reference_type"] == "original"


def test_complex_scene_has_every_group_and_consistent_geometry(tmp_path: Path) -> None:
    plan = geometry.plan_scene(build_template("doraemon", CANVAS), CANVAS)
    result, gate = run_gated(plan, FakeDriver(plan), request="doraemon", directory=tmp_path, source={"kind": "template", "name": "doraemon"})
    spec = gate.spec
    for group in ("metadata", "canvas", "medium", "composition", "objects", "color_lighting", "strokes", "execution", "admin"):
        assert group in spec
    assert len(spec["objects"]) >= 20 and all(o["occlusion"] is not None for o in spec["objects"])
    assert spec["color_lighting"]["primary_color"].startswith("#") and spec["composition"]["focal_point"]["u"] > 0
    assert result.ok and gate.run.state == State.COMPLETED


# 3 / 4 ------------------------------------------------------------------------------------------------------


def test_famous_work_is_specified_as_a_reproduction_not_a_free_invention(tmp_path: Path) -> None:
    brief = brief_mod.build_brief("Vẽ The Starry Night của Van Gogh", store=ArtKnowledgeStore()).to_dict()
    plan = plan_of(simple_objects())
    _, gate = run_gated(plan, FakeDriver(plan), request="Vẽ The Starry Night", directory=tmp_path, brief=brief,
                        mode=Mode.OIL_PAINT_SIMULATION, source={"kind": "objects", "name": "starry"})
    m = gate.spec["metadata"]
    assert m["reference_type"] == "existing_artwork" and m["fidelity_level"] == "faithful_reproduction"
    assert m["title"].startswith("The Starry Night") and gate.spec["medium"]["medium"] == "oil"
    assert gate.spec["medium"]["material_constraints"]["physical_simulation"] is False
    assert gate.spec["admin"]["brief_verification"] == "BUILTIN_KNOWLEDGE"  # not claimed as verified


def test_photo_reference_is_recorded_and_light_is_inferred_not_known(tmp_path: Path) -> None:
    ref = Image.new("RGB", (160, 100), (30, 30, 30))
    ImageDraw.Draw(ref).ellipse((100, 10, 150, 50), fill=(250, 250, 200))
    plan = plan_of(simple_objects())
    _, gate = run_gated(plan, FakeDriver(plan), request="redraw this photo", directory=tmp_path, reference=ref,
                        source={"kind": "image", "name": "photo.jpg", "reference_path": "photo.jpg"}, user_supplied_plan=False)
    spec = gate.spec
    assert spec["metadata"]["reference_type"] == "photo_reference"
    assert spec["color_lighting"]["light_source"]["status"] == "INFERRED"
    assert spec["objects"][0]["confidence"] < 1.0  # traced, not given


# 5-9 --------------------------------------------------------------------------------------------------------


def test_missing_canvas_size_blocks() -> None:
    spec, plan, order = spec_for()
    del spec["canvas"]["canvas_width"]
    v = validate_spec(spec, plan=plan, draw_order=order, caps=CAPS)
    assert any(i.code == "MISSING_FIELD" and i.path == "canvas.canvas_width" for i in v.blocking)


def test_points_outside_the_canvas_block() -> None:
    spec, plan, order = spec_for()
    spec["objects"][0]["contour_points"][0] = [9999, 5]
    spec["strokes"][0]["stroke_points"][0] = [-50, 5]
    codes = {i.code for i in validate_spec(spec, caps=CAPS).blocking}
    assert "POINT_OUTSIDE" in codes


def test_negative_and_oversized_boxes_block() -> None:
    spec, _, _ = spec_for()
    spec["objects"][0]["bounding_box"]["width"] = -4
    spec["objects"][1]["bounding_box"]["width"] = 5000
    codes = {i.code for i in validate_spec(spec, caps=CAPS).blocking}
    assert {"BBOX_NEGATIVE", "BBOX_OUTSIDE"} <= codes


def test_bad_hex_colour_blocks() -> None:
    spec, _, _ = spec_for()
    spec["strokes"][0]["stroke_color"] = "red"
    spec["color_lighting"]["primary_color"] = "#12G456"
    paths = {i.path for i in validate_spec(spec, caps=CAPS).blocking if i.code == "HEX"}
    assert "strokes[0].stroke_color" in paths and "color_lighting.primary_color" in paths


def test_dependency_cycle_and_missing_dependency_block() -> None:
    spec, _, _ = spec_for()
    graph = spec["execution"]["dependency_graph"]
    a, b = list(graph)[:2]
    graph[a] = [b]
    graph[b] = [a]
    graph[list(graph)[2]] = ["ghost"]
    codes = {i.code for i in validate_spec(spec, caps=CAPS).blocking}
    assert "GRAPH_CYCLE" in codes and "GRAPH_MISSING" in codes


# 10 ---------------------------------------------------------------------------------------------------------


def _fill_scene(closed: bool):
    outline = (obj("box", "rect", [(100, 80), (300, 220)], stroke="#000000") if closed else
               obj("box", "polyline", [(100, 80), (300, 80), (300, 220), (100, 220)], stroke="#000000"))
    return [outline, obj("tint", "fill", [(200, 150)], fill="#f1c40f", stroke=None, layer=1, region="box" if closed else None)]


def test_flood_fill_in_an_open_region_is_blocked(tmp_path: Path) -> None:
    plan = plan_of(_fill_scene(False))
    driver = FakeDriver(plan)
    with pytest.raises(ToolError) as info:
        run_gated(plan, driver, request="open box", directory=tmp_path, source={"kind": "objects", "name": "box"})
    assert info.value.code == ErrorCode.INVALID_ARGUMENT and "FILL_NOT_CLOSED" in str(info.value.details)
    assert driver.drawn == []  # nothing touched the canvas


def test_flood_fill_in_a_closed_region_runs(tmp_path: Path) -> None:
    plan = plan_of(_fill_scene(True))
    driver = FakeDriver(plan)
    result, gate = run_gated(plan, driver, request="closed box", directory=tmp_path, source={"kind": "objects", "name": "box"})
    assert result.ok and driver.image.getpixel((200, 150)) == (0xF1, 0xC4, 0x0F)


# 11 ---------------------------------------------------------------------------------------------------------


def test_a_brush_this_paint_does_not_have_is_blocked(tmp_path: Path) -> None:
    objects = [obj("s", "path", [(50, 50), (300, 90), (500, 60)], stroke="#102030", width_px=9, brush="Charcoal pencil 9000")]
    plan = plan_of(objects)
    driver = FakeDriver(plan)
    with pytest.raises(ToolError) as info:
        run_gated(plan, driver, request="stroke", directory=tmp_path, source={"kind": "objects", "name": "s"})
    assert "TOOL_UNAVAILABLE" in str(info.value.details) and driver.drawn == []


# 12 ---------------------------------------------------------------------------------------------------------


def test_changed_request_starts_a_new_specification() -> None:
    first, _, _ = spec_for()
    gate = ExecutionGate()
    gate.run.go(State.ANALYZING_REQUEST)
    gate.run.go(State.AWAITING_USER_INPUT)
    gate.run.go(State.ANALYZING_REQUEST)  # the user answered / changed the request
    second, _, _ = spec_for(objects=simple_objects()[:2])
    assert first["metadata"]["artwork_id"] != second["metadata"]["artwork_id"]
    assert len(second["objects"]) == 2 and not gate.validated and not gate.is_open()


def test_ambiguous_request_waits_for_the_user(tmp_path: Path) -> None:
    brief = brief_mod.build_brief("vẽ The Scream", store=ArtKnowledgeStore()).to_dict()
    plan = plan_of(simple_objects())
    driver = FakeDriver(plan)
    with pytest.raises(ToolError) as info:
        run_gated(plan, driver, request="vẽ The Scream", directory=tmp_path, brief=brief, source={"kind": "objects", "name": "x"})
    assert info.value.details["state"] == "AWAITING_USER_INPUT" and info.value.details["questions"] and driver.drawn == []


# 13 ---------------------------------------------------------------------------------------------------------


def test_zoom_changed_between_planning_and_drawing_is_caught(tmp_path: Path) -> None:
    plan = plan_of(simple_objects())
    driver = FakeDriver(plan)
    driver.probe_scales = [1.0, 1.25]  # readiness sees 100 %, the pre-draw re-read sees 125 %
    with pytest.raises(ToolError) as info:
        run_gated(plan, driver, request="house", directory=tmp_path, source={"kind": "objects", "name": "h"})
    assert info.value.code == ErrorCode.CONFLICT and driver.drawn == []


# 14 ---------------------------------------------------------------------------------------------------------


def test_failure_midway_keeps_earlier_work_and_marks_the_spec_failed(tmp_path: Path) -> None:
    plan = plan_of(simple_objects())
    driver = FakeDriver(plan, fail_on="roof")
    with pytest.raises(ToolError) as info:
        run_gated(plan, driver, request="house", directory=tmp_path, source={"kind": "objects", "name": "h"})
    assert "mouse failure" in info.value.message and info.value.details["state"] == "FAILED"
    assert driver.drawn and "roof" not in driver.drawn and driver.image.getpixel((200, 300)) != (255, 255, 255)  # not cleared
    saved = Path(info.value.details["specification"]).read_text("utf-8")
    assert '"completion_status": "failed"' in saved


# 15 ---------------------------------------------------------------------------------------------------------


def test_real_driver_refuses_to_draw_without_an_open_gate() -> None:
    driver = PaintDriver.__new__(PaintDriver)  # no Paint needed: the check comes first
    driver.gate = None
    plan = plan_of(simple_objects())
    with pytest.raises(ToolError) as info:
        driver.draw(plan.objects[0])
    assert info.value.code == ErrorCode.POLICY_DENIED
    closed = ExecutionGate()
    closed.run.go(State.ANALYZING_REQUEST)
    closed.validated = closed.ready = True
    driver.gate = closed
    with pytest.raises(GateClosed):  # validated and ready, but not in DRAWING
        driver.draw(plan.objects[0])


def test_gate_states_cannot_be_skipped() -> None:
    gate = ExecutionGate()
    with pytest.raises(GateClosed):
        gate.run.go(State.DRAWING)
    gate.run.go(State.ANALYZING_REQUEST)
    with pytest.raises(GateClosed):
        gate.run.go(State.READY_TO_DRAW)
    with pytest.raises(GateClosed):
        gate.begin({"window_ok": True})


# 16 / 17 ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("broken", [{}, {"metadata": {}}, {"admin": "x"}, {"objects": "nope"}])
def test_invalid_model_output_is_rejected(broken) -> None:
    v = validate_spec(broken, caps=CAPS)
    assert v.blocking and not v.ok


def test_verified_without_evidence_is_rejected() -> None:
    spec, _, _ = spec_for()
    spec["execution"]["completion_status"] = "verified"
    assert any(i.code == "VERIFIED_WITHOUT_EVIDENCE" for i in validate_spec(spec, caps=CAPS).blocking)
    spec["execution"]["actual_result"] = {"ok": True, "screenshot": "x.png"}
    spec["admin"]["evidence"] = [{"kind": "screenshot", "path": "x.png"}]
    assert not any(i.code == "VERIFIED_WITHOUT_EVIDENCE" for i in validate_spec(spec, caps=CAPS).issues)


# 18 / 19 / 20 -----------------------------------------------------------------------------------------------


def test_offline_brief_does_not_need_the_network(tmp_path: Path) -> None:
    brief = brief_mod.build_brief("Vẽ The Starry Night của Van Gogh", store=ArtKnowledgeStore(), offline=True).to_dict()
    assert any("offline" in n for n in brief["notes"]) and brief["verification"] == "BUILTIN_KNOWLEDGE"
    plan = plan_of(simple_objects())
    _, gate = run_gated(plan, FakeDriver(plan), request="starry night", directory=tmp_path, brief=brief,
                        source={"kind": "objects", "name": "s"})
    assert gate.run.state == State.COMPLETED and gate.spec["admin"]["brief_verification"] == "BUILTIN_KNOWLEDGE"


def test_retry_limit_stops_a_driver_that_keeps_failing(tmp_path: Path) -> None:
    plan = plan_of(simple_objects())
    err = ToolError(ErrorCode.EXECUTION_FAILED, "Paint lost the foreground", retryable=True, details={"recoverable": "focus"})
    driver = FakeDriver(plan, ready_error=err)
    with pytest.raises(ToolError):
        run_gated(plan, driver, request="house", directory=tmp_path, source={"kind": "objects", "name": "h"})
    assert driver.drawn == []


def test_resume_draws_only_what_is_missing(tmp_path: Path) -> None:
    plan = plan_of(simple_objects())
    driver = FakeDriver(plan)
    run_gated(plan, driver, request="house", directory=tmp_path, source={"kind": "objects", "name": "h"})
    first = len(driver.drawn)
    again = FakeDriver(plan)
    again.image = driver.image.copy()  # the canvas as the first run left it
    result, gate = run_gated(plan, again, request="house", directory=tmp_path, resume=True,
                             source={"kind": "objects", "name": "h"})
    assert first >= 4 and again.drawn == [] and result.ok and gate.run.state == State.COMPLETED
