"""Doraemon template V2 (docs/specs/NANG_CAP_LOGIC_VE_DORAEMON.txt): geometry, layering, mapping, quality verdicts,
template versions. Pure part only: a fake Paint draws on a PIL image, no desktop is touched."""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from lga_worker.art.gate import State, run_gated
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import calibration, curves, doraemon, engine, geometry, occlusion, quality, raster
from lga_worker.paint.model import Canvas
from lga_worker.paint.template_store import TemplateStore, defects_from_quality, quality_score
from lga_worker.paint.templates import build_template
from lga_worker.windows.provider import Rect

CANVAS = Canvas(1152, 648)
CANVASES = [CANVAS, Canvas(800, 600), Canvas(1920, 1080), Canvas(3840, 2160), Canvas(1024, 768), Canvas(600, 900)]
CAPS = {"tools": ["Rectangle", "Oval", "Triangle", "Polygon", "Line", "Pencil", "Fill", "Brushes"],
        "brushes": ["Brush", "Oil brush", "Crayon", "Marker", "Natural pencil"], "version": "11.2605.81.0"}


def prepared(canvas: Canvas = CANVAS, **kw) -> doraemon.Prepared:
    return doraemon.prepare(canvas, **kw)


def by_id(p: doraemon.Prepared) -> dict:
    return p.scene.by_id()


# --- normalised geometry ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("canvas", CANVASES)
def test_every_constraint_holds_on_every_canvas_shape(canvas: Canvas) -> None:
    p = prepared(canvas)
    assert [c.name for c in p.constraints if not c.ok] == []
    plan = geometry.plan_scene(p.scene.objects, canvas)
    assert plan.warnings == []
    m = plan.margin_px
    for o in plan.objects:
        x0, y0, x1, y1 = o.bounds
        assert x0 >= m - 0.5 and y0 >= m - 0.5 and x1 <= canvas.width - m + 0.5 and y1 <= canvas.height - m + 0.5, o.id


@pytest.mark.parametrize("canvas", CANVASES)
def test_the_figure_is_scaled_uniformly_never_stretched(canvas: Canvas) -> None:
    p = prepared(canvas)
    pts = [q for name in doraemon.SILHOUETTE_PARTS for q in doraemon._all_points(by_id(p)[name])]
    x0, y0, x1, y1 = doraemon._bounds(pts)
    aspect_px = ((x1 - x0) * canvas.px_per_unit) / ((y1 - y0) * canvas.px_per_unit)
    reference = prepared(CANVAS)
    rp = [q for name in doraemon.SILHOUETTE_PARTS for q in doraemon._all_points(by_id(reference)[name])]
    rx0, ry0, rx1, ry1 = doraemon._bounds(rp)
    assert aspect_px == pytest.approx((rx1 - rx0) / (ry1 - ry0), abs=0.002)
    assert doraemon.SILHOUETTE_ASPECT[0] <= aspect_px <= doraemon.SILHOUETTE_ASPECT[1]


def test_baseline_proportions_are_fixed() -> None:
    """Version 1 had a head of 1.29 and a silhouette of about 0.98; the checks would catch both."""
    p = prepared()
    head = doraemon._bounds(by_id(p)["head_outer"].points)
    assert (head[2] - head[0]) / (head[3] - head[1]) == pytest.approx(1.14, abs=0.01)
    v1 = geometry.plan_scene(build_template("doraemon_v1", CANVAS), CANVAS)
    xs = [x for o in v1.objects if o.id in ("head", "torso", "arm_l", "arm_r", "hand_l", "hand_r") for x, _ in o.outline]
    ys = [y for o in v1.objects for _, y in o.outline]
    assert (max(xs) - min(xs)) / (max(ys) - min(ys)) > 0.9  # the old figure was too wide: version 2 is not


def test_figure_height_and_centring_on_the_default_canvas() -> None:
    p = prepared()
    pts = [q for name in doraemon.SILHOUETTE_PARTS for q in doraemon._all_points(by_id(p)[name])]
    x0, y0, x1, y1 = doraemon._bounds(pts)
    assert 0.84 <= (y1 - y0) / CANVAS.unit_height <= 0.90
    assert abs((x0 + x1) / 2 - 500) < 5 and abs((y0 + y1) / 2 - CANVAS.unit_height / 2) < 5
    head = doraemon._bounds(by_id(p)["head_outer"].points)
    assert 0.44 <= (head[3] - head[1]) / (y1 - y0) <= 0.56


