"""Trí nhớ hội thoại dài: giữ nguyên văn các tin nhắn gần nhất, phần cũ hơn được TÓM TẮT theo từng khối cố định.
Mỗi khối chỉ tóm tắt một lần (cache theo hash nội dung) => hội thoại dài bao nhiêu cũng chỉ tốn thêm 1 lần gọi model
mỗi BLOCK tin nhắn mới, không phải mỗi lượt."""
import hashlib
import json
import threading
from datetime import datetime

from .catalog import connect

KEEP_RECENT = 8   # số tin nhắn gần nhất đưa nguyên văn
BLOCK = 10        # số tin nhắn mỗi khối tóm tắt
MAX_BLOCKS = 12   # tối đa ~120 tin nhắn cũ được nhớ dạng tóm tắt
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS convo_summaries (
    key        TEXT PRIMARY KEY,   -- sha256 của khối tin nhắn
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

PROMPT = """Tóm tắt đoạn hội thoại sau giữa NGƯỜI DÙNG và TRỢ LÝ để trợ lý nhớ về sau. Viết tiếng Việt, gạch đầu dòng,
tối đa 8 dòng. GIỮ NGUYÊN: tên file + đường dẫn, tên app, con số, quyết định, yêu cầu còn dang dở, điều người dùng dặn/thích.
Bỏ lời chào và chi tiết thừa.

HỘI THOẠI:
{text}"""


def _conn():
    c = connect()
    c.executescript(SCHEMA)
    return c


def _key(block: list[dict]) -> str:
    return hashlib.sha256(json.dumps(block, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _summarize(block: list[dict]) -> str:
    key = _key(block)
    c = _conn()
    r = c.execute("SELECT summary FROM convo_summaries WHERE key=?", (key,)).fetchone()
    c.close()
    if r:
        return r["summary"]
    from . import llm
    text = "\n".join(f"{'NGƯỜI DÙNG' if m['role'] == 'user' else 'TRỢ LÝ'}: {str(m['content'])[:1200]}" for m in block)
    out = llm.chat([{"role": "user", "content": PROMPT.format(text=text)}], role="chat", temperature=0,
                   timeout=300)["message"].get("content", "").strip()
    with _lock:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO convo_summaries (key, summary, created_at) VALUES (?,?,?)",
                  (key, out[:2000], datetime.now().isoformat(timespec="seconds")))
        c.commit()
        c.close()
    return out


def compact(history: list[dict]) -> tuple[str | None, list[dict]]:
    """-> (tóm tắt phần cũ hoặc None, các tin nhắn gần đây giữ nguyên văn)."""
    history = [m for m in history or [] if m.get("role") in ("user", "assistant")]
    if len(history) <= KEEP_RECENT:
        return None, history
    n_old = (len(history) - KEEP_RECENT) // BLOCK * BLOCK  # chỉ tóm tắt các khối ĐẦY (khối dở giữ nguyên văn)
    if n_old == 0:
        return None, history
    blocks = [history[i:i + BLOCK] for i in range(0, n_old, BLOCK)][-MAX_BLOCKS:]
    parts = []
    for b in blocks:
        try:
            parts.append(_summarize(b))
        except Exception:
            parts.append("\n".join(f"- {m['role']}: {str(m['content'])[:150]}" for m in b))  # model lỗi: rút gọn thô
    return "\n".join(parts), history[n_old:]
