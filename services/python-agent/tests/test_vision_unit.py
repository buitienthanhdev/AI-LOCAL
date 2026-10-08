from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from lga_common.config import Config
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.vision.backends import GeminiGrounding
from lga_worker.vision.gateway_client import ChatAnswer
from lga_worker.vision.provider import VisionProvider
from lga_worker.vision.ui_tars_parser import UiTarsParseError, parse_actions, smart_resize
from lga_worker.windows.provider import ScreenshotResult


# Golden cases from references/ui-tars/codes/tests/action_parser_test.py
def test_upstream_golden_click() -> None:
    actions = parse_actions("Thought: test\nAction: click(point='<point>200 300</point>')", 224, 224)
    assert actions[0].action_type == "click"
    assert actions[0].start_box is not None
    assert actions[0].thought == "test"


def test_smart_resize_matches_upstream_math() -> None:
    assert smart_resize(1080, 1920, max_pixels=1280 * 28 * 28) == (728, 1316)
    h, w = smart_resize(1080, 1920)
    assert h % 28 == 0 and w % 28 == 0


@pytest.mark.parametrize(
    ("text", "model_type", "expected"),
    [
        ("Action: click(start_box='(500,250)')", "uitars1", (0.5, 0.25)),
        ("Action：click(start_box='<|box_start|>(500,250)<|box_end|>')", "uitars1", (0.5, 0.25)),
        ("Action: click(point='<bbox>100 100 300 300</bbox>')", "uitars1", (0.2, 0.2)),
        ("(658,364)", "qwen25vl", (0.5, 0.5)),
    ],
)
def test_point_formats(text: str, model_type: str, expected: tuple[float, float]) -> None:
    point = parse_actions(text, 728, 1316, model_type=model_type)[0].start_point
    assert point is not None
    assert point == pytest.approx(expected, abs=1e-3)


def test_scroll_direction_and_type_content_preserved() -> None:
    actions = parse_actions("Action: scroll(point='<point>10 10</point>', direction='left')\n\n"
                            "type(content='a\\'b')", 1000, 1000, model_type="uitars1")
    assert actions[0].params["direction"] == "left"
    assert actions[1].action_type == "type"


def test_garbage_raises_instead_of_printing() -> None:
    with pytest.raises(UiTarsParseError):
        parse_actions("Action: click(start_box=open('x'))", 100, 100, model_type="uitars1")
    with pytest.raises(UiTarsParseError):
        parse_actions("Action: ", 100, 100)


class FakeGateway:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def upload(self, path: Path) -> str:
        return "att_1"

    def chat(self, system: str, text: str, attachment_ids: list[str], timeout_s: float) -> ChatAnswer:
        return ChatAnswer(self.reply, "primary", "gemini")


def test_gemini_box_is_converted_to_normalised_center(tmp_path: Path) -> None:
    grounding = GeminiGrounding(FakeGateway('```json\n{"found": true, "box_2d": [100, 200, 300, 400], '
                                            '"label": "OK", "confidence": 0.9}\n```'))  # type: ignore[arg-type]
    g = grounding.locate(tmp_path / "x.png", 100, 100, "ok button", 5)
    assert g.found and (g.x, g.y) == pytest.approx((0.3, 0.2))
    assert g.box == pytest.approx((0.2, 0.1, 0.4, 0.3))


def test_gemini_rejects_out_of_range_boxes(tmp_path: Path) -> None:
    grounding = GeminiGrounding(FakeGateway('{"found": true, "box_2d": [100, 900, 300, 400]}'))  # type: ignore[arg-type]
    with pytest.raises(ToolError) as exc:
        grounding.locate(tmp_path / "x.png", 100, 100, "x", 5)
    assert exc.value.code == ErrorCode.EXECUTION_FAILED


def test_prepare_downscales_to_pixel_budget(config: Config, tmp_path: Path) -> None:
    source = tmp_path / "shot.png"
    Image.new("RGB", (1920, 1080), "white").save(source)
    provider = VisionProvider(config, desktop=None)  # type: ignore[arg-type]
    shot = ScreenshotResult(source, 1920, 1080, -1920, 0)
    gemini = provider.prepare(shot, for_ui_tars=False)
    assert gemini.width * gemini.height <= int(config.get("vision.max_pixels"))
    assert gemini.screen.left == -1920 and gemini.screen.width == 1920
    tars = provider.prepare(shot, for_ui_tars=True)
    assert (tars.height, tars.width) == smart_resize(1080, 1920, max_pixels=int(config.get("vision.max_pixels")))