def test_there_are_28_named_objects_with_a_stage_each() -> None:
    p = prepared()
    ids = [o.id for o in p.scene.objects]
    assert sorted(ids) == sorted(doraemon.ROLES) and len(ids) == 28
    assert set(doraemon.STAGE_OF.values()) == {"A", "B", "C", "D"}
    stage_layers: dict[str, list[int]] = {}
    for o in p.scene.objects:
        stage_layers.setdefault(doraemon.STAGE_OF[o.id], []).append(o.layer)
    assert max(stage_layers["A"]) < min(stage_layers["B"]) <= max(stage_layers["B"]) < min(stage_layers["C"])
    assert max(stage_layers["C"]) < min(stage_layers["D"])


def test_curves_are_sampled_evenly_and_finely() -> None:
    seg = ((0.0, 0.0), (10.0, 40.0), (50.0, 40.0), (60.0, 0.0))
    pts = curves.sample_bezier(*seg, 3.0)
    gaps = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    assert max(gaps) <= 3.3 and min(gaps) >= 2.7
    assert pts[0] == seg[0] and pts[-1] == pytest.approx(seg[3], abs=1e-6)
    assert curves.max_chord_error(pts, [curves.bezier_point(*seg, k / 100) for k in range(101)]) < 0.2
    mid = curves.bezier_midpoint(*seg)
    assert curves.distance_to_polyline(pts, mid) < 0.05


def test_polygon_vertices_keep_paints_minimum_gap() -> None:
    p = prepared()
    px = CANVAS.px_per_unit
    for o in p.scene.objects:
        if o.kind != "polygon":
            continue
        ring = list(o.points)
        gaps = [math.dist(a, b) * px for a, b in zip(ring, ring[1:] + ring[:1])]
        assert min(gaps) >= 5.0, (o.id, min(gaps))  # closer vertices read as a double click in Paint


# --- the features of the specification -------------------------------------------------------------------------


def test_nose_line_ends_on_the_smile_at_its_middle() -> None:
    p = prepared()
    by = by_id(p)
    smile, line = list(by["smile_curve"].points), list(by["nose_to_mouth"].points)
    first, last = smile[0], smile[-1]
    mid_x = (first[0] + last[0]) / 2
    assert line[-1][0] == pytest.approx(mid_x, abs=0.3)
    assert curves.distance_to_polyline(smile, line[-1]) < 0.1
    assert line[-1][1] <= max(q[1] for q in smile) + 0.3  # never deeper than the lowest point of the curve
    nose = doraemon._bounds(doraemon._all_points(by["nose"]))
    assert line[0][1] == pytest.approx(nose[3], abs=0.6)  # starts at the bottom of the nose


def test_exactly_six_whiskers_inside_the_head_and_mirrored() -> None:
    p = prepared()
    by = by_id(p)
    whiskers = [k for k in by if k.startswith("whisker_")]
    assert len(whiskers) == 6 and sum("_left_" in k for k in whiskers) == 3
    head = list(by["head_outer"].points)
    for k in whiskers:
        for q in (by[k].points[0], by[k].points[-1]):
            assert curves.polygon_contains(head, q)
    axis = p.scene.frame.pt(doraemon.AXIS, 0)[0]
    for row in ("top", "middle", "bottom"):
        a, b = by[f"whisker_left_{row}"].points[-1], by[f"whisker_right_{row}"].points[-1]
        assert (a[0] + b[0]) / 2 == pytest.approx(axis, abs=0.01) and a[1] == pytest.approx(b[1])


def test_collar_is_a_capsule_and_the_pocket_a_soft_u() -> None:
    by = by_id(prepared())
    collar = by["collar"]
    box = doraemon._bounds(collar.points)
    corner = (box[0], box[1])
    assert min(math.dist(corner, q) for q in collar.points) > 0.3 * (box[3] - box[1]) / 2
    assert collar.kind == "polygon" and len(collar.points) >= 12
    pocket = by["pocket_outline"]
    assert pocket.kind == "path" and pocket.fill is None and len(pocket.points) >= 20
    ys = [q[1] for q in pocket.points]
    top = min(ys)
    flat = [q for q in pocket.points if q[1] <= top + 1e-6]
    assert len(flat) >= 4  # the horizontal mouth line
    xs = [q[0] for q in pocket.points]
    assert max(ys) - top > 0.6 * (max(xs) - min(xs)) * 0.5  # a deep curved base, not a polygon corner


