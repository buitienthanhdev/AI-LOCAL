"""Tự kiểm tra sức khỏe hệ thống — các lỗi từng làm bot chậm/hỏng mà người dùng không tự thấy được:
hai máy chủ Ollama cùng chạy, model chat không vừa GPU (chạy bằng CPU => chậm gấp nhiều lần), VRAM gần đầy, Qdrant chết,
câu trả lời chậm bất thường, ổ đĩa sắp đầy. Mỗi mục: ok / cảnh báo + cách sửa.

    python -m brain doctor"""
import shutil
from pathlib import Path
import subprocess

import requests

from .config import CHAT_MODEL, DATA_DIR, OLLAMA_URL

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ollama_listeners() -> set[int]:
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=_NOWIN,
                         timeout=20).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":11434") and parts[3].upper() == "LISTENING":
            pids.add(int(parts[4]))
    out6 = subprocess.run(["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True, creationflags=_NOWIN,
                          timeout=20).stdout
    for line in out6.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":11434") and parts[3].upper() == "LISTENING":
            pids.add(int(parts[4]))
    return pids


def _check(name: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix}


def run() -> list[dict]:
    out = []
    # 1) chỉ được có MỘT máy chủ Ollama
    try:
        pids = _ollama_listeners()
        out.append(_check("Máy chủ Ollama", len(pids) == 1,
                          f"{len(pids)} tiến trình nghe cổng 11434" + (f" (PID {', '.join(map(str, sorted(pids)))})" if pids else ""),
                          "Có nhiều bản 'ollama serve' cùng chạy: bot và lệnh ollama có thể dùng hai bản khác nhau, nạp "
                          "model hai lần => tràn VRAM, rất chậm. Tắt bớt (Task Manager > ollama.exe) hoặc khởi động lại Ollama."
                          if pids else "Ollama không chạy — mở ứng dụng Ollama."))
    except Exception as e:
        out.append(_check("Máy chủ Ollama", False, f"không kiểm tra được: {e}"))
    # 2) model chat nằm trọn trong GPU?
    try:
        models = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5).json().get("models", [])
        chat = next((m for m in models if m["name"].startswith(CHAT_MODEL)), None)
        if chat:
            frac = chat["size_vram"] / max(chat["size"], 1)
            out.append(_check("Model chat trên GPU", frac >= 0.99, f"{CHAT_MODEL}: {frac:.0%} trên GPU "
                              f"({chat['size_vram'] / 2**30:.1f} GB)",
                              "Một phần model chạy bằng CPU (VRAM không đủ) => trả lời chậm gấp nhiều lần. Đóng bớt app "
                              "dùng GPU (game, trình chỉnh video, AI khác) rồi hỏi lại."))
        else:
            out.append(_check("Model chat trên GPU", True, f"{CHAT_MODEL} chưa nạp (sẽ nạp ~10 s ở câu hỏi tới)"))
        for m in models:
            if "embed" in m["name"] and m["size_vram"] > 3 * 2**30:
                out.append(_check("Model embedding", False, f"{m['name']} chiếm {m['size_vram'] / 2**30:.1f} GB VRAM",
                                  "Ngữ cảnh embedding quá lớn — kiểm tra num_ctx trong index.embed."))
    except Exception as e:
        out.append(_check("Model chat trên GPU", False, f"không hỏi được Ollama: {type(e).__name__}",
                          "Ollama không phản hồi — mở/khởi động lại Ollama."))
    # 3) VRAM
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, creationflags=_NOWIN, timeout=15).stdout.strip()
        used, total = (int(x) for x in q.splitlines()[0].split(","))
        out.append(_check("VRAM", used / total < 0.93, f"{used / 1024:.1f}/{total / 1024:.1f} GB",
                          "GPU gần đầy — model dễ bị đẩy sang CPU. Đóng bớt app dùng GPU."))
    except Exception:
        pass
    # 4) Qdrant
    from .config import QDRANT_MODE
    from .index import _qdrant_alive
    alive = QDRANT_MODE != "server" or _qdrant_alive()
    out.append(_check("Kho vector (Qdrant)", alive, "đang chạy" if alive else "không chạy",
                      "Bấm 'Khởi động lại máy chủ' trong tab ⚙ để bật lại Qdrant."))
    # 5) độ trễ trả lời thực tế
    try:
        from .obs import summary
        s = summary(3600).get("ask_latency_s")
        if s:
            out.append(_check("Thời gian trả lời (1 giờ)", s["p95"] < 90, f"trung bình {s['avg']} s · p95 {s['p95']} s · {s['n']} câu",
                              "Chậm bất thường — xem các mục trên (GPU/Ollama) hoặc việc học nền đang 'Học ngay' tối đa."))
    except Exception:
        pass
    # 6) sao lưu + toàn vẹn
    try:
        import time as _t
        from . import backup
        st = backup.status()
        b, i = st.get("backup"), st.get("integrity")
        age = (_t.time() - b["ts"]) / 3600 if b else None
        out.append(_check("Sao lưu dữ liệu", bool(b) and age < 48 and b.get("check_ok"),
                          f"lần gần nhất {b['at']} ({b['mb']} MB) → {Path(b['file']).parent}" if b else "chưa có bản nào",
                          "Chạy `python -m brain backup` (bảo trì hằng giờ cũng tự sao lưu mỗi ngày)."))
        if i:
            out.append(_check("Toàn vẹn dữ liệu", i["ok"], f"quick_check={i['quick_check']} · đoạn {i['chunks']:,} · vector "
                              f"{i['vector_points'] if i['vector_points'] is not None else '?'}",
                              "DB lỗi hoặc kho vector lệch catalog: khôi phục bản sao lưu / chạy `python -m brain index`."))
    except Exception:
        pass
    # 7) ổ đĩa chứa dữ liệu
    free = shutil.disk_usage(DATA_DIR).free / 2**30
    out.append(_check("Ổ đĩa dữ liệu", free > 5, f"còn trống {free:.0f} GB", "Ổ gần đầy — dọn bớt để bot ghi được dữ liệu."))
    return out


def text() -> str:
    return "\n".join(f"{'✔' if c['ok'] else '⚠'} {c['name']}: {c['detail']}" + (f"\n    → {c['fix']}" if c["fix"] else "")
                     for c in run())
