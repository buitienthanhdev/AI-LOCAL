"""Art intelligence, pure part: modes, brief, knowledge, state machine, stylize, engine fill safety / recovery."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from lga_worker.art import brief as brief_mod
from lga_worker.art import modes, reference, stylize
from lga_worker.art.knowledge import ArtKnowledgeStore
from lga_worker.art.modes import SPECS, Mode
from lga_worker.art.scene import render
from lga_worker.art.state import ArtRun, IllegalTransition, State
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import engine, geometry
from lga_worker.paint.model import Canvas, DrawingObject

CANVAS = Canvas(800, 450)


def _brief(text: str, **kw):
    return brief_mod.build_brief(text, store=ArtKnowledgeStore(), **kw)


def test_named_work_is_identified_as_a_reproduction_in_oil() -> None:
    b = _brief("Vẽ The Starry Night của Vincent van Gogh")
    assert b.resolution == "IDENTIFIED" and b.intent == "REPRODUCTION" and "A" in b.kinds
    assert b.mode == Mode.OIL_PAINT_SIMULATION.value and b.needs_reference
    assert b.artwork and b.artwork["artist"] == "Vincent van Gogh" and "cypress" in " ".join(b.artwork["signature"])
    assert b.verification == "BUILTIN_KNOWLEDGE"  # nothing was fetched: not claimed as verified


def test_vietnamese_title_and_cartoon_adaptation() -> None:
    b = _brief("vẽ Đêm đầy sao của Van Gogh theo phong cách hoạt hình")
    assert b.resolution == "IDENTIFIED" and b.intent == "ADAPTATION"
    assert b.mode == Mode.FLAT_COLOR.value


def test_ambiguous_title_asks_instead_of_guessing() -> None:
    b = _brief("vẽ The Scream")
    assert b.resolution == "AMBIGUOUS" and b.questions and b.verification == "UNVERIFIED"


def test_user_words_beat_the_planner_mode() -> None:
    b = _brief("vẽ chân dung bằng bút chì, không tô màu", mode="FLAT_COLOR")
    assert b.mode == Mode.PENCIL_SKETCH.value and b.no_colour
    assert SPECS[Mode.PENCIL_SKETCH].colour == "grey"
    assert _brief("vẽ tranh trắng đen một ngôi nhà").mode == Mode.BLACK_WHITE.value
    assert SPECS[_brief("vẽ The Starry Night không tô màu").mode_enum].colour in ("none", "grey")


def test_invention_and_media() -> None:
    assert _brief("Vẽ một con rồng trên đỉnh núi").kinds == ["C"]
    b = _brief("Vẽ một bức tranh phong cảnh theo phong cách sơn mài Việt Nam")
    assert b.mode == Mode.LACQUER_ART_SIMULATION.value and "D" in b.kinds and "E" in b.kinds
    assert "cannot be done digitally" in " ".join(b.notes)
    assert _brief("vẽ phong cảnh màu nước").mode == Mode.WATERCOLOR_SIMULATION.value
    assert _brief("pixel art con mèo").mode == Mode.PIXEL_ART.value


def test_unknown_painting_by_known_artist_is_unverified() -> None:
    b = _brief("vẽ bức tranh Hoa sen của Van Gogh")
    assert b.resolution == "UNKNOWN_ARTWORK" and b.verification == "UNVERIFIED"


def test_normalize_keeps_vietnamese_d() -> None:
    assert modes.normalize("Đêm đầy sao — Bút chì!") == "dem day sao but chi"


def test_state_machine_refuses_unverified_completion() -> None:
    run = ArtRun()
    run.go(State.CLASSIFIED)
    with pytest.raises(IllegalTransition):
        run.go(State.COMPLETED)
    for s in (State.REFERENCE_RESOLVED, State.ART_ANALYZED, State.MEDIUM_SELECTED, State.RENDERING_MODE_SELECTED,
              State.CANVAS_CALIBRATED, State.PLAN_CREATED, State.PLAN_VALIDATED, State.EXECUTING):
        run.go(s)
    run.go(State.PAUSED)
    run.go(State.EXECUTING)
    run.go(State.VERIFYING)
    run.go(State.CORRECTING)
    run.go(State.VERIFYING)
    run.go(State.COMPLETED)
    with pytest.raises(IllegalTransition):
        run.go(State.EXECUTING)


@pytest.fixture(scope="module")
def picture() -> Image.Image:
    im = Image.new("RGB", (320, 200), (40, 60, 160))
    d = ImageDraw.Draw(im)
    d.ellipse((200, 20, 280, 100), fill=(250, 220, 60))
    d.rectangle((0, 140, 320, 200), fill=(20, 20, 30))
    d.polygon([(60, 140), (90, 40), (120, 140)], fill=(30, 90, 50))
    return im


@pytest.mark.parametrize("mode", list(Mode))
def test_every_mode_renders_and_keeps_its_promise(picture: Image.Image, mode: Mode) -> None:
    styled = stylize.stylize(picture, mode, detail="normal", max_strokes=400)
    assert styled.scene.shapes
    colours = styled.scene.colors()
    spread = max((max(c) - min(c)) for c in colours) if colours else 0
    spec = SPECS[mode]
    if spec.colour in ("grey", "none"):
        assert spread <= 12, (mode, spread)  # no hue in a pencil / ink / grey picture
    if spec.colour == "none":
        assert all(max(c) < 40 or min(c) > 215 for c in colours), mode
    assert render(styled.scene, 160).size[0] == 160


def test_watercolour_leaves_paper_and_lacquer_uses_its_palette(picture: Image.Image) -> None:
    w = render(stylize.stylize(picture, Mode.WATERCOLOR_SIMULATION, detail="normal").scene, 160)
    assert w.getextrema()[0][1] == 255  # something stays paper white
    lq = stylize.stylize(picture, Mode.LACQUER_ART_SIMULATION, detail="normal").scene
    assert set(lq.colors()) <= set(stylize.LACQUER.values())


class FakeDriver:
    """Draws on a PIL image; a bucket fill floods from the seed like Paint's."""

    def __init__(self, plan, *, fail_ready_once=False):
        self.plan, self.scale = plan, 1.0
        self.image = Image.new("RGB", (plan.canvas.width, plan.canvas.height), (255, 255, 255))
        self.history: list[Image.Image] = []
        self.fail_ready_once, self.recovered = fail_ready_once, 0

    def check_ready(self):
        if self.fail_ready_once:
            self.fail_ready_once = False
            raise ToolError(ErrorCode.EXECUTION_FAILED, "Paint lost the foreground", retryable=True,
                            details={"recoverable": "focus"})

    def recover(self, error):
        self.recovered += 1
        return True

    def draw(self, obj):
        self.history.append(self.image.copy())
        if obj.kind == "fill":
            ImageDraw.floodfill(self.image, (round(obj.points[0][0]), round(obj.points[0][1])), obj.fill, thresh=0)
            return
        d = ImageDraw.Draw(self.image)
        pts = list(obj.outline)
        if obj.fill is not None and obj.closed:
            d.polygon(pts, fill=obj.fill)
        if obj.stroke is not None:
            d.line([*pts, pts[0]] if obj.closed else pts, fill=obj.stroke, width=obj.stroke_px)

    def undo(self):
        self.image = self.history.pop()
        return True

    def capture(self):
        return self.image.copy()