def test_bell_sits_on_the_collar_with_groove_and_dot() -> None:
    by = by_id(prepared())
    bell, collar = doraemon._bounds(doraemon._all_points(by["bell"])), doraemon._bounds(by["collar"].points)
    assert bell[1] < collar[3] < bell[3]
    assert by["bell_groove"].kind == "line" and by["bell_dot"].fill == "#000000"
    assert [o.id for o in prepared().scene.objects].index("bell") > [o.id for o in prepared().scene.objects].index("collar")


# --- layering / occlusion ----------------------------------------------------------------------------------------


def test_occlusion_graph_is_acyclic_and_the_layers_respect_it() -> None:
    assert occlusion.find_cycle(doraemon.EDGES) is None
    layers = {o.id: o.layer for o in prepared().scene.objects}
    assert occlusion.order_problems(layers, doraemon.EDGES) == []


@pytest.mark.parametrize("back,front", [
    ("arm_left", "torso"), ("arm_right", "torso"),            # the torso covers the root of the arms
    ("torso", "head_outer"), ("head_outer", "face_patch"),     # the head in front of the neck, the face inside it
    ("head_outer", "collar"), ("face_patch", "collar"),        # the collar over head and face (not covered by them)
    ("collar", "bell"), ("head_outer", "bell"),                # the bell over the collar, never under the head
    ("face_patch", "smile_curve"), ("face_patch", "whisker_left_top"), ("face_patch", "eye_left"),
])
def test_regression_layer_bugs_of_version_1(back: str, front: str) -> None:
    layers = {o.id: o.layer for o in prepared().scene.objects}
    assert layers[front] > layers[back]
    plan = geometry.plan_scene(prepared().scene.objects, CANVAS)
    order = [o.id for o in plan.objects]
    assert order.index(front) > order.index(back)


def test_occlusion_helpers_detect_cycles_and_broken_orders() -> None:
    with pytest.raises(occlusion.OcclusionCycle):
        occlusion.assign_layers(["a", "b"], [("a", "b"), ("b", "a")])
    assert occlusion.assign_layers(["a", "b", "c"], [("a", "b"), ("b", "c")]) == {"a": 0, "b": 1, "c": 2}
    problems = occlusion.order_problems({"arm": 5, "torso": 1}, [("arm", "torso")])
    assert problems and "torso" in problems[0]
    assert occlusion.order_problems({"a": 1}, [("a", "ghost")])[0].startswith("'ghost' is missing")


def test_the_engine_draws_back_to_front_so_the_collar_is_not_painted_over() -> None:
    p = prepared()
    plan = geometry.plan_scene(p.scene.objects, CANVAS)
    fake = FakePaint(plan)
    drawn: list[str] = []
    orig = fake.draw
    fake.draw = lambda o: (drawn.append(o.id), orig(o))[1]  # type: ignore[method-assign]
    result = engine.run_plan(plan, fake)
    assert result.ok
    for back, front in doraemon.EDGES:
        assert drawn.index(front) > drawn.index(back), (back, front)


# --- stroke profile / palette --------------------------------------------------------------------------------------


@pytest.mark.parametrize("canvas,expected", [(CANVAS, (5, 3, 4)), (Canvas(1920, 1080), (8, 5, 7)), (Canvas(800, 450), (3, 3, 3))])
def test_stroke_profile_scales_with_the_canvas(canvas: Canvas, expected: tuple[int, int, int]) -> None:
    prof = doraemon.StrokeProfile.for_canvas(canvas)
    assert (prof.silhouette, prof.region, prof.line) == expected
    assert prof.silhouette in geometry.SHAPE_WIDTHS and prof.region in geometry.SHAPE_WIDTHS  # widths Paint really offers
    assert prof.silhouette >= prof.region


def test_outline_hierarchy_and_symmetry_in_the_plan() -> None:
    plan = geometry.plan_scene(prepared().scene.objects, CANVAS)
    width = {o.id: o.stroke_px for o in plan.objects}
    assert width["head_outer"] > width["face_patch"] and width["torso"] > width["belly"]
    assert width["arm_left"] == width["arm_right"] and width["whisker_left_top"] == width["whisker_right_top"]
    assert len({width[k] for k in width if k.startswith("whisker_")}) == 1


