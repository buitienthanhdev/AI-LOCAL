# Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
# SPDX-License-Identifier: Apache-2.0
#
# Adapted from UI-TARS (https://github.com/bytedance/UI-TARS, commit 582f3a7,
# codes/ui_tars/action_parser.py and codes/ui_tars/prompt.py), licensed under the Apache License 2.0
# (third_party/licenses/ui-tars-Apache-2.0.txt).
#
# Modifications by the Local Gemini Agent project:
# * `smart_resize` and its helpers are kept as in the original.
# * Action parsing returns typed `ParsedAction` objects with points/boxes normalised to 0..1 instead of
#   stringified lists; boxes are parsed with regular expressions instead of `eval()`.
# * Failures raise `UiTarsParseError` instead of `print()` + `assert`; the removed `ast.Str` branch
#   (gone in Python 3.14) is not used.
# * `<bbox>x1 y1 x2 y2</bbox>`, `<point>x y</point>`, `(x,y)` and `<|box_start|>(x,y)<|box_end|>` forms are
#   accepted, as are full-width colons (as in UI-TARS-desktop's TypeScript parser).
# * Scroll directions left/right are preserved (the original pyautogui translation only handled up/down).
# * The pyautogui code generator is not included: actions are executed by the agent's tool router.
"""UI-TARS output parsing and image resizing for grounding."""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field

IMAGE_FACTOR = 28
MIN_PIXELS = 100 * 28 * 28
MAX_PIXELS = 16384 * 28 * 28
MAX_RATIO = 200
# UI-TARS-1.0 / Qwen2-VL style models emit coordinates relative to a 0..1000 grid.
RELATIVE_COORDINATE_FACTOR = 1000

# Grounding prompt from codes/ui_tars/prompt.py (GROUNDING_DOUBAO), unchanged.
GROUNDING_PROMPT = (
    "You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform "
    "the next action to complete the task. \n\n## Output Format\n\nAction: ...\n\n\n## Action Space\n"
    "click(point='<point>x1 y1</point>'')\n\n## User Instruction\n{instruction}"
)


class UiTarsParseError(ValueError):
    pass


def round_by_factor(number: float, factor: int) -> int:
    return round(number / factor) * factor


def ceil_by_factor(number: float, factor: int) -> int:
    return math.ceil(number / factor) * factor


def floor_by_factor(number: float, factor: int) -> int:
    return math.floor(number / factor) * factor


