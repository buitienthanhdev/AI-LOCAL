"""Cổng GPU: câu hỏi của người dùng không phải xếp hàng sau việc học nền."""
import threading
import time

from brain import llm


def test_background_waits_while_user_is_asking(monkeypatch):
    order = []
    monkeypatch.setattr(llm, "_chat", lambda m, model, role, *a, **k: order.append(role) or {"message": {}})
    with llm.foreground():
        t = threading.Thread(target=lambda: llm.chat([], role="vision"))
        t.start()
        time.sleep(0.5)
        assert order == [], "việc nền chưa được gửi khi người dùng đang hỏi"
        llm.chat([], role="chat")
        assert order == ["chat"]
    t.join(3)
    assert order == ["chat", "vision"]


def test_only_one_background_request_in_flight(monkeypatch):
    live, peak = [0], [0]

    def fake(m, model, role, *a, **k):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        time.sleep(0.1)
        live[0] -= 1
        return {"message": {}}
    monkeypatch.setattr(llm, "_chat", fake)
    ts = [threading.Thread(target=lambda: llm.chat([], role="extract")) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert peak[0] == 1, "chỉ 1 yêu cầu nền tại một thời điểm => câu hỏi chờ tối đa 1 việc dở"


def test_vision_inside_a_user_request_is_not_delayed(monkeypatch):
    order = []
    monkeypatch.setattr(llm, "_chat", lambda m, model, role, *a, **k: order.append(role) or {"message": {}})
    with llm.foreground():  # vd. đọc ảnh đính kèm khi trả lời
        llm.chat([], role="vision")
    assert order == ["vision"] and not llm.user_waiting()


def test_every_call_has_a_generation_cap(monkeypatch):
    """Không có trần token, một lần model lặp vô tận đã sinh >13.700 token và chặn hàng đợi ~6 phút."""
    sent = []

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}, "done_reason": "stop"}

        def iter_lines(self):  # việc nền (vision/extract) gọi ở chế độ stream
            yield __import__("json").dumps(self.json()).encode()

        def close(self):
            pass
    monkeypatch.setattr(llm.requests, "post", lambda url, json, timeout, stream=False: sent.append(json) or R())
    monkeypatch.setattr(llm.registry, "pick", lambda role: "m")
    monkeypatch.setattr(llm.registry, "production", lambda role: "m")
    for role in ("chat", "vision", "extract"):
        llm.chat([{"role": "user", "content": "x"}], role=role)
    caps = [p["options"]["num_predict"] for p in sent]
    assert all(0 < c <= 2048 for c in caps) and sent[1]["options"]["repeat_penalty"] > 1


def test_background_call_is_preempted_by_user_and_retried(monkeypatch):
    """Đọc ảnh nền đang sinh dở thì người dùng hỏi => đóng kết nối (Ollama dừng), chờ người dùng xong, làm lại từ đầu."""
    import json as _json
    calls, closed = [], []

    class Stream:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def iter_lines(self):
            for i in range(3):
                if self.n == 1 and i == 1:
                    llm._fg += 1  # người dùng hỏi giữa chừng

                    def done():
                        import time
                        time.sleep(0.4)
                        llm._fg -= 1
                    __import__("threading").Thread(target=done).start()
                yield _json.dumps({"message": {"content": f"p{i} "}, "done": i == 2, "eval_count": 3,
                                   "eval_duration": 10**9}).encode()

        def close(self):
            closed.append(self.n)

    def post(url, json, timeout, stream=False):
        calls.append(stream)
        return Stream(len(calls))
    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm.registry, "pick", lambda role: "m")
    monkeypatch.setattr(llm.registry, "production", lambda role: "m")
    r = llm.chat([{"role": "user", "content": "tả ảnh"}], role="vision")
    assert calls == [True, True], "lần 1 bị ngắt, lần 2 chạy lại (đều ở chế độ stream)"
    assert closed == [1, 2] and r["message"]["content"] == "p0 p1 p2 " and not llm.user_waiting()


def test_user_chat_is_not_streamed_or_preempted(monkeypatch):
    seen = []

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}}
    monkeypatch.setattr(llm.requests, "post", lambda url, json, timeout, stream=False: seen.append(stream) or R())
    monkeypatch.setattr(llm.registry, "pick", lambda role: "m")
    monkeypatch.setattr(llm.registry, "production", lambda role: "m")
    assert llm.chat([{"role": "user", "content": "x"}], role="chat")["message"]["content"] == "ok" and seen == [False]


def test_repeat_limit_abort_is_retried_with_anti_loop(monkeypatch):
    """Ollama trả 500 'token repeat limit reached' khi model lặp chữ => thử lại với repeat_penalty + nhiệt độ cao hơn."""
    sent = []

    class R:
        def __init__(self, n):
            self.status_code = 500 if n == 1 else 200
            self.text = '{"error":"prediction aborted, token repeat limit reached"}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}}
    monkeypatch.setattr(llm.requests, "post", lambda url, json, timeout, stream=False: sent.append(json) or R(len(sent)))
    monkeypatch.setattr(llm.registry, "pick", lambda role: "m")
    monkeypatch.setattr(llm.registry, "production", lambda role: "m")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat([{"role": "user", "content": "x"}], role="chat")["message"]["content"] == "ok"
    assert "repeat_penalty" not in sent[0]["options"]
    assert sent[1]["options"]["repeat_penalty"] > 1 and sent[1]["options"]["temperature"] >= 0.4
