"""Workflow bền: chuỗi bước, trạng thái lưu DB sau MỖI bước; crash/restart thì resume đúng bước dở, không lặp lại
bước đã xong. Các hàm bước phải idempotent (có thể chạy lại an toàn nếu crash ngay trong bước)."""
import json
import threading
import traceback
import uuid
from datetime import datetime
from typing import Callable

from . import obs
from .catalog import connect

MAX_ATTEMPTS = 3
_defs: dict[str, list[tuple[str, Callable[[dict], dict | None]]]] = {}
_run_lock = threading.Lock()


def define(wf_type: str, steps: list[tuple[str, Callable[[dict], dict | None]]]):
    """Đăng ký loại workflow: danh sách (tên bước, hàm(state) -> dict cập nhật state)."""
    _defs[wf_type] = steps


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start(wf_type: str, state: dict | None = None, wf_id: str | None = None) -> str:
    if wf_type not in _defs:
        raise KeyError(f"workflow chưa định nghĩa: {wf_type}")
    wf_id = wf_id or f"{wf_type}-{uuid.uuid4().hex[:8]}"
    conn = connect()
    conn.execute("""INSERT OR IGNORE INTO workflows (wf_id, type, status, step, state, attempts, created_at, updated_at)
                    VALUES (?,?,'running',0,?,0,?,?)""", (wf_id, wf_type, json.dumps(state or {}), _now(), _now()))
    conn.commit()
    conn.close()
    return wf_id


def run(wf_id: str) -> dict:
    """Chạy tiếp từ bước hiện tại tới hết hoặc tới khi lỗi. Trả về hàng workflow."""
    with _run_lock:
        conn = connect()
        w = conn.execute("SELECT * FROM workflows WHERE wf_id=?", (wf_id,)).fetchone()
        if not w or w["status"] == "done":
            conn.close()
            return dict(w) if w else {}
        steps = _defs[w["type"]]
        state = json.loads(w["state"] or "{}")
        step = w["step"]
        conn.execute("UPDATE workflows SET status='running', attempts=attempts+1, updated_at=? WHERE wf_id=?",
                     (_now(), wf_id))
        conn.commit()
        while step < len(steps):
            name, fn = steps[step]
            try:
                upd = fn(state) or {}
            except Exception as e:
                err = f"bước '{name}': {type(e).__name__}: {e}"
                conn.execute("UPDATE workflows SET status='failed', error=?, state=?, updated_at=? WHERE wf_id=?",
                             (err, json.dumps(state, ensure_ascii=False, default=str), _now(), wf_id))
                conn.commit()
                obs.metric("error.workflow", 1, type=w["type"], step=name)
                conn.close()
                return {"wf_id": wf_id, "status": "failed", "step": step, "error": err,
                        "trace": traceback.format_exc()[-600:]}
            state.update(upd)
            step += 1  # lưu NGAY sau mỗi bước hoàn tất
            conn.execute("UPDATE workflows SET step=?, state=?, updated_at=?, error=NULL WHERE wf_id=?",
                         (step, json.dumps(state, ensure_ascii=False, default=str), _now(), wf_id))
            conn.commit()
        conn.execute("UPDATE workflows SET status='done', updated_at=? WHERE wf_id=?", (_now(), wf_id))
        conn.commit()
        conn.close()
        return {"wf_id": wf_id, "status": "done", "step": step, "state": state}


def resume_all() -> list[dict]:
    """Khởi động lại: chạy tiếp mọi workflow còn dở (running do crash, hoặc failed chưa quá số lần thử)."""
    conn = connect()
    ids = [r["wf_id"] for r in conn.execute(
        "SELECT wf_id FROM workflows WHERE status='running' OR (status='failed' AND attempts<?)", (MAX_ATTEMPTS,))]
    conn.close()
    return [run(i) for i in ids if i]


def get(wf_id: str) -> dict | None:
    conn = connect()
    r = conn.execute("SELECT * FROM workflows WHERE wf_id=?", (wf_id,)).fetchone()
    conn.close()
    return dict(r) if r else None