def smart_resize(height: int, width: int, factor: int = IMAGE_FACTOR, min_pixels: int = MIN_PIXELS,
                 max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    """Rescale so both sides are divisible by `factor`, the pixel count is within bounds and the aspect
    ratio is kept as closely as possible. Returns (height, width)."""
    if max(height, width) / min(height, width) > MAX_RATIO:
        raise ValueError(f"absolute aspect ratio must be smaller than {MAX_RATIO}, "
                         f"got {max(height, width) / min(height, width)}")
    h_bar = max(factor, round_by_factor(height, factor))
    w_bar = max(factor, round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)
        w_bar = ceil_by_factor(width * beta, factor)
    return h_bar, w_bar


@dataclass(frozen=True)
class ParsedAction:
    action_type: str
    # Normalised (0..1) boxes: (x1, y1, x2, y2); a point is a zero-size box.
    start_box: tuple[float, float, float, float] | None = None
    end_box: tuple[float, float, float, float] | None = None
    params: dict[str, str] = field(default_factory=dict)
    thought: str | None = None
    reflection: str | None = None

    @property
    def start_point(self) -> tuple[float, float] | None:
        if self.start_box is None:
            return None
        x1, y1, x2, y2 = self.start_box
        return ((x1 + x2) / 2, (y1 + y2) / 2)


_NUMBER = r"-?\d+(?:\.\d+)?"
_POINT_TAG = re.compile(rf"<point>\s*({_NUMBER})[\s,]+({_NUMBER})\s*</point>")
_BBOX_TAG = re.compile(rf"<bbox>\s*({_NUMBER})[\s,]+({_NUMBER})[\s,]+({_NUMBER})[\s,]+({_NUMBER})\s*</bbox>")
_BOX_TOKENS = re.compile(r"<\|box_start\|>|<\|box_end\|>")
_NUMBERS = re.compile(_NUMBER)


def _normalize_markup(text: str) -> str:
    text = text.replace("：", ":").replace("[EOS]", "")
    text = _BOX_TOKENS.sub("", text)
    text = _POINT_TAG.sub(lambda m: f"({m.group(1)},{m.group(2)})", text)
    text = _BBOX_TAG.sub(lambda m: f"({m.group(1)},{m.group(2)},{m.group(3)},{m.group(4)})", text)
    text = text.replace("start_point=", "start_box=").replace("end_point=", "end_box=")
    return re.sub(r"(?<![a-z_])point=", "start_box=", text)


def _parse_call(call_text: str) -> tuple[str, dict[str, str]]:
    try:
        node = ast.parse(call_text.strip(), mode="eval")
    except SyntaxError as exc:
        raise UiTarsParseError(f"not a function call: {call_text!r}") from exc
    call = node.body
    if not isinstance(call, ast.Call):
        raise UiTarsParseError(f"not a function call: {call_text!r}")
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    else:
        raise UiTarsParseError(f"unsupported callee in {call_text!r}")
    params: dict[str, str] = {}
    for keyword in call.keywords:
        if keyword.arg is None or not isinstance(keyword.value, ast.Constant):
            raise UiTarsParseError(f"argument {keyword.arg!r} is not a literal in {call_text!r}")
        params[keyword.arg] = str(keyword.value.value)
    return name, params


def _box(raw: str, model_type: str, resized_hw: tuple[int, int] | None) -> tuple[float, float, float, float]:
    numbers = [float(n) for n in _NUMBERS.findall(raw)]
    if len(numbers) not in (2, 4):
        raise UiTarsParseError(f"box must have 2 or 4 numbers: {raw!r}")
    if model_type == "qwen25vl":
        if resized_hw is None:
            raise UiTarsParseError("absolute coordinates need the smart-resized image size")
        height, width = resized_hw
        normalised = [n / (height if i % 2 else width) for i, n in enumerate(numbers)]
    else:
        normalised = [n / RELATIVE_COORDINATE_FACTOR for n in numbers]
    if len(normalised) == 2:
        normalised = normalised * 2
    x1, y1, x2, y2 = normalised
    return (x1, y1, x2, y2)


def parse_actions(text: str, image_height: int, image_width: int, model_type: str = "qwen25vl",
                  min_pixels: int = MIN_PIXELS, max_pixels: int = MAX_PIXELS) -> list[ParsedAction]:
    """Parse a UI-TARS reply ("Thought: ...\\nAction: click(start_box='(x,y)')").

    For UI-TARS-1.5 (`qwen25vl`) coordinates are absolute pixels of the smart-resized image derived from
    (image_height, image_width); for UI-TARS-1.0 they are relative to a 0..1000 grid.
    A grounding-only reply without "Action:" (just `(x,y)` or a call) is also accepted.
    """
    text = _normalize_markup(text.strip())
    resized = smart_resize(image_height, image_width, IMAGE_FACTOR, min_pixels, max_pixels) \
        if model_type == "qwen25vl" else None
    thought = reflection = None
    match = re.search(r"Reflection:\s*(.+?)Action_Summary:\s*(.+?)(?=\s*Action:|$)", text, re.DOTALL)
    if match:
        reflection, thought = match.group(1).strip(), match.group(2).strip()
    else:
        match = re.search(r"(?:Thought|Action_Summary):\s*(.+?)(?=\s*Action:|$)", text, re.DOTALL)
        if match:
            thought = match.group(1).strip()
    action_text = text.split("Action:")[-1].strip()
    if re.fullmatch(rf"\(\s*{_NUMBER}\s*,\s*{_NUMBER}\s*\)", action_text):
        action_text = f"click(start_box='{action_text}')"
    if not action_text:
        raise UiTarsParseError("reply contains no action")
    actions: list[ParsedAction] = []
    for chunk in re.split(r"\)\s*\n\s*\n", action_text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not chunk.endswith(")"):
            chunk += ")"
        name, params = _parse_call(chunk.replace("\n", "\\n"))
        start = _box(params.pop("start_box"), model_type, resized) if "start_box" in params else None
        end = _box(params.pop("end_box"), model_type, resized) if "end_box" in params else None
        actions.append(ParsedAction(name, start, end, params, thought, reflection))
    if not actions:
        raise UiTarsParseError("reply contains no action")
    return actions
