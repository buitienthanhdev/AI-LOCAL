"""Grounding backends: map a natural-language target description to a point in a screenshot.

* `GeminiGrounding`: Gemini (through the local gateway) with image input. Gemini is asked for its native
  grounding format, `box_2d = [ymin, xmin, ymax, xmax]` normalised to 0..1000.
* `UiTarsGrounding`: a UI-TARS model served by any OpenAI-compatible server (Ollama, vLLM), prompted with
  the official grounding prompt and parsed with the vendored UI-TARS parser. It refuses to run when the
  GPU lacks free memory, instead of silently spilling to CPU.

Both return normalised (0..1) coordinates relative to the image they were given; the provider maps them
back to screen pixels.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.vision.gateway_client import GatewayClient
from lga_worker.vision.ui_tars_parser import GROUNDING_PROMPT, UiTarsParseError, parse_actions

GEMINI_BOX_SCALE = 1000.0
NVIDIA_SMI_TIMEOUT_S = 10.0
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

GROUNDING_SYSTEM = (
    "You are a precise GUI grounding model. You receive one screenshot and a description of a single UI "
    "target. Reply with exactly one JSON object and nothing else: "
    '{"found": true|false, "box_2d": [ymin, xmin, ymax, xmax], "label": "<visible text or short name of the '
    'target>", "confidence": <0..1>}. box_2d is normalised to 0-1000 over the whole image. If the target is '
    'not visible, reply {"found": false, "reason": "<why>"}.'
)
DESCRIBE_SYSTEM = (
    "You look at a screenshot of a Windows desktop and answer the question about it precisely and briefly. "
    "Only state what is visible; say so if something cannot be determined from the image."
)


@dataclass(frozen=True)
class Grounding:
    found: bool
    x: float  # normalised 0..1 within the image
    y: float
    box: tuple[float, float, float, float] | None
    label: str
    confidence: float | None
    backend: str
    raw: str


def _extract_json(text: str) -> dict:
    match = _JSON_OBJECT.search(text)
    if not match:
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"vision model reply has no JSON: {text[:200]!r}")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"vision model reply is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError(ErrorCode.EXECUTION_FAILED, "vision model reply is not a JSON object")
    return value


class GroundingBackend(ABC):
    name: str

    @abstractmethod
    def locate(self, image: Path, width: int, height: int, description: str, timeout_s: float) -> Grounding: ...


class GeminiGrounding(GroundingBackend):
    name = "gemini"

    def __init__(self, client: GatewayClient) -> None:
        self._client = client

    def locate(self, image: Path, width: int, height: int, description: str, timeout_s: float) -> Grounding:
        attachment = self._client.upload(image)
        answer = self._client.chat(GROUNDING_SYSTEM, f"Target: {description}", [attachment], timeout_s)
        data = _extract_json(answer.content)
        backend = f"gemini:{answer.backend}"
        if not data.get("found"):
            return Grounding(False, 0.0, 0.0, None, str(data.get("reason") or ""), None, backend, answer.content)
        box = data.get("box_2d")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int | float) for v in box)):
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"vision model returned an invalid box_2d: {box!r}")
        ymin, xmin, ymax, xmax = (float(v) / GEMINI_BOX_SCALE for v in box)
        if not (0 <= xmin <= xmax <= 1 and 0 <= ymin <= ymax <= 1):
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"vision model box is outside the image: {box!r}")
        confidence = data.get("confidence")
        return Grounding(True, (xmin + xmax) / 2, (ymin + ymax) / 2, (xmin, ymin, xmax, ymax),
                         str(data.get("label") or ""), float(confidence) if isinstance(confidence, int | float) else None,
                         backend, answer.content)

    def describe(self, image: Path, question: str, timeout_s: float) -> tuple[str, str]:
        attachment = self._client.upload(image)
        answer = self._client.chat(DESCRIBE_SYSTEM, question, [attachment], timeout_s)
        return answer.content, f"gemini:{answer.backend}"


def free_vram_mb() -> int | None:
    """Free memory of the first NVIDIA GPU, or None when there is no NVIDIA driver."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return None
    result = subprocess.run([smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True,
                            text=True, timeout=NVIDIA_SMI_TIMEOUT_S, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return int(result.stdout.strip().splitlines()[0])


class UiTarsGrounding(GroundingBackend):
    name = "ui_tars"

    def __init__(self, base_url: str, model: str, min_free_vram_mb: int) -> None:
        if not model:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "vision.ui_tars_model is not configured")
        self._base = base_url.rstrip("/")
        self._model = model
        self._min_free_vram_mb = min_free_vram_mb
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check_capacity(self) -> None:
        free = free_vram_mb()
        if free is not None and free < self._min_free_vram_mb:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"only {free} MB GPU memory free; UI-TARS needs {self._min_free_vram_mb} MB "
                            "(unload other models or use the gemini vision backend)",
                            details={"free_vram_mb": free})

    def locate(self, image: Path, width: int, height: int, description: str, timeout_s: float) -> Grounding:
        self.check_capacity()
        data_url = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
        body = {"model": self._model, "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": GROUNDING_PROMPT.format(instruction=description)},
        ]}]}
        request = urllib.request.Request(self._base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                reply = json.loads(response.read())["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"UI-TARS server at {self._base}: {exc}") from exc
        try:
            actions = parse_actions(reply, image_height=height, image_width=width, model_type="qwen25vl")
        except UiTarsParseError as exc:
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"unparseable UI-TARS reply: {exc}") from exc
        point = actions[0].start_point
        if point is None:
            return Grounding(False, 0.0, 0.0, None, actions[0].action_type, None, self.name, reply)
        return Grounding(True, point[0], point[1], actions[0].start_box, actions[0].thought or "", None, self.name, reply)
