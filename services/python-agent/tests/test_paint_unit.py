"""Paint drawing engine, pure part: geometry, reference raster, pixel verification, calibration."""

from __future__ import annotations

import dataclasses
import math

import pytest
from PIL import Image, ImageDraw

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import calibration, geometry, raster, verify
from lga_worker.paint.model import Canvas, DrawingObject
from lga_worker.paint.templates import TEMPLATES, build_template
from lga_worker.windows.provider import Rect

CANVAS = Canvas(1152, 648)
SMALL = Canvas(800, 600)


def plan_of(name: str, canvas: Canvas = CANVAS) -> geometry.Plan:
    return geometry.plan_scene(build_template(name, canvas), canvas)


# --- geometry ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TEMPLATES))
@pytest.mark.parametrize("canvas", [CANVAS, SMALL, Canvas(1920, 1080), Canvas(600, 900)])
def test_templates_fit_every_canvas_without_warnings(name: str, canvas: Canvas) -> None:
    plan = geometry.plan_scene(build_template(name, canvas), canvas)
    assert plan.warnings == []
    m = plan.margin_px
    for obj in plan.objects:
        x0, y0, x1, y1 = obj.bounds
        assert x0 >= m - 0.5 and y0 >= m - 0.5, obj.id
        assert x1 <= canvas.width - m + 0.5 and y1 <= canvas.height - m + 0.5, obj.id
    ids = [o.id for o in plan.objects]
    assert len(ids) == len(set(ids))


def test_objects_are_drawn_back_to_front() -> None:
    plan = plan_of("doraemon")
    layers = [o.layer for o in plan.objects]
    assert layers == sorted(layers)
    order = [o.id for o in plan.objects]
    assert order.index("torso") < order.index("head") < order.index("face") < order.index("nose")


def test_stroke_width_follows_canvas_size_and_role() -> None:
    assert geometry.base_stroke_px(Canvas(1152, 648)) == 2
    assert geometry.base_stroke_px(Canvas(3840, 2160)) == 7
    # Paint offers only 1/3/5/8 px for shapes; the plan is made of widths that exist
    assert [geometry.stroke_px(CANVAS, w) for w in ("detail", "structure", "accent")] == [1, 3, 5]
    assert [geometry.stroke_px(Canvas(1920, 1080), w) for w in ("detail", "structure", "accent")] == [3, 5, 8]
    assert {geometry.snap_width(v) for v in (0.2, 2, 4, 6.5, 7, 40)} == {1, 3, 5, 8}
    with pytest.raises(ToolError):
        geometry.stroke_px(CANVAS, "fat")


def test_point_outside_the_drawable_area_is_rejected_with_the_allowed_range() -> None:
    # The Doraemon run drew from y=200 on a canvas that began at y=284: the engine refuses this class of mistake.
    bad = DrawingObject("head", "ellipse", ((10.0, 10.0), (400.0, 300.0)), fill="blue")
    with pytest.raises(ToolError) as caught:
        geometry.plan_scene([bad], CANVAS)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENT
    assert "head" in caught.value.message and "outside the drawable area" in caught.value.message


@pytest.mark.parametrize("obj,needle", [
    (DrawingObject("a", "line", ((100, 100), (200, 200)), fill="red"), "cannot have a fill"),
    (DrawingObject("a", "star", ((100, 100), (200, 200))), "unknown kind"),
    (DrawingObject("a", "polygon", ((100, 100), (200, 200))), "needs 3+ points"),
    (DrawingObject("a", "rect", ((100, 100), (200, 100)), fill="red"), "flat"),
    (DrawingObject("a", "rect", ((100, 100), (200, 200)), fill="chartreuse-ish"), "not '#RRGGBB'"),
    (DrawingObject("a", "rect", ((100, 100), (200, 200)), fill=None, stroke=None), "neither fill nor stroke"),
])
def test_invalid_objects_say_what_is_wrong(obj: DrawingObject, needle: str) -> None:
    with pytest.raises(ToolError) as caught:
        geometry.plan_scene([obj], CANVAS)
    assert needle in caught.value.message


def test_duplicate_ids_and_partial_same_layer_overlaps_are_flagged() -> None:
    a = DrawingObject("a", "rect", ((100, 100), (300, 300)), fill="red")
    with pytest.raises(ToolError, match="duplicate"):
        geometry.plan_scene([a, a], CANVAS)
    b = DrawingObject("b", "rect", ((200, 200), (400, 400)), fill="blue")
    plan = geometry.plan_scene([a, b], CANVAS)
    assert any("overlap partially" in w for w in plan.warnings)
    nested = DrawingObject("c", "rect", ((120, 120), (180, 180)), fill="blue")
    assert geometry.plan_scene([a, nested], CANVAS).warnings == []


