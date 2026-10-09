"""Web nội bộ (chỉ dùng thư viện chuẩn): chat, tìm kiếm, tải file — tất cả đều lọc theo quyền."""
import base64
import json
import mimetypes
import re
import time
from datetime import datetime
import socket
import sqlite3
import threading
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

from . import auth, jobs
from .catalog import connect
from .config import ADMIN_GROUP, CHAT_MODEL, DATA_DIR, MAX_ATTACH_MB, WEB_HOST, WEB_PORT
from .server_acl import can_read

WEB_DIR = Path(__file__).parent / "web"
LOCAL_ADDRS = {"127.0.0.1", "::1"}
LOCAL_SESSION = {"user": "admin (máy chủ)", "groups": [ADMIN_GROUP]}
_llm_slots = threading.Semaphore(2)  # 1 GPU: giới hạn số câu hỏi xử lý song song


def _is_ip(name: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _allowed_names() -> set[str]:
    h = socket.gethostname().lower()
    return {"localhost", h, h + ".local"}


_fails: dict[str, list[float]] = {}  # IP -> thời điểm đăng nhập sai
LOGIN_MAX_FAILS, LOGIN_WINDOW_S = 5, 900


def _login_blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
    return len(_fails[ip]) >= LOGIN_MAX_FAILS


class Handler(BaseHTTPRequestHandler):
    server_version = "CompanyBrain/0.1"

    # ---------- tiện ích ----------
    def _token(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c["cb_session"].value if "cb_session" in c else None

    def _session(self):
        if self.client_address[0] in LOCAL_ADDRS:  # người ngồi trên chính máy chủ = admin
            return LOCAL_SESSION
        return auth.session(self._token())

    def _host_ok(self) -> bool:
        """Chống DNS rebinding: trang web lạ trỏ tên miền của nó về 127.0.0.1 sẽ được trình duyệt coi là 'cùng nguồn'
        và đọc/ghi được API admin. Chỉ chấp nhận Host là IP, localhost, hoặc tên máy này; Origin (nếu có) phải trùng Host."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0].strip("[]") if not host.startswith("[") else host[1:host.find("]")]
        if not name or not (_is_ip(name) or name in _allowed_names()):
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin != "null" and urlparse(origin).netloc != host:
            return False
        return True

    def _json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        if "application/json" not in self.headers.get("Content-Type", ""):
            return {}  # chặn form POST chéo trang
        n = int(self.headers.get("Content-Length") or 0)
        if n > (MAX_ATTACH_MB * 4 + 2) * 1024 * 1024:  # 4 file đính kèm (base64 ~ +33%)
            raise ValueError("Yêu cầu quá lớn")
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, fmt, *args):
        pass

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if not self._host_ok():
                return self._json({"error": "Host/Origin không hợp lệ"}, 403)
            if url.path in ("/", "/index.html", "/panel"):
                # /panel: giao diện khung chat của nút AI (chỉ cho máy này)
                if url.path == "/panel" and self.client_address[0] not in LOCAL_ADDRS:
                    return self._json({"error": "Không tìm thấy"}, 404)
                body = (WEB_DIR / ("panel.html" if url.path == "/panel" else "index.html")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/health":
                self._json({"ok": True})
            elif url.path == "/api/doctor":
                s = self._session()
                if not s or ADMIN_GROUP not in s["groups"]:
                    return self._json({"checks": []})
                from . import doctor
                self._json({"checks": doctor.run()})
            elif url.path == "/api/me":
                s = self._session()
                self._json({"user": s["user"], "groups": s["groups"], "admin": ADMIN_GROUP in s["groups"]}
                           if s else {"user": None})
            elif url.path == "/api/status":
                s = self._session()
                if not s:
                    return self._json({"error": "Chưa đăng nhập"}, 401)
                conn = connect()
                stats = dict(conn.execute("""SELECT COUNT(*) files, SUM(indexed_sha256 = sha256) n_indexed,
                    SUM(acl='private') private FROM files WHERE status='active'""").fetchone())
                facts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM facts GROUP BY status")}
                qa = dict(conn.execute("""SELECT COUNT(*) total, SUM(rating=1) good, SUM(rating=-1) bad
                                          FROM qa_log""").fetchone())
                conn.close()
                from . import watcher
                self._json({"job": jobs.state, "catalog": stats, "facts": facts, "qa": qa,
                            "watcher": watcher.state} if ADMIN_GROUP in s["groups"]
                           else {"job": {"running": jobs.state["running"], "last_run": jobs.state["last_run"]}})
            elif url.path.startswith("/api/file/"):
                self._file(url)
            elif url.path == "/api/facts":
                self._facts(url)
            elif url.path == "/api/memories":
                s = self._session()
                if not s:
                    return self._json({"error": "Chưa đăng nhập"}, 401)
                from .memory import list_for
                self._json({"memories": list_for(s["groups"], s["user"])})
            elif url.path == "/api/open_rules":
                s = self._session()
                if not s or ADMIN_GROUP not in s["groups"]:
                    return self._json({"rules": []})
                from . import launcher
                self._json({"rules": launcher._grouped(launcher.list_rules())})
            elif url.path == "/api/progress":
                s = self._session()
                if not s:
                    return self._json({"error": "Chưa đăng nhập"}, 401)
                from .progress import compute
                admin = ADMIN_GROUP in s["groups"]
                p = compute(detail=admin)  # thống kê toàn máy (đường dẫn riêng tư) chỉ cho admin
                p["job"] = {k: jobs.state.get(k) for k in ("running", "message", "last_run", "force")}
                from . import governor
                governor.target_duty()  # làm mới chế độ/ghi chú theo tải hiện tại
                p["limit"] = {"percent": governor.state["limit"], "duty": governor.duty(),
                              "cpu_cap": governor.state["cpu_cap"], "paused_ram": governor.state["paused_ram"],
                              "mode": governor.state["mode"], "target": int(100 * governor.state["target"]),
                              "note": governor.state["note"], "ext_cpu": governor.state["ext_cpu"],
                              "ext_gpu": governor.state["ext_gpu"]}
                if admin:
                    p["job"]["current_path"] = jobs.state.get("current_path")
                    from . import gui, tasks
                    p["unread"] = tasks.unread(s["user"])
                    p["gui"] = gui.status()  # bot đang có quyền điều khiển chuột/bàn phím?
                self._json(p)
            elif url.path == "/api/tasks":
                s = self._session()
                if not s or ADMIN_GROUP not in s["groups"]:
                    return self._json({"tasks": [], "notifications": [], "skills": []})
                from . import skills, tasks
                self._json({"tasks": [t | {"desc": tasks.describe(t)} for t in tasks.list_tasks(s["user"])],
                            "notifications": tasks.notifications(s["user"]),
                            "skills": skills.list_all()})
            else:
                s = self._session()
                if not s:
                    return self._json({"error": "Chưa đăng nhập"}, 401)
                from . import api_ext
                r = api_ext.get(url.path, url.query, s)
                self._json(*(r[::-1] if r else ({"error": "Không tìm thấy"}, 404)))
        except Exception:
            traceback.print_exc()
            self._json({"error": "Lỗi máy chủ"}, 500)

    def _facts(self, url):
        """Danh sách fact (lọc quyền theo file nguồn). ?status=conflicted&q=m500"""
        s = self._session()
        if not s:
            return self._json({"error": "Chưa đăng nhập"}, 401)
        qs = parse_qs(url.query)
        status, q = (qs.get("status") or [""])[0], (qs.get("q") or [""])[0]
        from .facts import norm
        sql = """SELECT x.*, f.name AS source_name, f.acl FROM facts x JOIN files f ON f.file_id=x.source_file_id
                 WHERE x.status != 'rejected'"""
        args = []
        if status:
            sql += " AND x.status=?"
            args.append(status)
        if q:
            sql += " AND (x.subject_key LIKE ? OR x.value_key LIKE ?)"
            args += [f"%{norm(q)}%"] * 2
        sql += " ORDER BY x.status='conflicted' DESC, x.subject_key, x.predicate_key, x.valid_from LIMIT 500"
        conn = connect()
        rows = [dict(r) for r in conn.execute(sql, args) if can_read(r["acl"], s["groups"])]
        conn.close()
        self._json({"facts": rows})

    def _file(self, url):
        s = self._session()
        if not s:
            return self._json({"error": "Chưa đăng nhập"}, 401)
        try:
            fid = int(url.path.rsplit("/", 1)[1])
        except ValueError:
            return self._json({"error": "id sai"}, 400)
        conn = connect()
        f = conn.execute("SELECT path, name, acl FROM files WHERE file_id=? AND status='active'", (fid,)).fetchone()
        conn.close()
        if not f or not can_read(f["acl"], s["groups"]):
            return self._json({"error": "Không có quyền hoặc file không tồn tại"}, 404)
        p = Path(f["path"])
        if not p.is_file():
            return self._json({"error": "File đã bị di chuyển/xóa khỏi ổ đĩa"}, 410)
        inline = parse_qs(url.query).get("inline") == ["1"]
        ctype = mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition",
                         f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f['name'])}")
        self.end_headers()
        with open(p, "rb") as fh:
            while chunk := fh.read(1 << 16):
                self.wfile.write(chunk)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if not self._host_ok():
                return self._json({"error": "Host/Origin không hợp lệ"}, 403)
            data = self._body()
            if path == "/api/login":
                ip = self.client_address[0]
                if _login_blocked(ip):
                    return self._json({"error": "Sai quá 5 lần — thử lại sau 15 phút"}, 429)
                token = auth.login(str(data.get("username", "")), str(data.get("password", "")))
                if not token:
                    _fails.setdefault(ip, []).append(time.time())
                    from . import obs
                    obs.audit(ip, "login_failed", str(data.get("username", ""))[:80], None)
                    return self._json({"error": "Sai tài khoản hoặc mật khẩu"}, 401)
                _fails.pop(ip, None)
                return self._json({"ok": True}, headers={
                    "Set-Cookie": f"cb_session={token}; HttpOnly; SameSite=Strict; Path=/"})
            if path == "/api/logout":
                auth.logout(self._token())
                return self._json({"ok": True}, headers={"Set-Cookie": "cb_session=; Max-Age=0; Path=/"})

            s = self._session()
            if not s:
                return self._json({"error": "Chưa đăng nhập"}, 401)
            if path == "/api/ask":
                q = str(data.get("question", "")).strip()[:2000]
                history = [{"role": h["role"], "content": str(h["content"])[:4000]}
                           for h in data.get("history", []) if h.get("role") in ("user", "assistant")]
                images, extra = [], ""
                atts = data.get("attachments") or []
                if len(atts) > 4:
                    return self._json({"error": "Tối đa 4 file đính kèm"}, 400)
                from .llm import foreground
                from .media import prepare_attachment
                err = None
                with foreground():  # đọc file đính kèm là việc của người dùng: việc học nền nhường GPU
                    for att in atts:
                        try:
                            raw = base64.b64decode(str(att.get("data", "")), validate=True)
                        except ValueError:
                            err = "File đính kèm lỗi"
                            break
                        if len(raw) > MAX_ATTACH_MB * 1024 * 1024:
                            err = f"File đính kèm tối đa {MAX_ATTACH_MB} MB"
                            break
                        try:
                            with _llm_slots:
                                imgs, txt = prepare_attachment(str(att.get("name", "file"))[:200], raw)
                        except ValueError as e:
                            err = str(e)
                            break
                        images += imgs
                        extra += txt
                if err:
                    return self._json({"error": err}, 400)
                if not q and not atts:
                    return self._json({"error": "Câu hỏi trống"}, 400)
                q = q or "Hãy mô tả / tóm tắt nội dung file đính kèm."
                from . import agent
                from .agent import ask
                t0 = time.time()
                streaming = bool(data.get("stream"))
                if streaming:  # NDJSON: {"step": "..."} từng bước khi bot làm, cuối cùng {"result": {...}}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True

                    def emit(label: str):
                        try:
                            self.wfile.write((json.dumps({"step": label}, ensure_ascii=False) + "\n").encode())
                            self.wfile.flush()
                        except OSError:
                            pass  # người dùng đóng panel giữa chừng: vẫn trả lời xong để ghi nhật ký
                    agent.progress.cb = emit
                    emit("Đang chờ lượt GPU…" if _llm_slots._value == 0 else "Đang đọc câu hỏi…")
                try:
                    with _llm_slots:
                        res = ask(q + extra, s["groups"], history=history, images=images, user=s["user"])
                except Exception as e:
                    if not streaming:
                        raise
                    traceback.print_exc()  # header đã gửi => báo lỗi bằng dòng kết quả, không gửi trang lỗi 500
                    self.wfile.write((json.dumps({"result": {"answer": f"Lỗi máy chủ: {type(e).__name__}", "sources": [],
                                                             "actions": []}}, ensure_ascii=False) + "\n").encode())
                    return
                finally:
                    agent.progress.cb = None
                from . import obs
                obs.metric("ask_latency_s", time.time() - t0)  # thời gian người dùng thực sự chờ (cho tab Sức khỏe)
                if atts:
                    q += f"  [đính kèm: {', '.join(str(a.get('name')) for a in atts)}]"
                res["qa_id"] = None
                try:  # ghi nhật ký không được làm hỏng câu trả lời (vd. DB đang bận vì quét)
                    conn = connect()
                    cur = conn.execute("""INSERT INTO qa_log (user, groups, question, answer, sources, steps, model,
                                          created_at, latency_s) VALUES (?,?,?,?,?,?,?,?,?)""",
                                       (s["user"], ",".join(s["groups"]), q, res["answer"],
                                        json.dumps(res["sources"], ensure_ascii=False),
                                        json.dumps(res["steps"], ensure_ascii=False), res.get("model") or CHAT_MODEL,
                                        datetime.now().isoformat(timespec="seconds"), round(time.time() - t0, 2)))
                    conn.commit()
                    res["qa_id"] = cur.lastrowid
                    conn.close()
                except sqlite3.Error:
                    traceback.print_exc()
                if history:  # tự học trong nền: người dùng có đang sửa sai / xác nhận / dặn dò không
                    from .memory import learn_async
                    learn_async(history, str(data.get("question", "")).strip()[:400], s["user"], s["groups"],
                                res["qa_id"])
                if streaming:
                    try:
                        self.wfile.write((json.dumps({"result": res}, ensure_ascii=False, default=str) + "\n").encode())
                    except OSError:
                        pass
                    return
                return self._json(res)
            if path == "/api/action":
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin"}, 403)
                from .actions import execute
                return self._json(execute(str(data.get("id", "")), s["user"], bool(data.get("confirm"))))
            if path == "/api/feedback":
                rating = data.get("rating")
                if rating not in (1, -1):
                    return self._json({"error": "rating phải là 1 hoặc -1"}, 400)
                conn = connect()
                n = conn.execute("""UPDATE qa_log SET rating=?, comment=?, rated_at=? WHERE qa_id=? AND user=?""",
                                 (rating, str(data.get("comment") or "")[:2000] or None,
                                  datetime.now().isoformat(timespec="seconds"), int(data.get("qa_id", 0)),
                                  s["user"])).rowcount
                conn.commit()
                qa = conn.execute("SELECT * FROM qa_log WHERE qa_id=?", (int(data.get("qa_id", 0)),)).fetchone()
                if n and qa:
                    from . import contrib
                    contrib.record(conn, s["user"], "feedback", datetime.now().isoformat(timespec="seconds"),
                                   f"fb:{qa['qa_id']}", None, None, None, f"đánh giá câu hỏi #{qa['qa_id']}")
                    conn.commit()
                conn.close()
                if n and qa:  # biến đánh giá thành kinh nghiệm (chạy nền vì cần embedding)
                    from .memory import learn_from_feedback
                    threading.Thread(target=learn_from_feedback, daemon=True,
                                     args=(dict(qa), rating, data.get("comment"), s["groups"])).start()
                return self._json({"ok": bool(n)}, 200 if n else 404)
            if path == "/api/transcribe":  # giọng nói (WAV base64) -> chữ, dùng cho nút micro
                try:
                    raw = base64.b64decode(str(data.get("data", "")), validate=True)
                except ValueError:
                    return self._json({"error": "Audio lỗi"}, 400)
                if len(raw) > MAX_ATTACH_MB * 1024 * 1024:
                    return self._json({"error": "Audio quá dài"}, 400)
                import tempfile
                from pathlib import Path as _P
                from .media import transcribe
                with tempfile.TemporaryDirectory() as d, _llm_slots:
                    src = _P(d) / "voice.wav"
                    src.write_bytes(raw)
                    text = transcribe(src, _P(d))
                text = re.sub(r"^\[\d\d:\d\d\]\s*", "", text, flags=re.M).replace("\n", " ").strip()
                return self._json({"text": "" if "[không có lời nói]" in text else text})
            if path == "/api/gui/stop":  # dừng điều khiển chuột/bàn phím (ai đăng nhập cũng được bấm — dừng luôn an toàn)
                from . import gui
                gui.revoke("người dùng bấm Dừng trên panel")
                return self._json({"ok": True})
            if path in ("/api/tasks/set", "/api/notifications/read", "/api/skills/delete"):
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin"}, 403)
                from . import skills, tasks
                if path == "/api/tasks/set":  # active: 1 bật, 0 tạm dừng, null xóa
                    return self._json({"ok": tasks.set_active(int(data.get("task_id", 0)), s["user"], data.get("active"))})
                if path == "/api/notifications/read":
                    tasks.mark_read(s["user"])
                    return self._json({"ok": True})
                return self._json({"ok": skills.delete_by_id(int(data.get("skill_id", 0)))})
            if path == "/api/open_rules/remove":
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin"}, 403)
                from . import launcher
                return self._json(launcher.remove_rules(data.get("exts") or []))
            if path == "/api/memories/toggle":
                from .memory import set_active
                ok = set_active(int(data.get("mem_id", 0)), bool(data.get("active")), s["groups"], s["user"])
                return self._json({"ok": ok}, 200 if ok else 403)
            if path == "/api/facts/review":
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin được duyệt tri thức"}, 403)
                from .facts import review
                ok = review(int(data.get("fact_id", 0)), str(data.get("action")), s["user"],
                            str(data.get("note") or "")[:500] or None)
                if ok:
                    from . import contrib, obs
                    c2 = connect()
                    f = c2.execute("""SELECT x.*, f.project, f.department FROM facts x JOIN files f ON f.file_id=x.source_file_id
                                      WHERE x.fact_id=?""", (int(data["fact_id"]),)).fetchone()
                    if f:
                        kind = "fact_validated" if data.get("action") == "approve" else "fact_rejected"
                        contrib.record(c2, s["user"], kind, datetime.now().isoformat(timespec="seconds"),
                                       f"review:{f['fact_id']}:{kind}", f["source_file_id"], f["project"], f["department"],
                                       f"{f['subject']} · {f['predicate']} = {f['value']}")
                        c2.commit()
                    c2.close()
                    obs.audit(s["user"], "fact_" + str(data.get("action")), str(data.get("fact_id")),
                              str(data.get("note") or ""))
                return self._json({"ok": ok}, 200 if ok else 400)
            if path == "/api/search":
                from .index import search
                hits = search(str(data.get("query", ""))[:500], s["groups"], limit=15)
                return self._json({"hits": [{k: h[k] for k in ("score", "file_id", "name", "path", "loc", "author",
                                                               "modified_at", "text", "why")} for h in hits]})
            if path == "/api/reindex":
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin"}, 403)
                jobs.trigger(force=bool(data.get("force")))
                return self._json({"ok": True})
            if path == "/api/learn/normal":  # thôi cưỡng chế: quay về quy tắc cũ (≤ 60% tài nguyên, chỉ khi máy rảnh)
                if ADMIN_GROUP not in s["groups"]:
                    return self._json({"error": "Chỉ admin"}, 403)
                was = bool(jobs.state.get("force"))
                jobs.state["force"] = False
                from . import governor
                governor.set_force(False)  # trả giới hạn CPU, về học thích ứng
                from . import obs
                obs.audit(s["user"], "learn_force_off", None, "về chế độ học ngầm thường")
                return self._json({"ok": True, "was_forced": was})
            from . import api_ext
            r = api_ext.post(path, data, s)
            self._json(*(r[::-1] if r else ({"error": "Không tìm thấy"}, 404)))
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"Lỗi máy chủ: {type(e).__name__}"}, 500)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _log_to_file():
    """pythonw không có console: stdout/stderr = None => mọi traceback bị mất. Ghi vào data/logs/server.log (xoay 5 MB)."""
    import io
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    f = log_dir / "server.log"
    if f.exists() and f.stat().st_size > 5 * 2**20:
        f.replace(log_dir / "server.1.log")
    stream = io.TextIOWrapper(open(f, "ab", buffering=0), encoding="utf-8", errors="replace", line_buffering=True,
                              write_through=True)
    import sys
    if sys.stderr is None or getattr(sys, "frozen", False) or "pythonw" in sys.executable.lower():
        sys.stderr = stream
        sys.stdout = stream
    sys.stderr.write(f"\n==== start {datetime.now().isoformat(timespec='seconds')} ====\n")


def serve():
    _log_to_file()
    # Mở cổng web NGAY để nút AI kết nối được; phần nặng (nạp kho vector ~1 phút) chạy nền.
    httpd = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    jobs.lower_priority()
    from . import governor
    governor.cap_cpu_of_this_process()  # chặn cứng CPU máy chủ ở RESOURCE_LIMIT

    def warm_up():
        from . import registry
        registry.bootstrap()  # đảm bảo registry có model production mặc định
        from .index import client
        client()  # bật Qdrant server cục bộ (nếu chưa chạy) và kết nối
        from . import watcher
        watcher.start(jobs.scan_roots(), on_changed=watcher.realtime_index)
        jobs.start_background(now=True)
    from . import tasks
    tasks.start()  # hẹn giờ / nhắc việc / việc lặp lại
    threading.Thread(target=warm_up, daemon=True, name="warm-up").start()
    print(f"Company Brain đang chạy:\n  Trên máy này : http://127.0.0.1:{WEB_PORT}\n"
          f"  Trong LAN    : http://{lan_ip()}:{WEB_PORT}", flush=True)
    httpd.serve_forever()
