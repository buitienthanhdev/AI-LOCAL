"""Điều tiết việc học ngầm THÍCH ỨNG theo tài nguyên đang trống — không còn "chờ máy rảnh 3 phút mới học".

Nhịp học = tỉ lệ thời gian được phép bận (duty). Sau mỗi phần việc mất t giây, bot nghỉ t × (1/duty − 1) giây
(GPU không có API chặn cứng %, nên "làm rồi nghỉ" là cách chuẩn để giới hạn trung bình). Duty được tính LIÊN TỤC:

  • MAX  (bấm "Học ngay"): duty = 1 — không nghỉ, gỡ giới hạn CPU, chạy song song nhiều file.
  • IDLE (không chạm chuột/phím ≥ IDLE_MINUTES): duty tới RESOURCE_LIMIT (60%), giảm nếu app khác đang chiếm GPU/CPU.
  • ACTIVE (đang dùng máy): duty = (phần tài nguyên còn trống − dự phòng cho người dùng), trần ACTIVE_MAX_DUTY (~35%),
    sàn TRICKLE_DUTY (~3% = "nhỏ giọt"): máy trống nhiều thì học nhiều hơn xíu, máy bận thì học rất ít.

Tải của CÁC APP KHÁC được đo lúc bot đang NGHỈ (không lẫn với tải của chính bot). RAM đầy thì tạm dừng.
Khi nghỉ, bot kiểm tra lại mỗi 0,5 s: người dùng đi khỏi máy / bấm "Học ngay" thì dậy ngay, không phải chờ hết nhịp nghỉ cũ.
Chat/trả lời câu hỏi KHÔNG bị điều tiết (người dùng đang chờ)."""
import ctypes
import shutil
import subprocess
import threading
import time
from ctypes import wintypes

from .config import (ACTIVE_MAX_DUTY, ACTIVE_RESERVE, FORCE_RAM_PAUSE_AT, IDLE_MINUTES, IDLE_RESERVE, MAX_REST_S,
                     RAM_PAUSE_AT, RESOURCE_LIMIT, TRICKLE_DUTY)

_lock = threading.Lock()
_last = time.monotonic()
_job = None
_force = False
_resting_since: float | None = None
_ext = {"cpu": 50.0, "gpu": 50.0, "at": 0.0, "samples": 0}   # tải của app KHÁC (EWMA), mặc định thận trọng
state = {"limit": int(RESOURCE_LIMIT * 100), "work_s": 0.0, "rest_s": 0.0, "paused_ram": False, "cpu_cap": False,
         "mode": "idle", "target": RESOURCE_LIMIT, "ext_cpu": None, "ext_gpu": None, "note": ""}
_tls = threading.local()
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ------------------------------------------------------------------ đo hệ thống
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def ram_load() -> int:
    m = _MemStatus()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.dwMemoryLoad)


def idle_seconds() -> float:
    """Số giây từ lần cuối có thao tác chuột/bàn phím trên toàn hệ thống."""
    li = _LastInput(ctypes.sizeof(_LastInput))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((tick - li.dwTime) & 0xFFFFFFFF) / 1000


_prev_cpu: tuple[int, int] | None = None


def cpu_load() -> float | None:
    """% CPU toàn máy kể từ lần gọi trước."""
    global _prev_cpu
    idle, kern, user = (ctypes.c_ulonglong() for _ in range(3))
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    cur = (idle.value, kern.value + user.value)
    prev, _prev_cpu = _prev_cpu, cur
    if not prev or cur[1] <= prev[1]:
        return None
    return round(100 * (1 - (cur[0] - prev[0]) / (cur[1] - prev[1])), 1)


def _rust_gpu():
    from .config import USE_RUST_GOVERNOR
    if not USE_RUST_GOVERNOR:
        return None
    try:
        import brain_core
        return brain_core.gpu_stat if hasattr(brain_core, "gpu_stat") else None
    except ImportError:
        return None


_gpu_native = _rust_gpu()


def gpu_load() -> float | None:
    """% GPU (NVIDIA) tức thời; None nếu không đọc được. Ưu tiên NVML (Rust, không tạo tiến trình); lùi về nvidia-smi."""
    if _gpu_native is not None:
        st = _gpu_native()
        if st is not None:
            return float(st[0])
    return _gpu_load_smi()


def _gpu_load_smi() -> float | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=4, creationflags=NOWIN).stdout.strip().splitlines()
        return float(out[0])
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        return None


def _sampler():
    """Chỉ lấy mẫu khi bot ĐÃ nghỉ ≥ 1,2 s (cửa sổ đo GPU/CPU không còn dính việc của chính bot) => đó là tải của app khác."""
    while True:
        time.sleep(1.0)
        try:
            since = _resting_since
            cpu = cpu_load()
            if since is None or time.monotonic() - since < 1.2:
                continue
            gpu = gpu_load()
            for key, val in (("cpu", cpu), ("gpu", gpu)):
                if val is not None:
                    _ext[key] = val if _ext["samples"] == 0 else 0.5 * _ext[key] + 0.5 * val
            _ext["samples"] += 1
            _ext["at"] = time.monotonic()
        except Exception:
            pass