def test_curve_passes_through_its_points_and_is_smooth() -> None:
    pts = [(0.0, 0.0), (50.0, 40.0), (100.0, 0.0)]
    out = geometry.catmull_rom(pts)
    for p in pts:
        assert min(((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5 for q in out) < 1e-6
    steps = [((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(out, out[1:], strict=False)]
    assert max(steps) <= geometry.CURVE_STEP_PX * 1.5


def test_interior_point_is_inside_for_every_closed_object() -> None:
    plan = plan_of("doraemon")
    for obj in plan.objects:
        if not obj.closed:
            assert geometry.interior_point(obj) is None
            continue
        x, y = geometry.interior_point(obj)
        assert geometry.point_in_polygon((x, y), obj.outline), obj.id


# --- reference raster and bucket fill safety ----------------------------------------------------------------


def test_bucket_fill_is_unsafe_on_an_unclosed_outline_and_safe_on_a_closed_one() -> None:
    # the failure of the Doraemon run: Fill clicked inside an outline that was never closed -> whole page blue
    opened = Image.new("RGB", (200, 200), "white")
    ImageDraw.Draw(opened).line([(50, 50), (150, 50), (150, 150), (50, 150), (50, 60)], fill="black", width=3)
    enclosed, _ = raster.bucket_fill_region(opened, (100, 100))
    assert enclosed is False
    closed = Image.new("RGB", (200, 200), "white")
    ImageDraw.Draw(closed).rectangle((50, 50, 150, 150), outline="black", width=3)
    enclosed, area = raster.bucket_fill_region(closed, (100, 100))
    assert enclosed is True and 90 * 90 < area < 100 * 100


def test_every_filled_template_region_is_bucket_safe_in_the_reference_raster() -> None:
    plan = plan_of("house")
    wall = plan.by_id("wall")
    seed = geometry.interior_point(wall)
    bare = raster.render(plan.canvas, [dataclasses.replace(wall, fill=None)])
    enclosed, area = raster.bucket_fill_region(bare, seed)
    x0, y0, x1, y1 = wall.bounds
    inner = (x1 - x0 - 2 * wall.stroke_px) * (y1 - y0 - 2 * wall.stroke_px)
    assert enclosed is True and 0.95 * inner < area <= (x1 - x0) * (y1 - y0)
    assert raster.render_plan(plan, up_to_layer=0).getpixel(seed) == wall.fill


# --- verification ---------------------------------------------------------------------------------------------


def test_a_perfect_canvas_verifies() -> None:
    plan = plan_of("doraemon")
    report = verify.compare(plan, raster.render_plan(plan))
    assert report.ok, report.summary()
    assert report.spill_ratio == 0.0
    assert all(o.ok for o in report.objects)


def test_flooding_the_page_blue_is_caught_as_spill() -> None:
    plan = plan_of("doraemon")
    flooded = Image.new("RGB", (CANVAS.width, CANVAS.height), (63, 72, 204))
    ImageDraw.Draw(flooded).ellipse((560, 357, 642, 392), outline="black", width=4)
    report = verify.compare(plan, flooded)
    assert not report.ok
    assert report.spill_ratio > 0.5
    assert "head" in report.failed_ids


def test_a_missing_object_is_named_and_only_it_fails() -> None:
    plan = plan_of("doraemon")
    without_nose = raster.render(plan.canvas, [o for o in plan.objects if o.id != "nose"])
    report = verify.compare(plan, without_nose)
    assert not report.ok
    assert report.failed_ids == ["nose"]
    assert "fill" in next(o for o in report.objects if o.id == "nose").detail


def test_wrong_fill_colour_is_detected() -> None:
    plan = plan_of("house")
    wrong = [dataclasses.replace(o, fill=(0, 200, 0)) if o.id == "roof" else o for o in plan.objects]
    report = verify.compare(plan, raster.render(plan.canvas, wrong))
    assert report.failed_ids == ["roof"]


def test_one_pixel_drift_passes_and_a_large_shift_fails() -> None:
    plan = plan_of("house")
    image = raster.render_plan(plan)
    shifted_1 = Image.new("RGB", image.size, "white")
    shifted_1.paste(image, (1, 1))
    assert verify.compare(plan, shifted_1).ok
    shifted_20 = Image.new("RGB", image.size, "white")
    shifted_20.paste(image, (20, 12))
    assert not verify.compare(plan, shifted_20).ok


def test_zoomed_out_canvas_gets_a_wider_tolerance() -> None:
    assert verify.tolerance_for_scale(1.0) == 2
    assert verify.tolerance_for_scale(0.5) == 3
    assert verify.tolerance_for_scale(2.0) == 2


def test_wrong_capture_size_asks_for_recalibration() -> None:
    plan = plan_of("house")
    with pytest.raises(ToolError, match="re-calibrate"):
        verify.compare(plan, Image.new("RGB", (100, 100), "white"))


def test_partial_layers_verify_only_what_should_be_drawn_so_far() -> None:
    plan = plan_of("house")
    half = raster.render_plan(plan, up_to_layer=1)
    assert verify.compare(plan, half, up_to_layer=1).ok
    assert not verify.compare(plan, half).ok  # door and windows are missing


# --- calibration ----------------------------------------------------------------------------------------------


def test_paint_status_texts_parse() -> None:
    assert calibration.parse_canvas_size("1152 × 648px") == Canvas(1152, 648)
    assert calibration.parse_canvas_size("800x600px") == Canvas(800, 600)
    assert calibration.parse_zoom("100%") == 1.0
    assert calibration.parse_zoom("37,5 %") == 0.375
    with pytest.raises(ToolError):
        calibration.parse_canvas_size("45 px")


def test_transform_round_trips_and_checks_the_rectangle() -> None:
    t = calibration.CanvasTransform.from_rect(CANVAS, Rect(384, 284, 384 + 1152, 284 + 648), zoom_hint=1.0)
    assert t.scale == 1.0
    assert t.to_screen(0, 0) == (384, 284)
    assert t.to_screen(1151, 647) == (1535, 931)
    assert t.to_canvas(384 + 576, 284 + 324) == (576.0, 324.0)
    assert t.contains_screen(400, 300) and not t.contains_screen(400, 200)  # y=200 is above the canvas
    with pytest.raises(ToolError, match="does not match"):
        calibration.CanvasTransform.from_rect(CANVAS, Rect(0, 0, 1152, 500))
    with pytest.raises(ToolError, match="implies zoom"):
        calibration.CanvasTransform.from_rect(CANVAS, Rect(0, 0, 576, 324), zoom_hint=1.0)
    half = calibration.CanvasTransform.from_rect(CANVAS, Rect(100, 100, 100 + 576, 100 + 324))
    assert half.scale == 0.5 and half.to_screen(100, 100) == (150, 150)


def test_blank_canvas_is_found_on_a_synthetic_screenshot() -> None:
    shot = Image.new("RGB", (1920, 1080), (238, 244, 232))
    draw = ImageDraw.Draw(shot)
    draw.rectangle((0, 0, 1919, 170), fill=(252, 246, 235))
    for i in range(10):  # white colour swatches in the ribbon must not confuse the scan
        draw.ellipse((790 + 24 * i, 100, 806 + 24 * i, 116), fill=(255, 255, 255))
    draw.rectangle((384, 284, 384 + 1152 - 1, 284 + 648 - 1), fill=(255, 255, 255))
    t = calibration.CanvasTransform.from_rect(CANVAS, Rect(384, 284, 384 + 1152, 284 + 648))
    assert calibration.find_blank_canvas(shot, CANVAS, 1.0) == (384, 284)
    assert calibration.cross_check(t, shot, (0, 0)) == 0
    moved = calibration.CanvasTransform.from_rect(CANVAS, Rect(390, 280, 390 + 1152, 280 + 648))
    assert calibration.cross_check(moved, shot, (0, 0)) == 6
    flooded = shot.copy()
    ImageDraw.Draw(flooded).rectangle((384, 284, 1535, 931), fill=(63, 72, 204))
    assert calibration.find_blank_canvas(flooded, CANVAS, 1.0) is None


# --- engine (with a fake Paint) --------------------------------------------------------------------------------


class FakePaint:
    """Draws on a PIL image with the reference rasteriser; `skip_first` / `flood_on` inject faults."""

    scale = 1.0

    def __init__(self, canvas: Canvas, *, skip_first: frozenset[str] = frozenset(), flood_on: str | None = None):
        self.image = Image.new("RGB", (canvas.width, canvas.height), "white")
        self.skip_first = set(skip_first)
        self.flood_on = flood_on
        self.calls: list[str] = []

    def check_ready(self) -> None:
        pass

    def draw(self, obj) -> None:
        self.calls.append(obj.id)
        if obj.id == self.flood_on:
            ImageDraw.Draw(self.image).rectangle((0, 0, *self.image.size), fill=(63, 72, 204))
            return
        if obj.id in self.skip_first:
            self.skip_first.discard(obj.id)
            return
        raster._draw_object(ImageDraw.Draw(self.image), obj)

    def capture(self) -> Image.Image:
        return self.image.copy()


def test_engine_draws_everything_once_when_nothing_goes_wrong() -> None:
    from lga_worker.paint import engine

    plan = plan_of("doraemon")
    fake = FakePaint(CANVAS)
    result = engine.run_plan(plan, fake)
    assert result.ok and result.redraws == {}
    assert sorted(fake.calls) == sorted(o.id for o in plan.objects)
    assert [fake.calls.index(i) for i in ("torso", "head", "nose")] == sorted(fake.calls.index(i) for i in ("torso", "head", "nose"))


def test_engine_redraws_only_the_object_that_was_missed() -> None:
    from lga_worker.paint import engine

    plan = plan_of("doraemon")
    fake = FakePaint(CANVAS, skip_first=frozenset({"nose"}))
    result = engine.run_plan(plan, fake)
    assert result.ok
    assert result.redraws["nose"] == 1
    assert set(result.redraws) <= {"nose", "mouth_line", "mouth"}  # the nose and what is drawn after it and touches it
    assert fake.calls.count("nose") == 2 and fake.calls.count("head") == 1


def test_engine_stops_instead_of_piling_on_when_the_page_floods() -> None:
    from lga_worker.paint import engine

    plan = plan_of("doraemon")
    fake = FakePaint(CANVAS, flood_on="head")
    result = engine.run_plan(plan, fake)
    assert not result.ok
    assert result.final.spill_ratio > 0.5
    assert len(fake.calls) < len(plan.objects)  # it stopped at the stage that broke, no repair on top of a flood
    assert all(fake.calls.count(i) == 1 for i in fake.calls)
    assert "nose" not in fake.calls  # later layers were never drawn onto the flooded page


def test_engine_honours_cancellation() -> None:
    from lga_worker.paint import engine

    plan = plan_of("house")
    fake = FakePaint(CANVAS)
    with pytest.raises(ToolError) as caught:
        engine.run_plan(plan, fake, should_stop=lambda: len(fake.calls) >= 2)
    assert caught.value.code == ErrorCode.CANCELLED
    assert len(fake.calls) == 2


def test_repair_redraws_what_was_covered_by_the_failed_object_not_just_the_failed_one() -> None:
    from lga_worker.paint import engine

    plan = plan_of("doraemon")
    todo = [o.id for o in engine.repair_set(plan, ["torso"])]
    assert todo[0] == "torso"
    assert {"arm_l", "belly", "pocket", "head"} <= set(todo)  # drawn later and overlapping: must go back on top
    assert "foot_l" not in todo  # drawn earlier: untouched
    assert engine.repair_set(plan, ["whisker_l0"]) == [plan.by_id("whisker_l0")] or "mouth" in [o.id for o in engine.repair_set(plan, ["whisker_l0"])]


def test_a_missing_curve_does_not_make_the_object_it_crosses_look_broken() -> None:
    # Live Doraemon run: the smile came out as a straight line, and the collar was reported as failing because the
    # smile's expected black pixels lay on the collar's outline.
    plan = plan_of("doraemon")
    without_mouth = raster.render(plan.canvas, [o for o in plan.objects if o.id != "mouth"])
    report = verify.compare(plan, without_mouth)
    assert report.failed_ids == ["mouth"]


# --- picture tracing (paint.draw_image) --------------------------------------------------------------------


def _synthetic_picture() -> Image.Image:
    """A grey card holding a red disc that holds a white dot, beside a yellow bar, on a blue field."""
    img = Image.new("RGB", (320, 220), (63, 72, 204))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 210, 200), fill=(195, 195, 195))
    d.ellipse((60, 50, 170, 160), fill=(237, 28, 36))
    d.ellipse((95, 85, 135, 125), fill=(255, 255, 255))
    d.rectangle((235, 40, 290, 180), fill=(255, 242, 0))
    return img


def test_picture_is_traced_into_nested_polygons_that_plan_and_verify() -> None:
    from lga_worker.paint import vectorize

    picture = _synthetic_picture()
    traced = vectorize.vectorize(picture, CANVAS, detail="normal")
    fills = {o.fill for o in traced.objects}
    assert {"#3F48CC", "#C3C3C3", "#ED1C24", "#FFFFFF", "#FFF200"} <= fills, fills
    assert all(o.kind == "polygon" and o.stroke == "#322823" and len(o.points) >= 3 for o in traced.objects)
    flat = vectorize.vectorize(picture, CANVAS, detail="normal", outline=False)
    assert all(o.stroke is None for o in flat.objects)
    assert traced.objects[0].id == "base" and traced.objects[0].layer == 0
    depth = {o.fill: o.layer for o in traced.objects if o.id != "base"}
    assert depth["#FFFFFF"] > depth["#ED1C24"] > depth["#C3C3C3"] >= 1  # card, then disc, then dot

    plan = geometry.plan_scene(traced.objects, CANVAS, check_overlaps=False)
    rendered = raster.render_plan(plan)
    assert vectorize.fidelity(picture, rendered, CANVAS)["ssim"] > 0.9
    assert verify.compare(plan, rendered).ok

    # polygons are in the safe area and no two consecutive vertices are close enough to read as a double click
    for obj in plan.objects:
        pts = obj.points
        assert all(math.dist(a, b) >= 4.0 for a, b in zip(pts, pts[1:], strict=False)), obj.id


def test_tracing_is_deterministic_and_respects_the_polygon_budget() -> None:
    from lga_worker.paint import vectorize

    picture = _synthetic_picture()
    a = vectorize.vectorize(picture, CANVAS, detail="low")
    b = vectorize.vectorize(picture, CANVAS, detail="low")
    assert [(o.fill, o.points) for o in a.objects] == [(o.fill, o.points) for o in b.objects]
    small = vectorize.vectorize(picture, CANVAS, detail="high", max_regions=4)
    assert len(small.objects) <= 5  # four regions plus the backing polygon
    with pytest.raises(ToolError):
        vectorize.vectorize(picture, CANVAS, detail="bogus")


def test_picture_fits_inside_the_canvas_with_its_aspect_ratio() -> None:
    from lga_worker.paint import vectorize

    left, top, w, h = vectorize.fit_box((480, 640), CANVAS)  # a portrait photo on a landscape canvas
    assert abs(w / h - 480 / 640) < 1e-9
    assert left > 0 and top > 0 and left + w < 1000 and top + h < CANVAS.unit_height
    assert abs(left - (1000 - (left + w))) < 1e-6  # centred


def test_polygon_is_started_at_its_longest_edge() -> None:
    from lga_worker.paint.driver import PaintDriver

    ring = [(100, 100), (104, 100), (106, 160), (180, 170), (200, 120)]  # edges: 4, 60, 75, 54, 100
    ordered = PaintDriver._polygon_start_at_longest_edge(ring)
    assert math.dist(ordered[0], ordered[1]) == max(
        math.dist(a, b) for a, b in zip(ring, ring[1:] + ring[:1], strict=True))
    assert sorted(ordered) == sorted(ring)  # the same polygon, only started elsewhere


class _StubbornPaint(FakePaint):
    """Never draws the listed objects (as Paint ignores a polygon whose first drag is too short)."""

    def __init__(self, canvas: Canvas, never: set[str]):
        super().__init__(canvas)
        self.never = never

    def draw(self, obj) -> None:
        self.calls.append(obj.id)
        if obj.id not in self.never:
            raster._draw_object(ImageDraw.Draw(self.image), obj)


def test_traced_pictures_tolerate_a_few_unmatched_regions_but_exact_drawings_do_not() -> None:
    from lga_worker.paint import engine

    plan = plan_of("doraemon")
    stubborn = {"pupil_l", "pupil_r"}
    strict = engine.run_plan(plan, _StubbornPaint(CANVAS, stubborn))
    assert not strict.ok and set(strict.final.failed_ids) == stubborn
    tolerant = engine.run_plan(plan, _StubbornPaint(CANVAS, stubborn), tolerance=0.10)
    assert tolerant.ok and set(tolerant.final.failed_ids) == stubborn
    too_many = engine.run_plan(plan, _StubbornPaint(CANVAS, {"head", "face", "torso", "belly"}), tolerance=0.10)
    assert not too_many.ok


def test_repair_only_redraws_objects_that_really_overlap() -> None:
    from lga_worker.paint import engine

    objs = [DrawingObject("a", "rect", ((100, 100), (200, 200)), fill="red", layer=0),
            DrawingObject("b", "rect", ((150, 150), (260, 260)), fill="blue", layer=1),    # overlaps a
            DrawingObject("c", "rect", ((700, 100), (800, 200)), fill="green", layer=1),   # nowhere near
            DrawingObject("d", "rect", ((205, 100), (240, 140)), fill="gold", layer=1)]    # boxes touch, pixels do not
    plan = geometry.plan_scene(objs, CANVAS, check_overlaps=False)
    ids = [o.id for o in engine.repair_set(plan, ["a"])]
    assert ids == ["a", "b"], ids


def test_the_default_detail_is_the_maximum() -> None:
    from lga_worker.paint import vectorize

    assert vectorize.DEFAULT_DETAIL == "max"
    levels = [vectorize.DETAIL[k]["regions"] for k in ("low", "normal", "high", "max")]
    assert levels == sorted(levels) and len(set(levels)) == 4
    picture = _synthetic_picture()
    assert len(vectorize.vectorize(picture, CANVAS).objects) >= len(vectorize.vectorize(picture, CANVAS, detail="low").objects)


# --- coloured pencil sketch (apps that cannot fill) ----------------------------------------------------------


def test_sketch_uses_the_colours_of_the_picture_for_every_stroke() -> None:
    from lga_worker.paint import sketch

    picture = _synthetic_picture()
    result = sketch.sketch(picture, CANVAS, detail="normal", colors=8)
    kinds = {s.kind for s in result.strokes}
    assert kinds == {"contour", "hatch"}
    assert all(s.closed for s in result.strokes if s.kind == "contour")
    assert all(len(s.points) == 2 for s in result.strokes if s.kind == "hatch")
    # the card is light grey, the disc red, the bar yellow, the field blue: strokes carry those colours, not black
    rgb = [sketch.hex_to_rgb(c) for c in result.palette]
    assert any(r > 180 and g < 90 and b < 90 for r, g, b in rgb), result.palette  # red
    assert any(b > 150 and r < 120 for r, g, b in rgb), result.palette  # blue
    assert not all(max(c) < 40 for c in rgb)
    # shading follows tone: the dark blue field is hatched, the light card (195) is not
    hatched = {s.color for s in result.strokes if s.kind == "hatch"}
    assert any(sketch.hex_to_rgb(c)[2] > 150 for c in hatched)
    assert sketch.render(result.strokes, CANVAS).size == (CANVAS.width, CANVAS.height)


def test_sketch_respects_its_stroke_budget_and_aci_fallback_is_sane() -> None:
    from lga_worker.paint import sketch

    big = sketch.sketch(_synthetic_picture(), CANVAS, detail="normal", colors=8, max_strokes=10_000)
    small = sketch.sketch(_synthetic_picture(), CANVAS, detail="normal", colors=8, max_strokes=60)
    assert len(small.strokes) < len(big.strokes)
    assert small.stats["hatch_spacing_factor"] > 1.0
    assert sketch.aci_nearest((255, 0, 0)) == 1 and sketch.aci_nearest((0, 0, 255)) == 5
    assert sketch.aci_nearest((254, 254, 254)) in (7, 255)


def test_sketch_plan_is_drawn_by_the_paint_engine_as_one_brush_stroke_each() -> None:
    from lga_worker.paint import engine, sketch

    picture = _synthetic_picture()
    result = sketch.sketch(picture, CANVAS, detail="low", colors=6, max_strokes=300)
    objects = [DrawingObject(f"s{n}", "path", tuple(s.points) + ((s.points[0],) if s.closed else ()), fill=None,
                             stroke=s.color, weight="detail", layer=0) for n, s in enumerate(result.strokes)]
    plan = geometry.plan_scene(objects, CANVAS, check_overlaps=False)
    fake = FakePaint(CANVAS)
    done = engine.run_plan(plan, fake, tolerance=0.1)
    assert done.drawn == len(plan.objects) and len(fake.calls) == len(plan.objects)
    agreement = verify.ink_agreement(plan, fake.capture())
    assert agreement["coverage"] > 0.99 and agreement["stray"] < 0.01
    assert verify.ink_agreement(plan, Image.new("RGB", (CANVAS.width, CANVAS.height), "white"))["coverage"] < 0.01


def test_repair_never_draws_objects_of_later_stages() -> None:
    plan = plan_of("doraemon")
    early = {o.id for o in plan.objects if o.layer <= 3}
    from lga_worker.paint import engine as paint_engine

    todo = {o.id for o in paint_engine.repair_set(plan, ["torso"], early)}
    assert "torso" in todo and todo <= early