def test_palette_is_the_baseline_and_options_override_it() -> None:
    p = prepared()
    colours = {o.fill for o in p.scene.objects if o.fill} | {o.stroke for o in p.scene.objects if o.stroke}
    assert colours == {"#0096E0", "#FFFFFF", "#000000", "#E60012", "#FFD400"}
    q = prepared(options={"body": "#112233"})
    assert by_id(q)["torso"].fill == "#112233"
    assert q.reference["status"] == "OFFLINE_TEMPLATE"


# --- self-correction and refusal ----------------------------------------------------------------------------------


def test_prepare_corrects_parameters_that_break_the_ratios() -> None:
    wide = doraemon.Params(hand_reach=36.5)  # inside the clamp, but the figure is too wide with a tall canvas rule
    p = prepared(params=wide)
    assert all(c.ok for c in p.constraints)
    with pytest.raises(ToolError) as err:
        doraemon.build_scene(CANVAS, doraemon.Params(head_aspect=1.4))
    assert err.value.code == ErrorCode.INVALID_ARGUMENT and "head_aspect" in err.value.message
    # prepare() clamps first, so a wild value is corrected instead of refused
    assert all(c.ok for c in prepared(params=doraemon.Params(head_aspect=1.4)).constraints)


def test_a_broken_scene_is_refused_before_any_drawing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(canvas, params=None):  # noqa: ANN001
        scene = real(canvas, params)
        scene.objects[:] = [o for o in scene.objects if o.id != "whisker_right_top"]
        return scene

    real = doraemon.build_scene
    monkeypatch.setattr(doraemon, "build_scene", broken)
    with pytest.raises(ToolError, match="breaks its own proportions"):
        doraemon.prepare(CANVAS)


def test_constraint_checker_flags_each_known_defect() -> None:
    p = prepared()
    scene = p.scene

    def with_replaced(oid: str, **changes):  # noqa: ANN003
        objs = [dataclasses.replace(o, **changes) if o.id == oid else o for o in scene.objects]
        return dataclasses.replace(scene, objects=objs)

    def failed(s) -> set[str]:  # noqa: ANN001
        return {c.name for c in doraemon.check_constraints(s) if not c.ok}

    line = by_id(p)["nose_to_mouth"]
    deeper = tuple(line.points[:-1]) + ((line.points[-1][0], line.points[-1][1] + 12),)
    assert "nose_line_ends_on_smile" in failed(with_replaced("nose_to_mouth", points=deeper))
    rect = by_id(p)["collar"]
    box = doraemon._bounds(rect.points)
    assert "collar_is_capsule" in failed(with_replaced("collar", points=((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))))
    assert "layer_order" in failed(with_replaced("arm_left", layer=99))
    assert "palette" in failed(with_replaced("bell", fill="#123456"))
    assert "named_objects" in failed(dataclasses.replace(scene, objects=[o for o in scene.objects if o.id != "whisker_left_top"]))


# --- canvas <-> screen mapping (one function, any zoom/origin/dpi) --------------------------------------------------


@pytest.mark.parametrize("zoom,origin", [(1.0, (384, 284)), (0.5, (100, 100)), (1.25, (0, 40)), (2.0, (-200, 60)), (1.5, (777, 301))])
def test_one_transform_matches_the_specified_formula(zoom: float, origin: tuple[int, int]) -> None:
    w, h = round(CANVAS.width * zoom), round(CANVAS.height * zoom)
    t = calibration.CanvasTransform.from_rect(CANVAS, Rect(origin[0], origin[1], origin[0] + w, origin[1] + h), zoom_hint=zoom)
    for cx, cy in ((0, 0), (576, 324), (1151, 647), (100.5, 200.25)):
        sx = origin[0] + cx * w / CANVAS.width
        sy = origin[1] + cy * h / CANVAS.height
        assert t.to_screen(cx, cy) == (round(sx), round(sy))
        back = t.to_canvas(*t.to_screen(cx, cy))
        assert abs(back[0] - cx) <= 0.5 / zoom + 1e-9 and abs(back[1] - cy) <= 0.5 / zoom + 1e-9


