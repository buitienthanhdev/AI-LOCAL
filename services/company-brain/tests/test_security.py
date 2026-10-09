"""Bảo mật web: chống DNS rebinding (Host/Origin lạ), giới hạn đăng nhập sai, chặn form POST chéo trang."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def srv(env):
    from brain import server
    server._fails.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port, path, body=None, headers=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={**({"Content-Type": ctype} if data else {}),
                                                                                    **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_dns_rebinding_host_is_rejected(srv):
    assert _req(srv, "/api/me", headers={"Host": "evil.example.com"})[0] == 403
    assert _req(srv, "/api/ask", {"question": "x"}, headers={"Host": "evil.example.com"})[0] == 403
    assert _req(srv, "/api/me")[0] == 200  # Host 127.0.0.1:port hợp lệ


def test_cross_origin_post_is_rejected(srv):
    st, _ = _req(srv, "/api/tasks/set", {"task_id": 1, "active": None}, headers={"Origin": "https://evil.example.com"})
    assert st == 403
    st, _ = _req(srv, "/api/me", headers={"Origin": f"http://127.0.0.1:{srv}"})
    assert st == 200


def test_login_rate_limited_after_5_failures(srv, monkeypatch):
    from brain import auth
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    codes = [_req(srv, "/api/login", {"username": "x", "password": "sai"})[0] for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_form_post_without_json_is_ignored(srv):
    st, body = _req(srv, "/api/login", {"username": "x"}, ctype="text/plain")
    assert st == 401  # body bị bỏ qua (không phải JSON) — form chéo trang không gửi được dữ liệu


def test_ask_streams_progress_then_result(srv, monkeypatch):
    from brain import agent

    def fake_ask(q, groups, **k):
        agent._say("Đang tìm trong tài liệu…")
        agent._say(agent._STEP_LABEL["open_app"])
        return {"answer": "xong", "sources": [], "steps": [], "actions": []}
    monkeypatch.setattr(agent, "ask", fake_ask)
    r = urllib.request.Request(f"http://127.0.0.1:{srv}/api/ask", data=json.dumps({"question": "mở word", "stream": True})
                               .encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        assert "ndjson" in resp.headers["Content-Type"]
        lines = [json.loads(x) for x in resp.read().decode().splitlines() if x.strip()]
    steps = [m["step"] for m in lines if "step" in m]
    assert "Đang mở ứng dụng…" in steps and lines[-1]["result"]["answer"] == "xong"


def test_ask_without_stream_still_returns_json(srv, monkeypatch):
    from brain import agent
    monkeypatch.setattr(agent, "ask", lambda q, groups, **k: {"answer": "ok", "sources": [], "steps": [], "actions": []})
    st, body = _req(srv, "/api/ask", {"question": "x"})
    assert st == 200 and body["answer"] == "ok"