def _scene() -> list[DrawingObject]:
    return [
        DrawingObject("box", "rect", ((100, 80), (300, 220)), None, "#000000", "structure", 0),
        DrawingObject("boxfill", "fill", ((200, 150),), "#f1c40f", None, "structure", 1, region="box"),
        DrawingObject("open", "polyline", ((400, 80), (600, 80), (600, 220), (400, 220)), None, "#000000", "structure", 0),
        DrawingObject("openfill", "fill", ((500, 150),), "#e74c3c", None, "structure", 1),
    ]


def test_bucket_fill_only_where_the_region_is_closed() -> None:
    plan = geometry.plan_scene(_scene(), CANVAS, margin_fraction=0.01, check_overlaps=False)
    driver = FakeDriver(plan)
    result = engine.run_plan(plan, driver)
    assert result.ok and result.skipped_fills == ["openfill"]
    seed_box = (round(plan.by_id("boxfill").points[0][0]), round(plan.by_id("boxfill").points[0][1]))
    assert driver.image.getpixel(seed_box) == (0xF1, 0xC4, 0x0F)
    assert driver.image.getpixel((5, 5)) == (255, 255, 255)  # nothing flooded the page
    decisions = [e.get("decision") for e in result.events if e["event"] == "fill"]
    assert "fill" in decisions and "unsafe" in decisions


def test_recoverable_error_is_recovered_not_fatal() -> None:
    plan = geometry.plan_scene(_scene()[:1], CANVAS, margin_fraction=0.01, check_overlaps=False)
    driver = FakeDriver(plan, fail_ready_once=True)
    result = engine.run_plan(plan, driver)
    assert result.ok and driver.recovered == 1
    assert any(e["event"] == "recovery" and e["kind"] == "focus" for e in result.events)


def test_unrecoverable_error_is_not_swallowed() -> None:
    plan = geometry.plan_scene(_scene()[:1], CANVAS, margin_fraction=0.01, check_overlaps=False)
    driver = FakeDriver(plan)
    driver.recover = lambda e: False  # type: ignore[method-assign]
    driver.fail_ready_once = True
    with pytest.raises(ToolError):
        engine.run_plan(plan, driver)


def test_resume_draws_only_what_is_missing() -> None:
    plan = geometry.plan_scene([_scene()[0], _scene()[2]], CANVAS, margin_fraction=0.01, check_overlaps=False)
    driver = FakeDriver(plan)
    driver.draw(plan.by_id("box"))  # a previous run got this far
    result = engine.run_plan(plan, driver, resume=True)
    assert result.ok and result.drawn == 1 and result.resumed == ["box"]


def test_exact_width_and_brush_survive_planning() -> None:
    plan = geometry.plan_scene([DrawingObject("s", "path", ((50, 50), (300, 80), (500, 60)), None, "#102030", "detail", 0,
                                              width_px=17, brush="Oil brush")], CANVAS, check_overlaps=False)
    o = plan.objects[0]
    assert o.stroke_px == 17 and o.brush == "Oil brush"


def test_reference_scoring_prefers_the_painting_over_derivatives() -> None:
    good = reference._score("File:Vincent van Gogh - The Starry Night - Google Art Project.jpg", "starry night van gogh",
                            1600, 1200, "Public domain")
    bad = reference._score("File:Starry Night poster detail crop.jpg", "starry night van gogh", 1600, 1200, "CC BY-SA 4.0")
    assert good > bad
