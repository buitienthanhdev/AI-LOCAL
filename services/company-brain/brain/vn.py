"""Hiểu cách người Việt gõ: bỏ dấu, gõ nhầm bộ gõ Telex/VNI khi đang gõ tiếng Anh, viết tắt.

Ví dụ: đang bật Telex mà gõ "word" sẽ ra "ửod" (w→ư, r→dấu hỏi). Hàm telex_variants("ửod") khôi phục lại
các chuỗi phím có thể đã gõ: "word", "wrod", "wod"... để so khớp mờ với tên ứng dụng / tên file."""
import re
import unicodedata
from difflib import SequenceMatcher

# Dấu thanh -> phím Telex
_TONE_KEY = {"́": "s", "̀": "f", "̉": "r", "̃": "x", "̣": "j"}
# Chữ có mũ/móc -> phím Telex (ư thường do gõ "w" một mình)
_BASE_KEYS = {"â": ["aa"], "ă": ["aw"], "ê": ["ee"], "ô": ["oo"], "ơ": ["ow", "w"], "ư": ["w", "uw"], "đ": ["dd"]}

# Viết tắt / tiếng lóng phổ biến khi chat
SLANG = {
    "gg": "google", "fb": "facebook", "yt": "youtube", "utb": "youtube", "ytb": "youtube", "ko": "khong", "k": "khong", "dc": "duoc", "đc": "duoc",
    "j": "gi", "vs": "voi", "ns": "noi", "nt": "nhan tin", "tl": "tra loi", "bt": "binh thuong", "mn": "moi nguoi",
    "ad": "admin", "pp": "powerpoint", "ppt": "powerpoint", "xl": "excel", "ex": "excel", "wd": "word",
}


def _py_plain(s: str) -> str:
    """Chữ thường, bỏ dấu tiếng Việt, đ->d. (Bản tham chiếu Python; brain_core.plain phải y hệt.)"""
    s = unicodedata.normalize("NFKD", str(s).replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


try:
    from .config import USE_RUST_NORM as _USE_RS
    import brain_core as _bc
    _rs_plain = _bc.plain if _USE_RS else None
except ImportError:
    _rs_plain = None


def plain(s: str) -> str:
    if _rs_plain is None:
        return _py_plain(s)
    return _rs_plain(s if type(s) is str else str(s))


def _syllable_variants(word: str) -> set[str]:
    """Các chuỗi phím Telex có thể sinh ra 1 từ có dấu."""
    tones, chars = [], []
    for ch in unicodedata.normalize("NFC", word.lower()):
        d = unicodedata.normalize("NFD", ch)
        base = d[0]
        marks = d[1:]
        tone = next((_TONE_KEY[m] for m in marks if m in _TONE_KEY), None)
        if tone:
            tones.append((len(chars), tone))
        # dựng lại chữ có mũ/móc (bỏ dấu thanh) để tra _BASE_KEYS
        hat = unicodedata.normalize("NFC", base + "".join(m for m in marks if m not in _TONE_KEY))
        chars.append(_BASE_KEYS.get(hat, [plain(hat)]))
    seqs = [""]
    for opts in chars:
        seqs = [s + o for s in seqs for o in opts][:32]
    out = set(seqs)
    for s in seqs:  # chèn phím dấu thanh ở mọi vị trí (người gõ có thể bấm dấu ở bất kỳ đâu)
        for _, t in tones[:1]:
            for i in range(len(s) + 1):
                out.add(s[:i] + t + s[i:])
    return out


def telex_variants(text: str, limit: int = 64) -> set[str]:
    words = text.split()
    if not words or len(words) > 4:
        return {plain(text)}
    combos = [""]
    for w in words:
        vs = sorted(_syllable_variants(w), key=len)[:12]
        combos = [(c + " " + v).strip() for c in combos for v in vs][:limit]
    return set(combos) | {plain(text)}


def expand_slang(text: str) -> str:
    return " ".join(SLANG.get(t, t) for t in plain(text).split())


def similarity(a: str, b: str) -> float:
    """0..1 — giống nhau tới đâu (bỏ khoảng trắng, không phân biệt hoa thường)."""
    a, b = re.sub(r"\s+", "", a), re.sub(r"\s+", "", b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def best_similarity(query: str, name: str) -> float:
    """So khớp mờ query (mọi biến thể gõ nhầm) với tên: lấy điểm cao nhất với cả tên hoặc từng từ trong tên."""
    n = plain(name)
    parts = [n] + n.split()
    best = 0.0
    for v in telex_variants(query) | {expand_slang(query)}:
        for p in parts:
            best = max(best, similarity(v, p))
    return best