def test_a_drifted_calibration_is_detected_not_drawn_through() -> None:
    from PIL import ImageDraw as D

    shot = Image.new("RGB", (1920, 1080), (238, 244, 232))
    D.Draw(shot).rectangle((384, 284, 384 + 1151, 284 + 647), fill=(255, 255, 255))
    t = calibration.CanvasTransform.from_rect(CANVAS, Rect(384, 284, 384 + 1152, 284 + 648))
    assert calibration.cross_check(t, shot, (0, 0)) == 0
    drifted = calibration.CanvasTransform.from_rect(CANVAS, Rect(394, 284, 394 + 1152, 284 + 648))
    assert calibration.cross_check(drifted, shot, (0, 0)) == 10  # above the 2 px tolerance: the driver stops and recalibrates


def test_the_scene_does_not_depend_on_any_screen_position() -> None:
    a = [(o.id, o.points) for o in prepared().scene.objects]
    b = [(o.id, o.points) for o in prepared().scene.objects]
    assert a == b  # pure function of the canvas size: no screen_rect, scale or window position goes in


# --- fake Paint, quality verdicts, specification -----------------------------------------------------------------


class FakePaint:
    scale = 1.0

    def __init__(self, plan, *, ready_error: ToolError | None = None, recover: bool = False):
        self.plan = plan
        self.image = Image.new("RGB", (plan.canvas.width, plan.canvas.height), "white")
        self.gate = None
        self.ready_error, self.can_recover, self.recovered = ready_error, recover, 0
        self.history: list[Image.Image] = []
        self.calls = 0

    def probe(self):
        return {"window_ok": True, "canvas": (self.plan.canvas.width, self.plan.canvas.height), "scale": 1.0, "undo": True,
                "tools": CAPS["tools"], "brushes": CAPS["brushes"], "version": CAPS["version"], "capabilities": CAPS}

    def check_ready(self):
        if self.ready_error is not None and self.calls == 3:
            err, self.ready_error = self.ready_error, None
            raise err

    def recover(self, error):  # noqa: ANN001
        self.recovered += 1
        return self.can_recover

    def capture(self):
        return self.image.copy()

    def undo(self):
        self.image = self.history.pop()
        return True

    def draw(self, o):
        if self.gate is not None:
            self.gate.assert_open()
        self.calls += 1
        self.history.append(self.image.copy())
        raster._draw_object(ImageDraw.Draw(self.image), o)


def run(canvas: Canvas = CANVAS, tmp_path: Path | None = None, *, driver_cls=FakePaint, **driver_kw):
    p = prepared(canvas)
    plan = geometry.plan_scene(p.scene.objects, canvas)
    info = doraemon.scene_info(p)
    driver = driver_cls(plan, **driver_kw)

    def assess(pl, img, res, shot, window_ok):  # noqa: ANN001
        return quality.assess(pl, img, res, info=info, window_ok=window_ok, screenshot=shot)

    source = {"kind": "template", "name": "doraemon", "template_version": p.scene.version, "reference_status": "OFFLINE_TEMPLATE",
              "reference_source": p.reference["source"], "measured": [], "object_confidence": 0.8}
    result, gate = run_gated(plan, driver, request="ve Doraemon", directory=tmp_path, source=source, assess=assess)
    return plan, driver, result, gate, info


def test_a_correct_drawing_passes_both_flags_but_states_its_limits(tmp_path: Path) -> None:
    plan, driver, result, gate, info = run(tmp_path=tmp_path)
    q = gate.quality
    assert result.ok and q["DRAW_EXECUTION_PASS"] and q["VISUAL_QUALITY_PASS"]
    fid = q["character_fidelity"]
    assert fid["status"] == "PASS" and fid["reference_status"] == "OFFLINE_TEMPLATE"
    assert any("OFFLINE_TEMPLATE" in n for n in fid["limitations"]) and any("not been reviewed" in n for n in fid["limitations"])
    assert max(v for v in fid["landmark_errors"].values() if v is not None) < 0.01
    assert q["canvas_safety"]["ok"] and q["canvas_safety"]["screenshot_opens"]
    assert quality_score(q) > 0.9
    # 4 stages -> one screenshot per stage, and they exist
    stages = gate.spec["execution"]["actual_result"]["stages"]
    assert len(stages) >= 4 and all(s["screenshot"] and Path(s["screenshot"]).is_file() for s in stages)


