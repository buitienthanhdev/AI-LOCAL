"""On-screen text recognition.

Two engines:
* Windows.Media.Ocr (PyWinRT): fast, no GPU, no download; uses the OCR languages installed with Windows.
  Windows has no Vietnamese OCR, so it reads English/Latin but not Vietnamese diacritics.
* EasyOCR (optional `ocr` extra, Apache-2.0, CPU): a Vietnamese model that reads diacritics. Loaded lazily
  and cached; the first call downloads its model (~100 MB) into runtime/models/easyocr.

Note: the agent reads real application text through UI Automation, which handles Vietnamese perfectly and
needs no OCR. OCR is the fallback for pixels only (images, canvas, games).
Results carry word boxes in the ORIGINAL image's pixels; the provider maps them to the screen.
"""

from __future__ import annotations

import io
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from lga_worker.errors import ErrorCode, ToolError

_easyocr_reader = None
_easyocr_lock = threading.Lock()
_VIETNAMESE_MARKERS = "ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ"


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


def _upscaled_png(image: Path, scale: float) -> bytes:
    from PIL import Image

    with Image.open(image) as source:
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        buffer = io.BytesIO()
        source.convert("RGB").resize(size, Image.Resampling.LANCZOS).save(buffer, format="PNG")
        return buffer.getvalue()


def vietnamese_available() -> bool:
    """True when the optional EasyOCR Vietnamese engine is installed.

    Only checks installation (importlib.util.find_spec); it does not import torch, so probing
    availability (e.g. from vision.status) never triggers torch's heavy load or a DLL conflict.
    """
    import importlib.util

    return importlib.util.find_spec("easyocr") is not None


def _easyocr(models_dir: Path):
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    with _easyocr_lock:
        if _easyocr_reader is None:
            # Avoid torch's "OMP: Error #15" / WinError 1114 when another OpenMP runtime is already
            # loaded in this process (the worker inherits a broad PATH).
            os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
            try:
                import easyocr
            except ImportError as exc:
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                                "Vietnamese OCR needs the optional 'ocr' extra: "
                                "uv sync --extra ocr (in services/python-agent)") from exc
            except OSError as exc:
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                                f"the Vietnamese OCR engine (torch) could not load: {exc}. "
                                "Windows OCR or the vision model are used instead.") from exc
            models_dir.mkdir(parents=True, exist_ok=True)
            # CPU only: the GPU is reserved for the UI-TARS vision model.
            _easyocr_reader = easyocr.Reader(["vi", "en"], gpu=False, verbose=False,
                                             model_storage_directory=str(models_dir),
                                             download_enabled=True)
    return _easyocr_reader


def _recognize_vietnamese(image: Path, scale: float, models_dir: Path) -> list[OcrLine]:
    import numpy as np
    from PIL import Image

    reader = _easyocr(models_dir)
    with Image.open(image) as source:
        if scale != 1.0:
            source = source.convert("RGB").resize(
                (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
                Image.Resampling.LANCZOS)
        array = np.array(source.convert("RGB"))
    lines: list[OcrLine] = []
    for box, text, confidence in reader.readtext(array):
        if not text.strip() or confidence < 0.3:
            continue
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        word = OcrWord(text, left, top, right - left, bottom - top)
        lines.append(OcrLine(text, [word]))
    return lines


def _easyocr_models_dir() -> Path:
    return Path(os.environ.get("LGA_ROOT", ".")) / "runtime" / "models" / "easyocr"


async def recognize(image: Path, scale: float = 1.0, prefer_vietnamese: bool = False) -> tuple[list[OcrLine], str]:
    """Recognise text in a PNG/JPEG file. Returns (lines, recogniser language tag).

    With `scale` > 1 the image is upscaled first (small UI text is recognised far more reliably); word boxes
    are returned in the ORIGINAL image's pixels. When `prefer_vietnamese` and the optional engine is
    installed, EasyOCR (vi+en) is used; otherwise the Windows engine runs.
    """
    import asyncio

    if prefer_vietnamese and vietnamese_available():
        lines = await asyncio.to_thread(_recognize_vietnamese, image, scale, _easyocr_models_dir())
        return lines, "vi+en (easyocr)"

    decoder_cls, engine_cls, writer_cls, stream_cls = _import_winrt()
    engine = engine_cls.try_create_from_user_profile_languages()
    if engine is None:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        "no Windows OCR language is installed for the user's profile languages")
    stream = stream_cls()
    writer = writer_cls(stream)
    data = _upscaled_png(image, scale) if scale != 1.0 else image.read_bytes()
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await decoder_cls.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    lines: list[OcrLine] = []
    for line in result.lines:
        words = [OcrWord(w.text, w.bounding_rect.x / scale, w.bounding_rect.y / scale, w.bounding_rect.width / scale,
                         w.bounding_rect.height / scale) for w in line.words]
        if words:
            lines.append(OcrLine(line.text, words))
    return lines, engine.recognizer_language.language_tag
