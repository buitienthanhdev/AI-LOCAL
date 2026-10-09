"""Đọc ảnh / audio / video bằng model đa phương thức (Gemma 4) để biến thành văn bản tìm kiếm được.
- Ảnh: mô tả + chép nguyên văn chữ trong ảnh (OCR).
- Audio: cắt đoạn 30 giây (16 kHz mono) -> chép lời.
- Video: lấy vài khung hình rải đều -> mô tả theo dòng thời gian; tách tiếng -> chép lời.
Kết quả được cache theo nội dung file, nên index lại (vd. đổi quyền) không tốn GPU lần nữa."""
import base64
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

import requests

from .governor import maybe_rest
from .config import (AUDIO_CHUNK_SEC, AUDIO_MAX_SEC, DATA_DIR, LLM_OPTIONS, MEDIA_KEEP_ALIVE, MEDIA_MODEL,
                     OLLAMA_URL, VIDEO_FRAMES)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v"}
MEDIA_EXT = IMAGE_EXT | AUDIO_EXT | VIDEO_EXT
CACHE_DIR = DATA_DIR / "media_cache"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

IMAGE_PROMPT = ("Mô tả bức ảnh này bằng tiếng Việt, ngắn gọn nhưng đủ để tìm kiếm sau này (đối tượng, bối cảnh, "
                "sản phẩm, biểu đồ/bảng nếu có). Sau đó ghi mục 'Chữ trong ảnh:' và chép NGUYÊN VĂN mọi chữ, số "
                "đọc được (giữ ngôn ngữ gốc). Không bịa thêm.")
AUDIO_PROMPT = ("Chép lại NGUYÊN VĂN lời nói trong đoạn audio, giữ ngôn ngữ gốc. Chỉ trả về lời chép, không bình "
                "luận. Không có lời nói thì trả về [không có lời nói].")
VIDEO_PROMPT = ("Đây là {n} khung hình lấy theo thứ tự thời gian từ video '{name}' (thời điểm: {times}). "
                "Mô tả bằng tiếng Việt nội dung video theo dòng thời gian, và chép nguyên văn chữ xuất hiện trên "
                "màn hình nếu có. Không bịa thêm.")


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, creationflags=_NOWIN, timeout=600)


def _ask(prompt: str, blobs: list[bytes]) -> str:
    """Gửi prompt + ảnh/audio (Ollama nhận cả hai qua trường 'images') tới model đa phương thức."""
    from . import llm
    r = llm.chat([{"role": "user", "content": prompt, "images": [base64.b64encode(b).decode() for b in blobs]}],
                 role="vision", temperature=0, keep_alive=MEDIA_KEEP_ALIVE)
    return r["message"]["content"].strip()


def duration(path: Path) -> float:
    p = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       errors="replace", creationflags=_NOWIN, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def _as_jpeg(src: Path, out: Path, at: float | None = None) -> bytes | None:
    """Chuẩn hóa ảnh/khung hình: JPEG, cạnh dài tối đa 1280px (đủ đọc chữ, đỡ tốn VRAM)."""
    args = (["-ss", f"{at:.2f}"] if at is not None else []) + [
        "-i", str(src), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(out)]
    _run(args)
    return out.read_bytes() if out.exists() and out.stat().st_size else None


def read_image(path: Path, tmp: Path) -> str:
    img = _as_jpeg(path, tmp / "img.jpg")
    if img is None:  # ffmpeg không đọc được (vd. định dạng lạ) => gửi nguyên file
        img = path.read_bytes()
    return _ask(IMAGE_PROMPT, [img])


def transcribe(path: Path, tmp: Path, total: float | None = None) -> str:
    total = min(total if total is not None else duration(path), AUDIO_MAX_SEC)
    parts, start = [], 0.0
    while start < max(total, 1):
        seg = tmp / f"a{int(start)}.wav"
        _run(["-ss", f"{start}", "-t", f"{AUDIO_CHUNK_SEC}", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(seg)])
        if not seg.exists() or seg.stat().st_size < 2000:  # không có (thêm) tiếng
            break
        maybe_rest()  # audio dài: nghỉ giữa các đoạn khi đang học nền
        text = _ask(AUDIO_PROMPT, [seg.read_bytes()])
        if "[không có lời nói]" not in text:
            parts.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
        start += AUDIO_CHUNK_SEC
    if total >= AUDIO_MAX_SEC:
        parts.append(f"(chỉ chép {AUDIO_MAX_SEC // 60} phút đầu)")
    return "\n".join(parts)


def read_video(path: Path, tmp: Path) -> str:
    total = duration(path)
    times = [total * (i + 0.5) / VIDEO_FRAMES for i in range(VIDEO_FRAMES)] if total else [0.0]
    frames = [f for i, t in enumerate(times) if (f := _as_jpeg(path, tmp / f"f{i}.jpg", at=t))]
    out = []
    if frames:
        stamp = ", ".join(f"{int(t // 60):02d}:{int(t % 60):02d}" for t in times[:len(frames)])
        out.append("## Hình ảnh\n" + _ask(VIDEO_PROMPT.format(n=len(frames), name=path.name, times=stamp), frames))
    speech = transcribe(path, tmp, total)
    if speech:
        out.append("## Lời thoại\n" + speech)
    return f"Video dài {int(total // 60)} phút {int(total % 60)} giây.\n" + "\n\n".join(out)


def prepare_attachment(name: str, data: bytes) -> tuple[list[str], str]:
    """File người dùng đính kèm khi hỏi. Ảnh => đưa thẳng cho model nhìn (base64 JPEG).
    Audio/video => chuyển thành lời chép/mô tả (văn bản) ghép vào câu hỏi."""
    ext = Path(name).suffix.lower()
    if ext not in MEDIA_EXT:
        raise ValueError(f"Không hỗ trợ đính kèm {ext or name}")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src = tmp / f"in{ext}"
        src.write_bytes(data)
        if ext in IMAGE_EXT:
            img = _as_jpeg(src, tmp / "a.jpg") or data
            return [base64.b64encode(img).decode()], ""
        text = transcribe(src, tmp) if ext in AUDIO_EXT else read_video(src, tmp)
        return [], f"\n\n[Nội dung file đính kèm '{name}']\n{text or '[không đọc được nội dung]'}"


def read_media(path: Path) -> str:
    """Văn bản mô tả nội dung file media (có cache theo size+mtime+đường dẫn)."""
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}|{MEDIA_MODEL}".encode()).hexdigest()
    cache = CACHE_DIR / f"{key}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    ext = path.suffix.lower()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        if ext in IMAGE_EXT:
            text = "## Ảnh\n" + read_image(path, tmp)
        elif ext in AUDIO_EXT:
            text = "## Lời thoại (audio)\n" + (transcribe(path, tmp) or "[không có lời nói]")
        else:
            text = read_video(path, tmp)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text
