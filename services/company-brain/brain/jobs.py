"""Chạy quét + index trong nền (một job tại một thời điểm), có trạng thái để web hiển thị.
Tiết kiệm tài nguyên: ưu tiên tiến trình thấp, chỉ index khi máy rảnh, nghỉ giữa các file."""
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import (SCAN_ALL_DRIVES, SCAN_EVERY_HOURS, SCAN_ROOTS,
                     SHARED_ROOTS, SUPPORTED_EXT)

state = {"running": False, "message": "Chưa chạy", "last_run": None, "last_result": None}
_lock = threading.Lock()


def _progress(msg: str, path: str | None = None):
    state["message"] = msg
    if path:
        state["current_path"] = path


def idle_seconds() -> float:
    from . import governor
    return governor.idle_seconds()


def _throttle():
    """Gọi trước mỗi file: điều tiết THÍCH ỨNG theo tài nguyên trống (không còn chờ máy rảnh) — xem governor.py.
    Chế độ 'Học ngay' (force) = duty 1: không nghỉ."""
    from . import governor
    governor.rest()


def scan_roots() -> list[Path]:
    from .scanner import fixed_drives
    roots = fixed_drives() if SCAN_ALL_DRIVES else [*SCAN_ROOTS, *SHARED_ROOTS]
    return [Path(r).resolve() for r in roots if Path(r).exists()]


def _purge_outside(roots: list[Path]) -> int:
    """File không còn thuộc phạm vi quét (đổi cấu hình) hoặc loại file không còn hỗ trợ => đánh dấu deleted."""
    from .catalog import connect
    prefixes = [os.path.join(str(r), "").lower() for r in roots]
    conn = connect()
    n = 0
    for r in conn.execute("SELECT file_id, path, ext FROM files WHERE status='active'").fetchall():
        if r["ext"] not in SUPPORTED_EXT or not any(r["path"].lower().startswith(p) for p in prefixes):
            conn.execute("UPDATE files SET status='deleted' WHERE file_id=?", (r["file_id"],))
            n += 1
    conn.commit()
    conn.close()
    return n


# Lỗi do HẠ TẦNG (Ollama bận/nạp model/hết VRAM, mạng) chứ không phải do file hỏng => mỗi vòng học cho đọc lại.
# Lỗi do file (mã hóa, hỏng định dạng) vẫn giữ để không thử lại vô ích.
TRANSIENT_ERRORS = ("LLMError%", "ConnectionError%", "ReadTimeout%", "Timeout%", "HTTPError%", "ConnectTimeout%")


def retry_transient_errors(conn) -> int:
    n = 0
    for pat in TRANSIENT_ERRORS:
        n += conn.execute("UPDATE files SET extract_error=NULL WHERE extract_error LIKE ? AND status='active'",
                          (pat,)).rowcount
    conn.commit()
    return n


def run_once(force: bool = False):
    """force=True: học ngay, không chờ máy rảnh (máy sẽ nặng trong lúc chạy)."""
    if not _lock.acquire(blocking=False):
        return False
    from . import governor
    governor.mark_background()
    state["running"] = True
    try:
        from .catalog import connect
        from .index import index_all
        from .scanner import backfill_tiers, scan
        roots = scan_roots()
        c = connect()
        backfill_tiers(c, progress=_progress)  # gán bậc học cho dòng cũ (một lần)
        retry_transient_errors(c)
        c.close()
        from .facts import extract_facts_pending
        result = {"out_of_scope": _purge_outside(roots)}
        # 1) Đọc TỒN ĐỌNG đã có trong danh mục NGAY (quét 750.000 file có thể mất nhiều phút — không bắt việc học chờ nó)
        result["facts_backlog"] = extract_facts_pending(progress=_progress, throttle=_throttle)
        result["index_backlog"] = index_all(progress=_progress, throttle=_throttle)
        # 2) Quét lưới an toàn (phát hiện thay đổi watcher bỏ sót), 3) đọc phần mới phát hiện
        for r in roots:
            _progress(f"Quét {r} ...")
            result[str(r)] = scan(r, progress=_progress)
        result["facts"] = extract_facts_pending(progress=_progress, throttle=_throttle)
        result["index"] = index_all(progress=_progress, throttle=_throttle)
        state.update(last_result=result, message="Hoàn tất")
        from . import obs
        obs.metric("scan_cycle_done", 1)
    except Exception as e:
        state["message"] = f"Lỗi: {type(e).__name__}: {e}"
    finally:
        state["running"] = False
        state["force"] = False
        governor.set_force(False)
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        _lock.release()
    return True


def lower_priority():
    """Hạ ưu tiên CPU/IO của tiến trình server để không tranh tài nguyên với ứng dụng khác."""
    try:
        import win32api
        import win32process
        win32process.SetPriorityClass(win32api.GetCurrentProcess(), win32process.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def start_background(now: bool = True):
    def loop():
        if not now:
            time.sleep(SCAN_EVERY_HOURS * 3600)
        while True:
            run_once()
            time.sleep(SCAN_EVERY_HOURS * 3600)
    threading.Thread(target=loop, daemon=True, name="scan-loop").start()

    def maintenance_loop():
        """Bảo trì tri thức mỗi giờ, ĐỘC LẬP với việc index (index có thể chạy nhiều ngày): đóng góp, Git, đồ thị,
        lỗ hổng tri thức, canary. Workflow bền: crash giữa chừng thì resume đúng bước dở."""
        from . import workflow, workflows_def  # noqa: F401  (import để đăng ký định nghĩa)
        time.sleep(20)
        try:
            workflow.resume_all()
        except Exception:
            pass
        while True:
            try:
                wid = workflow.start("MAINTENANCE", {})
                workflow.run(wid)
            except Exception:
                pass
            time.sleep(3600)
    threading.Thread(target=maintenance_loop, daemon=True, name="maintenance").start()


def trigger(force: bool = False):
    """force: bật chế độ học ngay — kể cả job đang chạy (đang chờ máy rảnh) cũng chạy tiếp ngay."""
    if force:
        from . import governor
        state["force"] = True
        governor.set_force(True)
    threading.Thread(target=run_once, args=(force,), daemon=True, name="scan-now").start()
