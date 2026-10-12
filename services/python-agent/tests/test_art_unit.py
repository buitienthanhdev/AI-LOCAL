"""The drawing brain, pure part: SVG parsing, the Scene, conversion to Paint objects and pencil strokes."""

from __future__ import annotations

import json

import pytest
from lga_common.config import Config

from lga_worker.art import capabilities, convert, export
from lga_worker.art.scene import Scene, Shape, render, simplify
from lga_worker.art.svg import parse_svg
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import geometry
from lga_worker.paint.model import Canvas
from lga_worker.server import build_registry

PAINT = Canvas(1152, 648)

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
<rect width="200" height="100" fill="#cde"/>
<circle cx="100" cy="50" r="30" fill="#ffd700" stroke="#000" stroke-width="2"/>
<path d="M20 90 q 30 -40 60 0 t 60 0" fill="none" stroke="#00f" stroke-width="3"/>
<g transform="translate(150 40) rotate(0)"><polygon points="0,0 40,0 20,40" style="fill:rgb(238,34,34);opacity:0.5"/></g>
<text x="5" y="5">hi</text></svg>"""


def test_svg_shapes_colours_and_background() -> None:
    scene = parse_svg(SVG)
    assert scene.background == (204, 221, 238)  # the full-frame rectangle became the sheet background
    circle = scene.shapes[0]
    assert circle.hint == "ellipse" and circle.fill == (255, 215, 0) and circle.stroke == (0, 0, 0)
    waves = [s for s in scene.shapes if s.stroke == (0, 0, 255)]
    assert waves and all(w.fill is None and not w.closed for w in waves)  # the wave sticks out of the frame: cut in pieces
    assert any(s.fill == (246, 144, 144) for s in scene.shapes)  # 50 % red over white
    assert any("text" in n for n in scene.notes)
    xs = [p[0] for s in scene.shapes for p in s.points]
    assert min(xs) >= -0.001 and max(xs) <= scene.width + 0.001  # fitted into the sheet


def test_svg_gradient_is_reduced_to_its_mean_colour() -> None:
    scene = parse_svg('<svg viewBox="0 0 10 10"><defs><linearGradient id="g"><stop offset="0" stop-color="#000"/>'
                      '<stop offset="1" stop-color="#fff"/></linearGradient></defs><rect width="5" height="5" fill="url(#g)"/></svg>')
    assert scene.shapes[0].fill in {(127, 127, 127), (128, 128, 128)}


def test_svg_arc_and_relative_commands_close_a_ring() -> None:
    scene = parse_svg('<svg viewBox="0 0 100 100"><path d="M10 50 a40 40 0 1 1 80 0 a40 40 0 1 1 -80 0 z" fill="red"/></svg>')
    s = scene.shapes[0]
    assert s.closed and len(s.points) > 20
    xs = [p[0] for p in s.points]
    ys = [p[1] for p in s.points]
    assert abs((max(xs) - min(xs)) - (max(ys) - min(ys))) < 3  # a circle, not an ellipse


@pytest.mark.parametrize("text", ["not svg", "<svg><rect", "<svg viewBox='0 0 1 1'></svg>", "<svg><text>x</text></svg>"])
def test_svg_rejects_what_cannot_be_drawn(text: str) -> None:
    with pytest.raises(ToolError) as info:
        parse_svg(text)
    assert info.value.code == ErrorCode.INVALID_ARGUMENT


def test_simplify_keeps_the_corners_of_a_ring() -> None:
    ring = [(0.0, 0.0), (5.0, 0.01), (10.0, 0.0), (10.0, 5.0), (10.0, 10.0), (5.0, 10.0), (0.0, 10.0), (0.0, 5.0)]
    assert len(simplify(ring, 0.5, True)) == 4


def test_export_roundtrips_through_svg() -> None:
    scene = parse_svg(SVG)
    again = parse_svg(export.to_svg(scene))
    assert again.background == (204, 221, 238)  # first rect is the sheet background again
    assert len(again.shapes) == len(scene.shapes)
    img = render(scene, 320)
    assert img.size == (320, 180)


def test_paint_objects_are_plannable_and_layers_never_overlap() -> None:
    scene = parse_svg(SVG)
    objects, report = convert.to_paint_objects(scene, PAINT)
    assert {o.kind for o in objects} >= {"ellipse", "path", "polygon"}
    plan = geometry.plan_scene(objects, PAINT, check_overlaps=False)
    assert report["colors"] <= 30 + 1
    by_layer: dict[int, list] = {}
    for o in plan.objects:
        by_layer.setdefault(o.layer, []).append(o)
    from lga_worker.paint.engine import _overlap

    for layer, items in by_layer.items():
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                assert not _overlap(plan, a, b), (layer, a.id, b.id)


def test_paint_objects_respect_the_mouse_limits() -> None:
    import math

    ring = tuple((500 + 300 * math.cos(t / 100 * math.tau), 280 + 200 * math.sin(t / 100 * math.tau)) for t in range(100))
    scene = Scene([Shape(ring, True, (200, 30, 30), None, 1.0)])
    objects, _ = convert.to_paint_objects(scene, PAINT)
    assert len(objects[0].points) <= 26
    px = PAINT.px_per_unit
    pts = [(x * px, y * px) for x, y in objects[0].points]
    assert min(math.dist(a, b) for a, b in zip(pts, pts[1:])) >= 5.0


def test_pencil_strokes_hide_what_a_later_shape_covers() -> None:
    low = Shape(((0, 0), (100, 0), (100, 100), (0, 100)), True, (200, 0, 0), None, 1.0)
    high = Shape(((0, 0), (100, 0), (100, 100), (0, 100)), True, (0, 0, 200), None, 1.0)
    strokes = convert.to_pencil_strokes(Scene([low, high]))
    colours = {rgb for _, rgb, _ in strokes}
    assert colours == {(0, 0, 200)}  # the red square is fully hidden: not one red line
    only_low = convert.to_pencil_strokes(Scene([low]))
    assert {rgb for _, rgb, _ in only_low} == {(200, 0, 0)}
    assert any(not closed and len(pts) == 2 for pts, _, closed in only_low)  # hatching


def test_stroke_only_shapes_are_drawn_as_they_are() -> None:
    line = Shape(((0, 0), (50, 50)), False, None, (10, 200, 10), 1.0)
    strokes = convert.to_pencil_strokes(Scene([line]))
    assert len(strokes) == 1 and strokes[0][1] == (10, 200, 10)


def test_templates_become_scenes() -> None:
    scene = convert.scene_from_template("doraemon")
    assert len(scene.shapes) > 10 and scene.stats()["filled"] > 5
    assert any(s.hint == "ellipse" for s in scene.shapes)


def test_application_names_resolve() -> None:
    for name, key in {"mspaint": "paint", "Microsoft Paint": "paint", "AutoCAD 2023": "autocad", "PowerPoint": "powerpoint",
                      "Word": "word", "Excel": "excel", "Inkscape": "file", None: "paint"}.items():
        profile = capabilities.resolve(name)
        assert profile is not None and profile.key == key, name
    assert capabilities.resolve("nonexistent-app") is None
    cad = capabilities.resolve("autocad")
    assert cad is not None
    assert capabilities.style_for(cad, None)[0] == "sketch"
    assert capabilities.style_for(cad, "fill")[1] is not None  # says it cannot fill
    paint = capabilities.resolve("paint")
    assert paint is not None and capabilities.style_for(paint, "auto") == ("fill", None)
    assert capabilities.style_for(paint, "sketch")[0] == "sketch"


def test_contract_has_a_handler(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    contract = json.loads((config.path("paths.tools_dir") / "art.json").read_text("utf-8"))
    names = {t["name"] for t in contract["tools"]}
    assert names == {"art.draw", "art.brief", "art.paint"}
    assert names <= set(registry.available_tools())
    spec = registry.specs["art.draw"]
    assert spec.input_schema.get("additionalProperties") is False
    assert spec.verification == "self"


def _square(x: float, y: float, size: float = 40.0, fill: tuple[int, int, int] = (200, 100, 0)) -> Shape:
    return Shape(((x, y), (x + size, y), (x + size, y + size), (x, y + size)), True, fill, None, 1.0)


def test_a_part_that_floats_just_off_the_body_is_found_and_attached() -> None:
    body = [_square(i * 30.0, 0.0, 40.0) for i in range(8)]  # a connected row of eight squares
    ear = _square(100.0, -50.0, 40.0)  # 10 units above the row: a near miss
    scene = Scene([*body, ear])
    groups = convert.detached_groups(scene)
    assert len(groups) == 1 and 5 < groups[0]["gap"] < 12
    assert convert.attach_detached(scene) == 1
    assert convert.detached_groups(scene) == []
    moved = scene.shapes[-1]  # the ear now overlaps the row
    assert max(p[1] for p in moved.points) > 0


def test_a_separate_object_far_away_is_left_alone() -> None:
    body = [_square(i * 30.0, 0.0, 40.0) for i in range(8)]
    sun = _square(900.0, 400.0, 60.0)
    assert convert.detached_groups(Scene([*body, sun])) == []
