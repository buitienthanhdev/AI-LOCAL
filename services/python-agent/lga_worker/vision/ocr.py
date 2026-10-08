"""Text recognition with the Windows built-in OCR engine (Windows.Media.Ocr via PyWinRT).

No model download and no GPU: it uses the OCR languages installed with Windows (Settings > Time &
language > Language). Results carry word boxes in image pixels; the provider maps them to the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lga_worker.errors import ErrorCode, ToolError


@dataclass(frozen=True)
class OcrWord:
    text: str
    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: list[OcrWord]

    @property
    def box(self) -> tuple[float, float, float, float]:
        left = min(w.left for w in self.words)
        top = min(w.top for w in self.words)
        right = max(w.left + w.width for w in self.words)
        bottom = max(w.top + w.height for w in self.words)
        return left, top, right, bottom


def _import_winrt():
    try:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    except ImportError as exc:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        "Windows OCR bindings are not installed (winrt-Windows.Media.Ocr); run `uv sync`") from exc
    return BitmapDecoder, OcrEngine, DataWriter, InMemoryRandomAccessStream


def available_languages() -> list[str]:
    _, engine_cls, _, _ = _import_winrt()
    return [lang.language_tag for lang in engine_cls.available_recognizer_languages]


async def recognize(image: Path) -> tuple[list[OcrLine], str]:
    """Recognise text in a PNG/JPEG file. Returns (lines, recogniser language tag)."""
    decoder_cls, engine_cls, writer_cls, stream_cls = _import_winrt()
    engine = engine_cls.try_create_from_user_profile_languages()
    if engine is None:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        "no Windows OCR language is installed for the user's profile languages")
    stream = stream_cls()
    writer = writer_cls(stream)
    writer.write_bytes(image.read_bytes())
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await decoder_cls.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    lines: list[OcrLine] = []
    for line in result.lines:
        words = [OcrWord(w.text, w.bounding_rect.x, w.bounding_rect.y, w.bounding_rect.width, w.bounding_rect.height)
                 for w in line.words]
        if words:
            lines.append(OcrLine(line.text, words))
    return lines, engine.recognizer_language.language_tag
