"""API mở rộng của máy chủ: đóng góp, đồ thị, lỗ hổng tri thức, model/benchmark/triển khai, số liệu hệ thống, sự kiện,
phiên bản, dataset/huấn luyện. Chỉ admin được gọi các thao tác thay đổi hệ thống; dữ liệu đọc luôn lọc theo quyền."""
import threading
import time
from urllib.parse import parse_qs

from . import graph as graph_mod
from . import obs
from .catalog import connect
from .config import ADMIN_GROUP

_tasks: dict[str, dict] = {}   # tác vụ nền do người dùng kích hoạt (benchmark, deploy...) — để UI xem tiến độ


def _bg(name: str, fn):
    """Chạy fn trong luồng nền, lưu trạng thái/kết quả vào _tasks[name]."""
    if _tasks.get(name, {}).get("running"):
        return {"started": False, "reason": "đang chạy"}
    _tasks[name] = {"running": True, "started": time.time(), "result": None, "error": None}

    def run():
        try:
            _tasks[name]["result"] = fn()
        except Exception as e:
            _tasks[name]["error"] = f"{type(e).__name__}: {e}"
        finally:
            _tasks[name]["running"] = False
    threading.Thread(target=run, daemon=True, name=f"task-{name}").start()
    return {"started": True}


def _q(url_query: str, key: str, default=""):
    return (parse_qs(url_query).get(key) or [default])[0]