def test_specification_records_a_reference_based_template_not_free_interpretation(tmp_path: Path) -> None:
    _, _, _, gate, _ = run(tmp_path=tmp_path)
    spec = gate.spec
    md = spec["metadata"]
    assert md["fidelity_level"] == "faithful_reproduction" and md["reference_type"] == "existing_artwork"
    assert md["reference_status"] == "OFFLINE_TEMPLATE" and md["template_version"] == doraemon.VERSION
    assert all(o["confidence"] < 1.0 for o in spec["objects"])  # inferred from a template, not measured on a picture
    prov = spec["admin"]["provenance"]
    assert prov["objects"]["status"] == "INFERRED" and prov["color_lighting.color_palette"]["status"] == "INFERRED"
    assert spec["execution"]["completion_status"] == "verified"  # "executed", kept apart from the visual verdict:
    assert spec["execution"]["visual_quality_status"] == "passed" and spec["execution"]["draw_execution_pass"] is True
    assert gate.run.state == State.COMPLETED


def test_execution_pass_never_implies_visual_pass(tmp_path: Path) -> None:
    class Messy(FakePaint):
        def draw(self, o):  # noqa: ANN001
            super().draw(o)
            if o.id == "nose_to_mouth":  # a stray dot far from anything planned (the mouse stopped and clicked)
                ImageDraw.Draw(self.image).ellipse((100, 560, 106, 566), fill=(0, 0, 0))

    plan, driver, result, gate, info = run(tmp_path=tmp_path, driver_cls=Messy)
    q = gate.quality
    assert q["DRAW_EXECUTION_PASS"] is True or result.ok is False  # the plan was drawn (or the spill stopped it)
    assert q["VISUAL_QUALITY_PASS"] is False
    assert q["visual_quality"]["stray_specks"]["count"] >= 1 or q["visual_quality"]["spill_ratio"] > 0
    assert gate.spec["execution"]["visual_quality_status"] == "failed"


def test_a_stretched_drawing_fails_character_fidelity_even_if_everything_was_drawn() -> None:
    p = prepared()
    plan = geometry.plan_scene(p.scene.objects, CANVAS)
    info = doraemon.scene_info(p)
    img = raster.render_plan(plan)
    wide = img.crop((330, 0, 822, 648)).resize((1152, 648))  # the same picture stretched sideways
    fid = quality.character_fidelity(plan, wide, info)
    assert fid["status"] == "FAIL" and any("silhouette_aspect" in x or "landmark" in x for x in fid["problems"])
    assert quality.character_fidelity(plan, img, info)["status"] == "PASS"
    assert quality.character_fidelity(plan, img, None)["status"] == "NOT_APPLICABLE"


def test_a_misplaced_nose_is_a_landmark_error() -> None:
    p = prepared()
    plan = geometry.plan_scene(p.scene.objects, CANVAS)
    info = doraemon.scene_info(p)
    moved = [dataclasses.replace(o, points=tuple((x + 70, y + 26) for x, y in o.points),
                                 outline=tuple((x + 70, y + 26) for x, y in o.outline)) if o.id == "nose" else o for o in plan.objects]
    img = raster.render(CANVAS, moved)
    fid = quality.character_fidelity(plan, img, info)
    err = fid["landmark_errors"]["nose"]
    assert fid["status"] == "FAIL" and (err is None or err > quality.LANDMARK_ERROR_MAX)  # not found near its place = wrong


def test_canvas_safety_catches_cut_off_ink_dialogs_and_missing_screenshots(tmp_path: Path) -> None:
    p = prepared()
    plan = geometry.plan_scene(p.scene.objects, CANVAS)
    img = raster.render_plan(plan)
    shot = tmp_path / "ok.png"
    img.save(shot)
    assert quality.canvas_safety(plan, img, window_ok=True, screenshot=str(shot))["ok"]
    cut = img.copy()
    ImageDraw.Draw(cut).rectangle((0, 300, 40, 340), fill=(0, 0, 0))
    assert not quality.canvas_safety(plan, cut, window_ok=True, screenshot=str(shot))["ok"]
    assert "dialog" in " ".join(quality.canvas_safety(plan, img, window_ok=False, screenshot=str(shot))["problems"])
    assert "no screenshot" in " ".join(quality.canvas_safety(plan, img, window_ok=True, screenshot=None)["problems"])
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    assert not quality.canvas_safety(plan, img, window_ok=True, screenshot=str(bad))["screenshot_opens"]


