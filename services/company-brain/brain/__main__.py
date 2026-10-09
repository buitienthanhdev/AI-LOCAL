"""CLI: python -m brain <lệnh>"""
import argparse
import json
import os
import sys
from pathlib import Path

if sys.stdout:  # pythonw (không có console) => stdout là None
    sys.stdout.reconfigure(encoding="utf-8")


def _j(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(prog="brain", description="Company Brain — Corporate Intelligence Platform (local)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="Quét thư mục vào catalog")
    s.add_argument("roots", nargs="*", help="thư mục cần quét")
    s.add_argument("--all", action="store_true", help="quét toàn bộ ổ cứng trong máy")
    sub.add_parser("index", help="Đọc nội dung + embed các file mới/đổi (bậc 1-2) vào kho tìm kiếm")
    for name in ("search", "ask"):
        p = sub.add_parser(name)
        p.add_argument("text")
        p.add_argument("--groups", default="all", help="nhóm của người hỏi, vd: rnd,hr")
        if name == "search":
            p.add_argument("--project")
            p.add_argument("--department")
        else:
            p.add_argument("-v", "--verbose", action="store_true")
            p.add_argument("--model", help="ép dùng model này (để so sánh)")
    sub.add_parser("stats", help="Thống kê catalog")
    sub.add_parser("serve", help="Chạy web nội bộ (kèm quét/index/bảo trì định kỳ)")
    sub.add_parser("widget", help="Nút nổi trên desktop")
    sub.add_parser("panel", help="(nội bộ) khung chat WebView của nút nổi")
    u = sub.add_parser("adduser", help="Tạo/đổi mật khẩu tài khoản web")
    u.add_argument("username")
    u.add_argument("--groups", default="", help="vd: rnd,sales (nhóm 'all' luôn có sẵn)")
    u.add_argument("--password", help="bỏ trống để nhập ẩn")
    sub.add_parser("users", help="Liệt kê tài khoản")
    f = sub.add_parser("facts", help="Trích fact cho tài liệu mới/đổi rồi in danh sách")
    f.add_argument("--status", help="lọc: conflicted, trusted, ...")
    f.add_argument("--no-extract", action="store_true")
    sub.add_parser("dupes", help="Các file trùng nội dung (cùng sha256)")

    # ---- trí tuệ doanh nghiệp
    c = sub.add_parser("contrib", help="Sổ đóng góp: ai đóng góp bao nhiêu (kèm bằng chứng)")
    c.add_argument("--project")
    c.add_argument("--person")
    c.add_argument("--days", type=int, default=365)
    g = sub.add_parser("graph", help="Duyệt đồ thị tri thức từ một thực thể")
    g.add_argument("entity")
    g.add_argument("--depth", type=int, default=2)
    gp = sub.add_parser("gaps", help="Lỗ hổng tri thức (--run để dò lại ngay)")
    gp.add_argument("--run", action="store_true")
    h = sub.add_parser("history", help="Lịch sử phiên bản + khác biệt nội dung của một file")
    h.add_argument("file_id", type=int)
    sub.add_parser("maintenance", help="Chạy ngay workflow bảo trì (đóng góp, Git, đồ thị, lỗ hổng, canary)")
    e = sub.add_parser("events", help="Hàng đợi sự kiện (bus bền)")
    e.add_argument("--status", default="%", help="pending|processing|done|dead")
    e.add_argument("--replay", action="store_true", help="đưa lại các sự kiện dead vào hàng đợi")
    sub.add_parser("metrics", help="Số liệu quan sát 1 giờ qua")

    # ---- model / benchmark / huấn luyện
    sub.add_parser("models", help="Registry model + model đang production")
    b = sub.add_parser("bench", help="Chạy benchmark nội bộ cho một model Ollama")
    b.add_argument("model")
    b.add_argument("--suites", default="core", help="core,facts")
    b.add_argument("--quick", action="store_true")
    sub.add_parser("backup", help="Sao lưu catalog.db ngay (online, nhất quán) + kiểm tra toàn vẹn")
    au = sub.add_parser("autonomy", help="Chế độ toàn quyền: bot tự thực hiện, không chờ bấm 'Thực hiện' (on/off)")
    au.add_argument("state", nargs="?", choices=["on", "off"])
    sub.add_parser("doctor", help="Tự kiểm tra sức khỏe: Ollama trùng, model trên GPU, VRAM, Qdrant, độ trễ, ổ đĩa")
    ae = sub.add_parser("agent-eval", help="Chấm điểm đầu-cuối: câu tiếng Việt tự nhiên -> đúng công cụ/tham số (dry-run)")
    ae.add_argument("--only", default=None, help="chỉ chạy case có id chứa chuỗi này")
    d = sub.add_parser("deploy", help="Quy trình bền: đăng ký -> benchmark -> canary (KHÔNG thẳng production)")
    d.add_argument("model")
    d.add_argument("--percent", type=int, default=10)
    d.add_argument("--quick", action="store_true")
    pr = sub.add_parser("promote", help="Đưa model đã benchmark/canary lên production")
    pr.add_argument("model")
    rb = sub.add_parser("rollback", help="Hoàn tác model của một vai trò")
    rb.add_argument("--role", default="chat")
    sub.add_parser("canary-eval", help="Đánh giá canary đang chạy: promote hoặc rollback")
    sub.add_parser("dataset", help="Dựng dataset huấn luyện từ tri thức đã kiểm chứng")
    sub.add_parser("export-dataset", help="(cũ) = dataset")
    t = sub.add_parser("train", help="Kiểm tra điều kiện / chạy huấn luyện LoRA (chỉ khi đủ điều kiện)")
    t.add_argument("--run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "scan":
        from .catalog import connect
        from .scanner import backfill_tiers, fixed_drives, scan
        roots = fixed_drives() if a.all else [Path(r) for r in a.roots]
        if not roots:
            ap.error("cần đường dẫn hoặc --all")
        conn = connect()
        backfill_tiers(conn)
        conn.close()
        for r in roots:
            print(f"Quét {r}", flush=True)
            print(json.dumps(scan(r), ensure_ascii=False), flush=True)
    elif a.cmd == "index":
        from .index import index_all
        if os.environ.get("BRAIN_FORCE") == "1":  # người dùng yêu cầu học ngay (Trợ lý: knowledge.update) => không nhường máy
            from . import governor
            governor.set_force(True)
        print(json.dumps(index_all(progress=lambda m, p=None: print(m, flush=True)), ensure_ascii=False))
    elif a.cmd == "search":
        from .index import search
        for h_ in search(a.text, a.groups.split(","), project=a.project, department=a.department):
            snippet = h_["text"].replace("\n", " ")[:160]
            print(f"{h_['score']:7.1f}  {h_['name']} · {h_['loc']}  [{h_['why']}]  ({h_['author']}, {h_['modified_at']})\n"
                  f"         {snippet}")
    elif a.cmd == "ask":
        from .agent import ask
        res = ask(a.text, a.groups.split(","), verbose=a.verbose, model=a.model)
        print(res["answer"])
        print(f"\n[độ tin cậy: {res['confidence']} · kiểm chứng: {res['verification']['verdict']} · model: {res['model']}]")
    elif a.cmd == "serve":
        from .server import serve
        serve()
    elif a.cmd == "widget":
        from .widget import main as widget_main
        widget_main()
    elif a.cmd == "panel":
        from .panel import main as panel_main
        panel_main()
    elif a.cmd == "adduser":
        import getpass
        from .auth import set_user
        pw = a.password or getpass.getpass("Mật khẩu: ")
        set_user(a.username, pw, [g_ for g_ in a.groups.split(",") if g_])
        print(f"Đã lưu tài khoản {a.username}")
    elif a.cmd == "users":
        from .auth import load_users
        for name, u_ in load_users().items():
            print(name, u_["groups"])
    elif a.cmd == "facts":
        from .catalog import connect
        if not a.no_extract:
            from .facts import extract_facts_pending
            print(json.dumps(extract_facts_pending(progress=print), ensure_ascii=False))
        conn = connect()
        q = "SELECT x.*, f.name FROM facts x JOIN files f ON f.file_id=x.source_file_id WHERE x.status != 'rejected'"
        args = []
        if a.status:
            q += " AND x.status=?"
            args.append(a.status)
        for r in conn.execute(q + " ORDER BY x.subject_key, x.predicate, x.valid_from", args):
            to = (r["valid_to"] or "nay")[:10]
            print(f"[{r['status']:<10} {r['confidence']:.2f}] {r['subject']} | {r['predicate']} | {r['value']}"
                  f"{' ' + r['unit'] if r['unit'] else ''}  ({(r['valid_from'] or '?')[:10]} → {to}; {r['name']})"
                  + (f"\n      ⚠ {r['note']}" if r["note"] else ""))
    elif a.cmd == "contrib":
        from . import contrib
        from .catalog import connect
        conn = connect()
        contrib.backfill(conn)
        conn.commit()
        _j(contrib.report(conn, ["admin"], True, a.project, a.person, a.days))
    elif a.cmd == "graph":
        from . import graph
        from .catalog import connect
        conn = connect()
        graph.rebuild(conn)
        _j(graph.neighbors(conn, a.entity, ["admin"], a.depth))
    elif a.cmd == "gaps":
        from . import gaps
        from .catalog import connect
        conn = connect()
        if a.run:
            _j(gaps.run(conn))
        for g_ in gaps.list_gaps(conn):
            print(f"[{g_['kind']:<26}] {g_['subject']}: {g_['detail']}")
    elif a.cmd == "history":
        from .agent import _file_history
        _j(_file_history(a.file_id, ["admin"]))
    elif a.cmd == "maintenance":
        from . import workflow, workflows_def  # noqa: F401
        _j(workflow.run(workflow.start("MAINTENANCE", {})))
    elif a.cmd == "events":
        from . import watcher
        if a.replay:
            print("Đã đưa lại", watcher.replay(), "sự kiện")
        from .catalog import connect
        for r in connect().execute("SELECT event_type, object_id, processing_status, attempts, error FROM events "
                                   "WHERE processing_status LIKE ? ORDER BY ts DESC LIMIT 30", (a.status,)):
            print(dict(r))
    elif a.cmd == "metrics":
        from . import obs
        _j(obs.summary())
    elif a.cmd == "models":
        from . import registry
        registry.bootstrap()
        _j(registry.status())
    elif a.cmd == "autonomy":
        from .actions import auto_approve
        on = auto_approve(None if a.state is None else a.state == "on")
        print("Toàn quyền: BẬT (bot tự làm, không hỏi)" if on else "Toàn quyền: TẮT (chờ bấm 'Thực hiện')")
    elif a.cmd == "backup":
        from . import backup
        print(backup.run_backup(force=True))
        print(backup.run_integrity())
    elif a.cmd == "doctor":
        from . import doctor
        print(doctor.text())
    elif a.cmd == "agent-eval":
        from . import agent_eval, registry
        registry.bootstrap()
        agent_eval.run(a.only, progress=lambda s: print(s, flush=True))
    elif a.cmd == "bench":
        from . import bench, registry
        registry.bootstrap()
        registry.register(a.model, "chat") if not registry.get(a.model) else None
        r = bench.run_suite(a.model, suites=a.suites.split(","), quick=a.quick)
        _j(r)
    elif a.cmd == "deploy":
        from . import workflow, workflows_def  # noqa: F401
        from . import registry
        registry.bootstrap()
        wid = workflow.start("DEPLOY_MODEL", {"model": a.model, "canary_percent": a.percent, "quick": a.quick,
                                              "actor": "cli"})
        _j(workflow.run(wid))
    elif a.cmd == "promote":
        from . import registry
        _j(registry.promote(a.model, "cli", "yêu cầu thủ công"))
    elif a.cmd == "rollback":
        from . import registry
        _j(registry.rollback(a.role, "cli", "yêu cầu thủ công"))
    elif a.cmd == "canary-eval":
        from . import registry
        _j(registry.evaluate_canary("chat", "cli"))
    elif a.cmd in ("dataset", "export-dataset"):
        from . import learning
        _j(learning.build_dataset())
    elif a.cmd == "train":
        from . import train
        _j(train.train() if a.run else train.preflight())
    elif a.cmd in ("stats", "dupes"):
        from .catalog import connect
        conn = connect()
        if a.cmd == "stats":
            q = """SELECT tier, status, COUNT(*) n, SUM(size) bytes, SUM(indexed_sha256 = sha256) n_indexed,
                          SUM(extract_error IS NOT NULL) errors FROM files GROUP BY tier, status ORDER BY tier"""
        else:
            q = """SELECT sha256, COUNT(*) n, GROUP_CONCAT(path, '  |  ') paths FROM files
                   WHERE status='active' AND tier=1 GROUP BY sha256 HAVING n > 1"""
        for r in conn.execute(q):
            print(dict(r))


if __name__ == "__main__":
    main()