threading.Thread(target=_sampler, daemon=True, name="load-sampler").start()


# ------------------------------------------------------------------ nhịp học
def is_force() -> bool:
    return _force


def set_force(on: bool) -> None:
    """Bật/tắt chế độ tối đa. Bật: gỡ giới hạn CPU; tắt: về giới hạn thường."""
    global _force
    _force = bool(on)
    set_cpu_cap(1.0 if on else RESOURCE_LIMIT)


def target_duty() -> float:
    """Tỉ lệ thời gian bot được phép bận ngay lúc này."""
    if _force:
        state.update(mode="max", target=1.0, note="Học tối đa: không nghỉ, song song, gỡ giới hạn CPU")
        return 1.0
    ext_gpu, ext_cpu = _ext["gpu"], _ext["cpu"]
    external = max(ext_gpu, 0.8 * ext_cpu)         # việc của bot chủ yếu là GPU; CPU ít quan trọng hơn
    headroom = 100.0 - external
    if idle_seconds() >= IDLE_MINUTES * 60:
        mode, cap, reserve = "idle", RESOURCE_LIMIT, IDLE_RESERVE
    else:
        mode, cap, reserve = "active", ACTIVE_MAX_DUTY, ACTIVE_RESERVE
    duty = max(TRICKLE_DUTY, min(cap, (headroom - reserve) / 100.0))
    if mode == "active":
        note = ("nhỏ giọt: máy đang bận" if duty <= TRICKLE_DUTY * 1.5 else
                "vừa phải: đang dùng máy nhưng còn trống" if duty < cap else "đang dùng máy, tài nguyên còn nhiều")
    else:
        note = "máy rảnh" + (" (app khác đang dùng GPU/CPU nên giảm)" if duty < cap else "")
    state.update(mode=mode, target=round(duty, 3), ext_cpu=round(ext_cpu), ext_gpu=round(ext_gpu), note=note)
    return duty


def reset():
    """Bắt đầu tính giờ làm việc từ bây giờ (thời gian chờ/nghỉ không bị tính là làm)."""
    global _last, _resting_since
    with _lock:
        _last = time.monotonic()
    _resting_since = None


def rest():
    """Gọi giữa các phần việc nền: nghỉ đủ để tỉ lệ bận ≈ duty hiện tại. Dậy sớm nếu điều kiện thay đổi."""
    global _resting_since
    with _lock:
        work = time.monotonic() - _last
    state["work_s"] += max(work, 0.0)
    slept = 0.0
    _resting_since = time.monotonic()
    while work > 0:
        d = target_duty()                                   # tính lại mỗi nhịp: người dùng bỏ đi / bấm Học ngay => dậy ngay
        need = 0.0 if d >= 1.0 else min(work * (1.0 / d - 1.0), MAX_REST_S)
        if slept >= need:
            break
        step = min(0.5, need - slept)
        time.sleep(step)
        slept += step
    state["rest_s"] += slept
    limit = FORCE_RAM_PAUSE_AT if _force else RAM_PAUSE_AT
    while ram_load() >= limit:
        state["paused_ram"] = True
        time.sleep(5)
    state["paused_ram"] = False
    reset()


def mark_background(on: bool = True):
    """Đánh dấu luồng hiện tại là việc học nền (chat thì không đánh dấu => không bị điều tiết)."""
    _tls.bg = on


def maybe_rest():
    """Dùng trong các vòng lặp dài (vd. chép lời từng đoạn audio): chỉ nghỉ nếu đang ở luồng học nền."""
    if getattr(_tls, "bg", False):
        rest()


def duty() -> float:
    """Tỉ lệ thời gian bận THỰC TẾ từ lúc chạy (để hiển thị)."""
    total = state["work_s"] + state["rest_s"]
    return round(100 * state["work_s"] / total, 1) if total else 0.0


# ------------------------------------------------------------------ giới hạn CPU cứng (Job Object)
def _cpu_rate(frac: float) -> bool:
    global _job
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class CpuRate(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]
    fresh = _job is None
    if fresh:
        _job = k.CreateJobObjectW(None, None)  # giữ handle suốt đời tiến trình
        if not _job:
            return False
    info = CpuRate(0x1 | 0x4, max(100, min(10000, int(frac * 10000))))  # ENABLE | HARD_CAP; đơn vị 1/100 %
    ok = bool(k.SetInformationJobObject(_job, 15, ctypes.byref(info), ctypes.sizeof(info)))
    if ok and fresh:
        ok = bool(k.AssignProcessToJobObject(_job, k.GetCurrentProcess()))
    return ok


def set_cpu_cap(frac: float) -> bool:
    """Đặt trần CPU của tiến trình máy chủ (1.0 = không giới hạn thực tế)."""
    ok = _cpu_rate(frac)
    state["cpu_cap"] = ok and frac < 1.0
    return ok


def cap_cpu_of_this_process() -> bool:
    """Chặn cứng CPU của tiến trình máy chủ ở RESOURCE_LIMIT (gọi khi khởi động)."""
    return set_cpu_cap(RESOURCE_LIMIT)