def test_a_popup_is_recovered_or_stops_the_run_never_ignored(tmp_path: Path) -> None:
    popup = ToolError(ErrorCode.EXECUTION_FAILED, "a menu or dialog is open in Paint", retryable=True,
                      details={"recoverable": "popup"})
    plan, driver, result, gate, _ = run(tmp_path=tmp_path, ready_error=popup, recover=True)
    assert result.ok and driver.recovered == 1
    assert any(e.get("event") == "recovery" and e["ok"] for e in result.events)
    p = prepared()
    plan2 = geometry.plan_scene(p.scene.objects, CANVAS)
    stuck = FakePaint(plan2, ready_error=popup, recover=False)
    with pytest.raises(ToolError, match="dialog"):
        run_gated(plan2, stuck, request="ve Doraemon", directory=tmp_path, source={"kind": "template", "name": "doraemon"})


# --- reference measurement ------------------------------------------------------------------------------------------


def synthetic_reference(head_w: int = 230, head_h: int = 200, body=(10, 140, 230)) -> Image.Image:
    img = Image.new("RGB", (600, 700), "white")
    d = ImageDraw.Draw(img)
    cx = 300
    d.ellipse((cx - head_w // 2, 20, cx + head_w // 2, 20 + head_h), fill=body, outline=(0, 0, 0))
    d.ellipse((cx - 110, 200, cx + 110, 560), fill=body)
    d.rectangle((cx - 100, 215, cx + 100, 245), fill=(230, 0, 20))
    d.ellipse((cx - 22, 240, cx + 22, 284), fill=(255, 212, 0))
    return img


def test_a_reference_is_measured_and_clamped_to_the_allowed_ratios() -> None:
    ref = doraemon.measure_reference(synthetic_reference(), url="https://example.invalid/dora.png", retrieved_at="2026-10-16T10:00:00")
    assert ref["status"] == "AVAILABLE" and {"body", "collar", "bell", "head_aspect", "silhouette_aspect"} <= set(ref["measured"])
    assert ref["body"].upper().startswith("#0")
    p = prepared(reference=ref)
    assert p.reference["status"] == "AVAILABLE" and p.reference["url"] == "https://example.invalid/dora.png"
    assert all(c.ok for c in p.constraints)
    wild = doraemon.apply_reference(doraemon.Params(), {"head_aspect": 2.4, "silhouette_aspect": 1.4})
    assert wild.head_aspect == doraemon.HEAD_ASPECT[1] and 30 <= wild.hand_reach <= 36.5
    blank = doraemon.measure_reference(Image.new("RGB", (50, 50), "white"))
    assert blank["status"] == "UNUSABLE"
    info = doraemon.scene_info(p)
    assert info["colour_sources"]["body"] == "measured from the reference"
    assert info["colour_sources"]["belly"].startswith("baseline")


# --- template versions and learning ----------------------------------------------------------------------------------


def fake_quality(*, draw=True, visual=True, fid_ok=True, aspect=0.77, cov=0.99, err=0.002, specks=0):
    return {"DRAW_EXECUTION_PASS": draw, "VISUAL_QUALITY_PASS": visual and fid_ok and draw,
            "visual_quality": {"ok": visual, "worst_stroke_coverage": cov, "spill_ratio": 0.0, "stray_specks": {"count": specks}},
            "character_fidelity": {"ok": fid_ok, "landmark_errors": {"nose": err, "bell": err},
                                   "checks": [] if fid_ok else [{"name": "silhouette_aspect", "ok": False, "value": aspect,
                                                                 "expected": [0.72, 0.86]}]}}


def shot(tmp_path: Path) -> str:
    path = tmp_path / "s.png"
    Image.new("RGB", (10, 10), "white").save(path)
    return str(path)


def test_runs_without_a_screenshot_or_quality_are_not_learned_from(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "store")
    with pytest.raises(ValueError, match="screenshot"):
        store.record_run("doraemon", version="v", params={}, quality=fake_quality(), screenshot=str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="quality"):
        store.record_run("doraemon", version="v", params={}, quality={}, screenshot=shot(tmp_path))
    assert store.runs("doraemon") == []


def test_repeated_observed_defects_produce_a_candidate_that_is_not_used_until_promoted(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "store")
    base = doraemon.Params()
    assert store.params("doraemon") == base and store.propose_candidate("doraemon") is None
    wide = fake_quality(visual=True, fid_ok=False, aspect=0.9)
    assert defects_from_quality(wide) == ["silhouette_too_wide"]
    store.record_run("doraemon", version=doraemon.VERSION, params=base.to_dict(), quality=wide, screenshot=shot(tmp_path))
    assert store.propose_candidate("doraemon") is None  # one observation is not a pattern
    store.record_run("doraemon", version=doraemon.VERSION, params=base.to_dict(), quality=wide, screenshot=shot(tmp_path))
    cand = store.propose_candidate("doraemon")
    assert cand and cand["because"] == {"silhouette_too_wide": 2}
    assert cand["params"]["hand_reach"] == base.hand_reach - 1.0
    assert store.params("doraemon") == base  # still the old one: a candidate is not the best version


def test_promotion_needs_every_gate_and_never_lowers_the_score(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "store")
    wide = fake_quality(fid_ok=False, aspect=0.9)
    for _ in range(2):
        store.record_run("doraemon", version=doraemon.VERSION, params=doraemon.Params().to_dict(), quality=wide, screenshot=shot(tmp_path))
    cand = store.propose_candidate("doraemon")
    ok, why = store.promote("doraemon", cand["id"], quality=fake_quality(visual=False), constraints_ok=True)
    assert not ok and any("VISUAL_QUALITY_PASS" in w for w in why)
    ok, why = store.promote("doraemon", cand["id"], quality=fake_quality(), constraints_ok=False)
    assert not ok and any("constraints" in w for w in why)
    ok, why = store.promote("doraemon", cand["id"], quality=fake_quality(draw=False), constraints_ok=True)
    assert not ok and any("DRAW_EXECUTION_PASS" in w for w in why)
    assert store.best("doraemon") is None
    ok, why = store.promote("doraemon", cand["id"], quality=fake_quality(err=0.001), constraints_ok=True)
    assert ok and why == []
    best = store.best("doraemon")
    assert best["version"].startswith(doraemon.VERSION) and store.params("doraemon").hand_reach == cand["params"]["hand_reach"]
    # a second candidate with a lower score must not replace it
    for _ in range(2):
        store.record_run("doraemon", version=best["version"], params=best["params"], quality=wide, screenshot=shot(tmp_path))
    second = store.propose_candidate("doraemon")
    ok, why = store.promote("doraemon", second["id"], quality=fake_quality(cov=0.91, err=0.015), constraints_ok=True)
    assert not ok and any("lower" in w for w in why)
    assert store.best("doraemon")["version"] == best["version"]
    assert store.promote("doraemon", "nope", quality=fake_quality(), constraints_ok=True) == (False, ["no candidate nope"])


def test_a_corrupt_or_out_of_range_best_version_falls_back_to_the_built_in_template(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path / "store")
    d = tmp_path / "store" / "doraemon"
    d.mkdir(parents=True)
    (d / "best.json").write_text(json.dumps({"version": "x", "params": {"head_aspect": 3.0}}), encoding="utf-8")
    assert store.params("doraemon") == doraemon.Params()
    (d / "best.json").write_text("{not json", encoding="utf-8")
    assert store.params("doraemon") == doraemon.Params()


def test_the_other_templates_still_draw_as_before() -> None:
    plan = geometry.plan_scene(build_template("house", CANVAS), CANVAS)
    assert len(plan.objects) == 10 and plan.warnings == []
    assert len(geometry.plan_scene(build_template("doraemon_v1", CANVAS), CANVAS).objects) == 27
    assert len(geometry.plan_scene(build_template("doraemon", CANVAS), CANVAS).objects) == 28


def test_every_corner_of_the_allowed_parameter_range_still_satisfies_the_constraints() -> None:
    for head in (1.08, 1.14, 1.20):
        for reach in (30.0, 33.0, 36.5):
            for frac in (0.84, 0.87, 0.90):
                p = doraemon.prepare(CANVAS, doraemon.Params(head_aspect=head, hand_reach=reach, height_fraction=frac))
                assert all(c.ok for c in p.constraints), (head, reach, frac, [c.name for c in p.constraints if not c.ok])
                geometry.plan_scene(p.scene.objects, CANVAS)