def get(path: str, query: str, s: dict) -> tuple[int, dict] | None:
    """Trả (status, json) nếu xử lý; None nếu không phải route của module này."""
    admin = ADMIN_GROUP in s["groups"]
    deny = (403, {"error": "Chỉ admin"})
    if path == "/api/contributions":
        from . import contrib
        conn = connect(readonly=True)
        try:
            return 200, contrib.report(conn, s["groups"], admin, _q(query, "project") or None, _q(query, "person") or None,
                                       int(_q(query, "days", "365")))
        finally:
            conn.close()
    if path == "/api/graph":
        conn = connect(readonly=True)
        try:
            return 200, graph_mod.neighbors(conn, _q(query, "entity"), s["groups"], int(_q(query, "depth", "2")))
        finally:
            conn.close()
    if path == "/api/versions":
        from .agent import _file_history
        return 200, _file_history(int(_q(query, "file_id", "0")), s["groups"])
    if path == "/api/gaps":
        if not admin:
            return deny
        from . import gaps
        conn = connect(readonly=True)
        try:
            return 200, {"gaps": gaps.list_gaps(conn, _q(query, "status", "open"))}
        finally:
            conn.close()
    if path == "/api/models":
        if not admin:
            return deny
        from . import registry
        from .bench import CORE
        out = registry.status()
        out["tasks"] = {k: {kk: vv for kk, vv in v.items() if kk != "started"} for k, v in _tasks.items()}
        out["bench_cases"] = len(CORE)
        conn = connect(readonly=True)
        out["bench_runs"] = [dict(r) for r in conn.execute(
            "SELECT run_id, model, suite, score, latency_p50, tokens_per_s, created_at FROM bench_runs "
            "ORDER BY run_id DESC LIMIT 10")]
        out["canary"] = registry.canary_stats("chat")
        conn.close()
        return 200, out
    if path == "/api/metrics":
        if not admin:
            return deny
        from . import governor, index, watcher
        m = obs.summary(int(_q(query, "window", "3600")))
        m["_governor"] = {"limit": governor.state["limit"], "duty": governor.duty(), "ram": governor.ram_load(),
                          "paused_ram": governor.state["paused_ram"], "cpu_cap": governor.state["cpu_cap"]}
        m["_watcher"] = watcher.state
        m["_qdrant"] = {"mode": __import__("brain.config", fromlist=["QDRANT_MODE"]).QDRANT_MODE,
                        "alive": index._qdrant_alive()}
        return 200, m
    if path == "/api/events":
        if not admin:
            return deny
        conn = connect(readonly=True)
        rows = [dict(r) for r in conn.execute(
            """SELECT event_id, event_type, object_id, processing_status, attempts, error, created_at, processed_at,
               previous_version, new_version FROM events WHERE processing_status LIKE ? ORDER BY ts DESC LIMIT 50""",
            (_q(query, "status", "%"),))]
        conn.close()
        return 200, {"events": rows}
    if path == "/api/datasets":
        if not admin:
            return deny
        from . import learning, train
        return 200, {"datasets": learning.list_datasets(), "preflight": train.preflight()}
    if path == "/api/workflows":
        if not admin:
            return deny
        conn = connect(readonly=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM workflows ORDER BY updated_at DESC LIMIT 20")]
        conn.close()
        return 200, {"workflows": rows}
    if path == "/api/audit":
        if not admin:
            return deny
        conn = connect(readonly=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY audit_id DESC LIMIT 50")]
        conn.close()
        return 200, {"audit": rows}
    return None


def post(path: str, data: dict, s: dict) -> tuple[int, dict] | None:
    admin = ADMIN_GROUP in s["groups"]
    if not path.startswith(("/api/models", "/api/events", "/api/dataset", "/api/train", "/api/gaps", "/api/maintenance",
                            "/api/people")):
        return None
    if not admin:
        return 403, {"error": "Chỉ admin"}
    actor = s["user"]
    from . import registry
    if path == "/api/models/register":
        obs.audit(actor, "model_register", data.get("name"))
        return 200, registry.register(str(data["name"]), str(data.get("role", "chat")), notes=data.get("notes"))
    if path == "/api/models/benchmark":
        from . import bench
        name, quick = str(data["name"]), bool(data.get("quick"))
        registry.register(name, str(data.get("role", "chat")))
        return 200, _bg(f"bench:{name}", lambda: bench.run_suite(name, suites=data.get("suites") or ["core"], quick=quick,
                                                                progress=None))
    if path == "/api/models/canary":
        return 200, registry.start_canary(str(data["name"]), int(data.get("percent", 10)), actor)
    if path == "/api/models/promote":
        obs.audit(actor, "model_promote", data.get("name"))
        return 200, registry.promote(str(data["name"]), actor, "admin duyệt")
    if path == "/api/models/rollback":
        obs.audit(actor, "model_rollback", data.get("role", "chat"))
        return 200, registry.rollback(str(data.get("role", "chat")), actor, "admin yêu cầu")
    if path == "/api/models/deploy":  # workflow bền: đăng ký -> benchmark -> canary
        from . import workflow, workflows_def  # noqa: F401
        wid = workflow.start("DEPLOY_MODEL", {"model": str(data["name"]), "role": data.get("role", "chat"),
                                              "canary_percent": int(data.get("percent", 10)), "actor": actor})
        return 200, {"workflow": wid, **_bg(f"deploy:{wid}", lambda: workflow.run(wid))}
    if path == "/api/events/replay":
        from . import watcher
        return 200, {"requeued": watcher.replay()}
    if path == "/api/dataset/build":
        from . import workflow, workflows_def  # noqa: F401
        wid = workflow.start("BUILD_TRAINING_DATASET", {})
        return 200, {"workflow": wid, "result": workflow.run(wid)}
    if path == "/api/train":
        from . import train
        return 200, train.train(data.get("dataset"))
    if path == "/api/gaps/resolve":
        conn = connect()
        conn.execute("UPDATE knowledge_gaps SET status=?, resolved_at=datetime('now','localtime') WHERE gap_id=?",
                     (str(data.get("status", "resolved")), int(data["gap_id"])))
        conn.commit()
        conn.close()
        obs.audit(actor, "gap_" + str(data.get("status", "resolved")), str(data["gap_id"]))
        return 200, {"ok": True}
    if path == "/api/maintenance/run":
        from . import workflow, workflows_def  # noqa: F401
        wid = workflow.start("MAINTENANCE", {})
        return 200, _bg("maintenance", lambda: workflow.run(wid)) | {"workflow": wid}
    if path == "/api/people/merge":
        from . import people
        conn = connect()
        people.merge(conn, int(data["keep"]), int(data["drop"]))
        conn.commit()
        conn.close()
        return 200, {"ok": True}
    return 404, {"error": "Không tìm thấy"}
