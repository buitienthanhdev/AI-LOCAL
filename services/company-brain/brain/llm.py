"""Cổng gọi LLM cục bộ (Ollama) dùng chung: chọn model theo vai trò (router), đo độ trễ + tokens/s,
thử lại khi lỗi tạm thời, tự lùi về model production nếu model canary/candidate lỗi (chịu lỗi: GPU OOM, model vắng)."""
import json
import threading
import time
from contextlib import contextmanager

import requests

from . import obs, registry
from .config import CHAT_KEEP_ALIVE, LLM_OPTIONS, OLLAMA_URL

# Ollama xử lý LẦN LƯỢT từng yêu cầu: nếu việc học nền (đọc ảnh/video, trích tri thức) xếp sẵn nhiều yêu cầu thì câu hỏi
# của người dùng phải chờ sau cả hàng (đo được ~6 phút). Cổng GPU: khi người dùng đang hỏi, việc nền không gửi yêu cầu mới;
# việc nền chỉ được 1 yêu cầu tại một thời điểm => câu hỏi chờ tối đa 1 việc nền đang dở.
BACKGROUND_ROLES = {"vision", "extract"}
# Trần số token được SINH mỗi lần gọi. Không có trần, model đôi khi rơi vào vòng lặp (lặp lại câu chữ) và sinh tới khi đầy
# ngữ cảnh — đo được một lần đọc ảnh sinh >13.700 token (~6 phút) chặn cả hàng đợi, kể cả câu hỏi của người dùng.
MAX_PREDICT = {"chat": 2048, "vision": 1536, "extract": 2048}
_fg = 0
_fg_lock = threading.Lock()
_bg_slot = threading.BoundedSemaphore(1)
_tls = threading.local()


class LLMError(RuntimeError):
    pass


@contextmanager
def foreground():
    """Bao quanh việc trả lời người dùng: trong lúc này việc học nền nhường GPU."""
    global _fg
    with _fg_lock:
        _fg += 1
    _tls.fg = getattr(_tls, "fg", 0) + 1
    try:
        yield
    finally:
        _tls.fg -= 1
        with _fg_lock:
            _fg -= 1


def user_waiting() -> bool:
    return _fg > 0


@contextmanager
def _background_turn():
    while True:
        while _fg > 0:  # người dùng đang hỏi => chờ
            time.sleep(0.3)
        _bg_slot.acquire()
        if _fg == 0:
            break
        _bg_slot.release()  # người dùng vừa hỏi trong lúc chờ slot => nhường tiếp
    try:
        yield
    finally:
        _bg_slot.release()


def _is_background(role: str) -> bool:
    if getattr(_tls, "fg", 0) > 0:  # gọi từ bên trong một câu trả lời (vd. đọc ảnh đính kèm) => là việc của người dùng
        return False
    return role in BACKGROUND_ROLES


def chat(messages: list[dict], model: str | None = None, role: str = "chat", tools: list | None = None,
         fmt: dict | None = None, temperature: float = 0.1, num_ctx: int = 16384, keep_alive: str = CHAT_KEEP_ALIVE,
         timeout: int = 900) -> dict:
    """Trả về JSON phản hồi của Ollama (+ '_model' = model thực sự đã dùng)."""
    if _is_background(role):
        for _ in range(20):  # bị người dùng ngắt => chờ họ hỏi xong rồi làm lại từ đầu
            with _background_turn():
                try:
                    return _chat(messages, model, role, tools, fmt, temperature, num_ctx, keep_alive, timeout,
                                 preemptible=True)
                except _Preempted:
                    obs.metric("llm_preempted", 1, role=role)
        raise LLMError("việc nền bị ngắt quá nhiều lần")
    return _chat(messages, model, role, tools, fmt, temperature, num_ctx, keep_alive, timeout)


class _Preempted(Exception):
    """Việc nền bị ngắt vì người dùng vừa hỏi (đóng kết nối => Ollama dừng sinh, GPU rảnh ngay)."""


def _post_preemptible(payload: dict, timeout: int) -> requests.Response | dict:
    """Gọi Ollama ở chế độ stream để có thể DỪNG giữa chừng khi người dùng hỏi: trước đây câu hỏi phải chờ việc đọc
    ảnh/trích tri thức đang chạy dở (đo được 4-10 s mỗi câu). Ghép các mảnh lại thành đúng dạng phản hồi không-stream."""
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={**payload, "stream": True}, timeout=timeout, stream=True)
    if r.status_code >= 400:
        return r
    content, last = [], {}
    try:
        for line in r.iter_lines():
            if _fg > 0:
                raise _Preempted()
            if not line:
                continue
            last = json.loads(line)
            content.append((last.get("message") or {}).get("content", ""))
    finally:
        r.close()  # với _Preempted: đóng kết nối => Ollama huỷ yêu cầu
    last["message"] = {"role": "assistant", "content": "".join(content)}
    return last


def _chat(messages, model, role, tools, fmt, temperature, num_ctx, keep_alive, timeout, preemptible=False) -> dict:
    chosen = model or registry.pick(role)
    candidates = [chosen] if model else [chosen, registry.production(role)]
    last_err: Exception | None = None
    for m in dict.fromkeys(candidates):
        repeat_retry = False
        for attempt in range(2):
            anti_loop = role == "vision" or repeat_retry  # chống lặp khi tả ảnh / sau lần bị huỷ vì lặp chữ
            payload = {"model": m, "messages": messages, "stream": False, "think": False, "keep_alive": keep_alive,
                       "options": {"temperature": max(temperature, 0.4) if repeat_retry else temperature, "num_ctx": num_ctx,
                                   **LLM_OPTIONS, "num_predict": MAX_PREDICT.get(role, 2048),
                                   **({"repeat_penalty": 1.15} if anti_loop else {})}}
            if tools:
                payload["tools"] = tools
            if fmt:
                payload["format"] = fmt
            t0 = time.time()
            try:
                if preemptible and not tools:
                    r = _post_preemptible(payload, timeout)
                    if isinstance(r, dict):
                        r, data = None, r
                else:
                    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
                if r is None:
                    pass
                elif r.status_code >= 500:  # lỗi tạm (nạp model, OOM...) => ghi lý do Ollama trả về, thử lại một lần
                    detail = (r.text or "")[:200]
                    obs.metric("error.llm", 1, model=m, role=role, status=r.status_code, detail=detail)
                    last_err = requests.HTTPError(f"{r.status_code}: {detail}")
                    if attempt == 0:
                        if "repeat limit" in detail:  # model lặp chữ => Ollama huỷ; thử lại y hệt sẽ lặp tiếp
                            repeat_retry = True
                        else:
                            time.sleep(3)
                        continue
                    break
                if r is not None:
                    r.raise_for_status()
                    data = r.json()
            except requests.RequestException as e:
                last_err = e
                obs.metric("error.llm", 1, model=m, role=role)
                if attempt == 0:
                    time.sleep(2)
                    continue
                break
            dt = time.time() - t0
            obs.metric("llm_latency_s", dt, model=m, role=role)
            if data.get("done_reason") == "length":  # chạm trần token: thường là model lặp vô tận
                obs.metric("error.llm_truncated", 1, model=m, role=role)
            ev, dur = data.get("eval_count"), data.get("eval_duration")
            if ev and dur:
                obs.metric("tokens_per_s", ev / (dur / 1e9), model=m)
            data["_model"] = m
            return data
    raise LLMError(f"LLM không phản hồi ({last_err})")
