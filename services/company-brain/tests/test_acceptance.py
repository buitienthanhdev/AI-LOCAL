"""8 tình huống nghiệm thu của đặc tả Corporate AI (xem docs). Mỗi test chứng minh một tình huống bằng hành vi thật của
hệ thống (watcher thật, DB thật, Qdrant bộ nhớ, workflow thật); chỉ embedding là bản giả xác định để chạy nhanh/không cần GPU."""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import file_row, scan_shared, write_docx


# ----------------------------------------------------------------------------- Case 1
def test_case1_new_file_is_learned_without_anyone_chatting(env):
    """File mới xuất hiện -> watcher thật phát hiện -> ingest -> extract metadata -> hiểu nội dung -> index ->
    hỏi sau đó tìm thấy. Không có người chat nào."""
    from brain import index, watcher
    watcher.state.update(last_event=None, processed=0)
    obs_ = watcher.start([env])
    try:
        path = write_docx(env / "RND" / "M500" / "Acronal_Report.docx", "Trần Thị Ly",
                          ["Báo cáo thử nghiệm", "Nhựa Acronal PS 673 cho độ bám dính tốt hơn nhựa thay thế giá rẻ."],
                          headings=True)
        indexed = []
        deadline = time.time() + 30
        while time.time() < deadline and not indexed:  # chờ watcher+worker (không gọi tay vào pipeline)
            time.sleep(0.5)
            row = file_row(path)
            if row and row["sha256"]:
                index.index_all(file_ids=[row["file_id"]])
                indexed.append(row["file_id"])
        assert indexed, "watcher không phát hiện file mới trong 30 giây"
        row = file_row(path)
        assert row["doc_author"] == "Trần Thị Ly" and row["tier"] == 1 and row["department"] == "RND" and row["project"] == "M500"
        assert row["indexed_sha256"] == row["sha256"], "file chưa được index xong"
        hits = index.search("nhựa Acronal bám dính", ["rnd"])
        assert hits and hits[0]["name"] == "Acronal_Report.docx"
        assert hits[0]["loc"], "kết quả phải có vị trí trong file (mục/trang)"
        from brain.catalog import connect
        c = connect()
        ev = c.execute("SELECT event_type, processing_status FROM events WHERE object_id=?", (str(path),)).fetchall()
        assert any(e["processing_status"] == "done" for e in ev)
        assert c.execute("SELECT COUNT(*) FROM file_events WHERE file_id=? AND event='discovered'",
                         (row["file_id"],)).fetchone()[0] == 1
    finally:
        watcher.stop(obs_)


# ----------------------------------------------------------------------------- Case 2
def test_case2_modified_file_creates_version_and_diff(env):
    from brain import index, watcher
    from brain.catalog import connect
    p = write_docx(env / "RND" / "M500" / "Formula.docx", "Nguyễn A", ["Acronal 28.5%", "TiO2 18%"])
    scan_shared(env)
    r1 = file_row(p)
    index.index_all()
    assert r1["version_no"] == 1
    time.sleep(1.1)
    write_docx(p, "Nguyễn A", ["Acronal 30%", "TiO2 18%", "Biocide 0.15%"], editor="Lê C")
    watcher.emit("FILE_MODIFIED", str(p.resolve()))
    changed = watcher.process_pending()
    assert changed == [r1["file_id"]]
    index.index_all(file_ids=changed)
    r2 = file_row(p)
    assert r2["version_no"] == 2
    c = connect()
    v = {x["version_no"]: x for x in c.execute("SELECT * FROM file_versions WHERE file_id=?", (r1["file_id"],))}
    assert set(v) == {1, 2}, "phải GIỮ phiên bản cũ"
    assert v[2]["editor"] == "Lê C", "biết AI sửa"
    assert v[2]["diff_added"] >= 2 and v[2]["diff_removed"] >= 1
    assert "Acronal 30%" in v[2]["diff_summary"] and "Acronal 28.5%" in v[2]["diff_summary"], "biết nội dung nào thay đổi"
    assert index.read_snapshot(v[1]["snapshot"]).count("28.5") == 1, "snapshot bản cũ còn nguyên"
    assert v[2]["observed_at"] > v[1]["observed_at"] or v[2]["observed_at"] == v[1]["observed_at"]


# ----------------------------------------------------------------------------- Case 3
def test_case3_conflicting_documents_are_flagged_not_resolved_by_guessing(env, monkeypatch):
    from brain import agent, facts
    from brain.catalog import connect

    def stub_llm_extract(name, text, model=None):  # test double: "chủ thể | quan hệ | giá trị" mỗi dòng
        return [dict(zip(("subject", "predicate", "value", "evidence"), (*ln.split("|"), ln))) for ln in text.splitlines()
                if ln.count("|") == 2 and ln.split("|")[1] in facts.PREDICATES]
    monkeypatch.setattr(facts, "llm_extract", stub_llm_extract)
    (env / "RND").mkdir()
    (env / "RND" / "Master_Spec.md").write_text("M500|target_ph|8.5\n", encoding="utf-8")
    (env / "RND" / "Lab_Report.md").write_text("M500|target_ph|8.7\n", encoding="utf-8")
    scan_shared(env)
    facts.extract_facts_pending()
    c = connect()
    rows = c.execute("SELECT value, status FROM facts WHERE predicate='target_ph'").fetchall()
    assert {r["status"] for r in rows} == {"conflicted"}, "hai nguồn khác nhau => CONFLICTED, không tự chọn"
    assert {r["value"] for r in rows} == {"8.5", "8.7"}, "giữ CẢ HAI giá trị"
    q = agent.Evidence()
    q.from_result(agent.run_sql("SELECT * FROM visible_facts WHERE predicate='target_ph'", ["rnd"]))
    assert len(q.conflicts) == 2 and agent._confidence("supported", q, "") == "conflicted"
    # người duyệt chọn 8.5 => 8.7 không còn là 'đáng tin', và có audit
    fid = next(r["fact_id"] for r in c.execute("SELECT fact_id, value FROM facts") if r["value"] == "8.5")
    assert facts.review(fid, "approve", "admin")
    st = {r["value"]: r["status"] for r in c.execute("SELECT value, status FROM facts WHERE predicate='target_ph'")}
    assert st["8.5"] == "approved" and st["8.7"] in ("conflicted", "superseded")


# ----------------------------------------------------------------------------- Case 4
def test_case4_no_permission_means_no_content_leak(env):
    from brain import agent, contrib, graph, index, sqlbox
    from brain.catalog import connect
    write_docx(env / "HR" / "Salary_2026.docx", "Phòng Nhân sự", ["Bảng lương: Nguyễn Văn A 45.000.000 VND, mã SALARYXYZ"])
    write_docx(env / "RND" / "Public.docx", "Ly", ["Tài liệu công khai về độ nhớt"])
    scan_shared(env)
    index.index_all()
    c = connect()
    graph.rebuild(c)
    contrib.backfill(c)
    c.commit()
    # tìm kiếm (cả vector lẫn từ khóa): nhóm rnd không thấy, hr thấy
    assert not any("Salary" in h["name"] for h in index.search("SALARYXYZ lương Nguyễn Văn A", ["rnd"]))
    assert any("Salary" in h["name"] for h in index.search("SALARYXYZ lương Nguyễn Văn A", ["hr"]))
    # SQL: không thấy trong view, không đọc được bảng gốc, không sửa/attach/pragma
    assert not [r for r in sqlbox.run_sql("SELECT name FROM visible_files", ["rnd"])["rows"] if "Salary" in r["name"]]
    for bad in ("SELECT * FROM files", "SELECT * FROM chunks", "SELECT * FROM sqlite_master", "DELETE FROM files",
                "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(files)", "SELECT * FROM people", "SELECT * FROM audit"):
        assert "error" in sqlbox.run_sql(bad, ["rnd"]), bad
    assert any("Salary" in r["name"] for r in sqlbox.run_sql("SELECT name FROM visible_files", ["hr"])["rows"])
    # công cụ khác: lịch sử phiên bản, đóng góp, đồ thị
    sal = file_row(env / "HR" / "Salary_2026.docx")
    assert "error" in agent._file_history(sal["file_id"], ["rnd"])
    rep = contrib.report(c, ["rnd"], False, since_days=None)
    assert all("Salary" not in e for r in rep["ranking"] for e in r["evidence"])
    assert "Salary" not in json.dumps(graph.neighbors(c, "Phòng Nhân sự", ["rnd"]), ensure_ascii=False)
    # toàn bộ câu trả lời của agent cho người không có quyền cũng không chứa nội dung (qua prefetch + tool)
    ev = agent.Evidence()
    pre = agent.prefetch("lương Nguyễn Văn A SALARYXYZ", ["rnd"], ev)
    assert "SALARYXYZ" not in json.dumps(pre, ensure_ascii=False) and "SALARYXYZ" not in ev.text(10000)


# ----------------------------------------------------------------------------- Case 5
def test_case5_crash_between_ingest_steps_recovers_without_duplicates(env, monkeypatch):
    from brain import index, watcher, workflow
    from brain.catalog import connect
    p = write_docx(env / "RND" / "A.docx", "Ly", ["Nội dung tài liệu A về nhựa Acronal"])
    # (a) trùng lặp: 20 sự kiện cho cùng object => 1 dòng pending
    for _ in range(20):
        watcher.emit("FILE_MODIFIED", str(p.resolve()))
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM events WHERE processing_status='pending'").fetchone()[0] == 1
    # (b) crash giữa chừng: nhận sự kiện nhưng "sập" trước khi xử lý xong
    claimed = watcher._claim(c)
    assert len(claimed) == 1
    assert c.execute("SELECT processing_status FROM events").fetchone()[0] == "processing"
    assert watcher.recover() == 1  # khởi động lại => đặt lại pending
    # (c) xử lý, rồi giả lập crash ngay khi index dở (lỗi giữa chừng) và thử lại
    changed = watcher.process_pending()
    boom = {"n": 0}
    real = index.embed

    def flaky(texts, query=False):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("GPU OOM (giả lập)")
        return real(texts, query)
    monkeypatch.setattr(index, "embed", flaky)
    stats = index.index_all(file_ids=changed)
    assert stats["errors"] == 1 and file_row(p)["indexed_sha256"] is None, "lỗi giữa chừng => KHÔNG đánh dấu đã index"
    c.execute("UPDATE files SET extract_error=NULL")
    c.commit()
    stats = index.index_all(file_ids=changed)
    assert stats["indexed"] == 1
    for _ in range(2):  # chạy lại (idempotent) không nhân đôi dữ liệu
        watcher.emit("FILE_MODIFIED", str(p.resolve()))
        watcher.process_pending()
        index.index_all(file_ids=[file_row(p)["file_id"]])
    fid = file_row(p)["file_id"]
    n_chunks = c.execute("SELECT COUNT(*) FROM chunks WHERE file_id=?", (fid,)).fetchone()[0]
    assert n_chunks == 1 and index.client().count("chunks").count == 1, "không có đoạn/vector trùng"
    assert c.execute("SELECT COUNT(*) FROM file_versions WHERE file_id=?", (fid,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM contributions WHERE file_id=?", (fid,)).fetchone()[0] == 1
    # (d) lỗi lặp lại => thử lại có lùi dần rồi vào dead-letter, replay được
    watcher.emit("FILE_MODIFIED", str(p.resolve()))
    monkeypatch.setattr(watcher, "process_file", lambda *a, **k: (_ for _ in ()).throw(OSError("khóa")))
    for _ in range(watcher.MAX_ATTEMPTS + 1):
        c.execute("UPDATE events SET last_at=0 WHERE processing_status='pending'")
        c.commit()
        watcher.process_pending()
    assert c.execute("SELECT processing_status FROM events ORDER BY ts DESC LIMIT 1").fetchone()[0] == "dead"
    monkeypatch.undo()
    assert watcher.replay() >= 1


def test_case5b_durable_workflow_resumes_from_failed_step(env):
    from brain import workflow
    calls = {"a": 0, "b": 0, "c": 0}

    def a(s):
        calls["a"] += 1
        return {"a": 1}

    def b(s):
        calls["b"] += 1
        if calls["b"] == 1:
            raise RuntimeError("worker crash")
        return {"b": 2}

    def c(s):
        calls["c"] += 1
        return {"c": s["a"] + s["b"]}
    workflow.define("T", [("a", a), ("b", b), ("c", c)])
    wid = workflow.start("T", {})
    r1 = workflow.run(wid)
    assert r1["status"] == "failed" and r1["step"] == 1
    res = workflow.resume_all()  # "khởi động lại"
    assert res[0]["status"] == "done" and res[0]["state"]["c"] == 3
    assert calls == {"a": 1, "b": 2, "c": 1}, "bước a đã xong KHÔNG chạy lại; bước b thử lại; c chạy một lần"
    assert workflow.get(wid)["status"] == "done"


# ----------------------------------------------------------------------------- Case 6
def test_case6_new_model_is_never_deployed_without_benchmark_and_rolls_back_on_regression(env):
    from brain import registry
    from brain.catalog import connect
    registry.bootstrap()
    prod = registry.production("chat")
    registry.register("model-moi:1", "chat")
    assert registry.get("model-moi:1")["status"] == "candidate"
    assert registry.pick("chat") == prod, "candidate chưa nhận lưu lượng"
    assert registry.start_canary("model-moi:1")["ok"] is False, "chưa benchmark => cấm canary"
    assert registry.promote("model-moi:1")["ok"] is False, "không được promote thẳng"
    registry.record_bench(prod, "core", 0.80, {}, 5.0, 30.0)
    registry.record_bench("model-moi:1", "core", 0.60, {}, 5.0, 30.0)   # tệ hơn production
    r = registry.start_canary("model-moi:1")
    assert r["ok"] is False and "thấp hơn" in r["error"], "regression trên benchmark => bị từ chối"
    registry.register("model-tot:1", "chat")
    registry.record_bench("model-tot:1", "core", 0.85, {}, 5.0, 30.0)
    assert registry.start_canary("model-tot:1", 30)["ok"]
    picks = {registry.pick("chat") for _ in range(200)}
    assert picks == {prod, "model-tot:1"}, "canary nhận một phần lưu lượng"
    # canary thật tệ hơn: nhiều đánh giá xấu => tự rollback
    c = connect()
    for i in range(14):
        c.execute("INSERT INTO qa_log (user, question, answer, model, created_at, latency_s, rating) VALUES (?,?,?,?,?,?,?)",
                  ("u", "q", "a", prod, "2999-01-01T00:00:00", 5.0, 1))
        c.execute("INSERT INTO qa_log (user, question, answer, model, created_at, latency_s, rating) VALUES (?,?,?,?,?,?,?)",
                  ("u", "q", "a", "model-tot:1", "2999-01-01T00:00:00", 5.0, -1))
    c.commit()
    # created_at ở tương lai để nằm trong cửa sổ thống kê
    d = registry.evaluate_canary("chat")
    assert d["decision"] == "rollback", d
    assert registry.production("chat") == prod and registry.get("model-tot:1")["status"] == "retired"
    assert {registry.pick("chat") for _ in range(50)} == {prod}
    hist = [r["action"] for r in c.execute("SELECT action FROM deployments ORDER BY dep_id")]
    assert "canary" in hist and "rollback" in hist
    # model tốt thật => lên production qua canary, và rollback khôi phục bản cũ
    registry.register("model-ok:1", "chat")
    registry.record_bench("model-ok:1", "core", 0.9, {}, 5.0, 30.0)
    registry.start_canary("model-ok:1", 20)
    for i in range(14):
        for m in (prod, "model-ok:1"):
            c.execute("INSERT INTO qa_log (user, question, answer, model, created_at, latency_s, rating) VALUES (?,?,?,?,?,?,?)",
                      ("u", "q", "a", m, "2999-01-01T00:00:00", 5.0, 1))
    c.commit()
    assert registry.evaluate_canary("chat")["decision"] == "promote"
    assert registry.production("chat") == "model-ok:1"
    assert registry.rollback("chat")["restored"] == prod and registry.production("chat") == prod


# ----------------------------------------------------------------------------- Case 7
def test_case7_contribution_answer_comes_from_ledger_with_evidence(env):
    from brain import agent, contrib, gitsync
    from brain.catalog import connect
    # Project X: Ly tạo 3 tài liệu, Thành tạo 1 và sửa 2 tài liệu của Ly, file do hệ thống tạo không được tính
    for i in range(3):
        write_docx(env / "RND" / "ProjectX" / f"Ly_{i}.docx", "Trần Thị Ly", [f"nội dung {i}"])
    write_docx(env / "RND" / "ProjectX" / "Thanh_0.docx", "Thành", ["thử nghiệm"])
    write_docx(env / "RND" / "ProjectX" / "Ly_0b.docx", "Trần Thị Ly", ["bản sửa"], editor="Thành")
    write_docx(env / "RND" / "ProjectX" / "Ly_1b.docx", "Trần Thị Ly", ["bản sửa"], editor="Thành")
    write_docx(env / "RND" / "ProjectX" / "Auto.docx", "python-docx", ["sinh tự động"])
    write_docx(env / "RND" / "Other" / "Khac.docx", "Phạm D", ["dự án khác"])
    scan_shared(env)
    c = connect()
    contrib.backfill(c)
    c.commit()
    rep = contrib.report(c, ["rnd"], False, project="ProjectX", since_days=None)
    names = [r["person"] for r in rep["ranking"]]
    assert names[0] in ("Trần Thị Ly", "Tran Thi Ly") and "Thành" in names[1] and len(names) == 2, rep["ranking"]
    assert rep["ranking"][0]["score"] > rep["ranking"][1]["score"]
    assert all(r["evidence"] for r in rep["ranking"]), "mỗi điểm phải có bằng chứng"
    assert not any("python" in n.lower() for n in names), "tài khoản phần mềm không phải con người"
    assert "Phạm D" not in names, "đúng phạm vi dự án"
    # điểm = tổng trọng số loại việc, KHÔNG phải số file
    ly = rep["ranking"][0]
    assert ly["score"] == pytest.approx(ly["kinds"]["Tạo tài liệu"] * contrib.WEIGHTS["created_doc"], abs=0.01)
    # AI chỉ diễn giải: công cụ trả về đúng dữ liệu sổ, và agent đưa nó vào bằng chứng
    res = agent.call_tool("contribution_report", {"project": "ProjectX", "since_days": 3650}, ["rnd"])
    ev = agent.Evidence()
    ev.from_result(res)
    assert any(e["kind"] == "contribution" for e in ev.items)
    # Git: commit của các tác giả được tính (nếu máy có git)
    if gitsync.git_available():
        repo = env / "RND" / "ProjectX"
        env_git = ["git", "-C", str(repo)]
        subprocess.run([*env_git, "init", "-q"], check=True)
        for who, n in (("Trần Thị Ly", 3), ("Thành", 1)):
            for k in range(n):
                (repo / f"code_{who[:2]}_{k}.py").write_text(f"print({k})\n" * 20, encoding="utf-8")
                subprocess.run([*env_git, "add", "-A"], check=True)
                subprocess.run([*env_git, "-c", f"user.name={who}", "-c", f"user.email={who[:2]}@x.vn", "commit", "-q",
                                "-m", f"add {k}"], check=True)
        scan_shared(env)  # phát hiện repo
        assert gitsync.sync_all()["new_commits"] == 4
        assert gitsync.sync_all()["new_commits"] == 0, "đồng bộ lặp lại không trùng"
        c = connect()
        gr = contrib.report(c, ["admin"], True, project="ProjectX", since_days=None)
        assert any("Git commit" in r["kinds"] for r in gr["ranking"])
        # commit Git không gắn file => người không phải admin không thấy
        nr = contrib.report(c, ["rnd"], False, project="ProjectX", since_days=None)
        assert not any("Git commit" in r["kinds"] for r in nr["ranking"])


# ----------------------------------------------------------------------------- Case 8
def test_case8_nobody_chats_for_days_system_still_learns_and_finds_gaps(env, monkeypatch):
    from brain import facts, gaps, learning, workflow, workflows_def  # noqa: F401
    from brain.catalog import connect

    def stub_llm_extract(name, text, model=None):
        return [dict(zip(("subject", "predicate", "value", "evidence"), (*ln.split("|"), ln))) for ln in text.splitlines()
                if ln.count("|") == 2 and ln.split("|")[1] in facts.PREDICATES]
    monkeypatch.setattr(facts, "llm_extract", stub_llm_extract)
    # dữ liệu: dự án P có 6 tài liệu cùng một người viết, không có spec; 2 file trùng; 1 mâu thuẫn; 1 tài liệu mồ côi
    for i in range(6):
        write_docx(env / "RND" / "P" / f"doc{i}.docx", "Chỉ Một Người", [f"nội dung khác nhau số {i} " * 30])
    same = write_docx(env / "RND" / "P" / "dup1.docx", "Chỉ Một Người", ["giống hệt " * 40])
    (env / "RND" / "P" / "dup2.docx").write_bytes(same.read_bytes())
    (env / "RND" / "Spec_A.md").write_text("M500|target_ph|8.5\n", encoding="utf-8")
    (env / "RND" / "Report_B.md").write_text("M500|target_ph|8.9\n", encoding="utf-8")
    write_docx(env / "Orphan.docx", "Ai Đó", ["tài liệu lạc lõng không thuộc dự án nào " * 40])
    scan_shared(env)
    from brain import index
    index.index_all()
    facts.extract_facts_pending()
    c = connect()
    # câu hỏi người dùng AI không trả lời được (từ trước) => lỗ hổng
    for _ in range(3):
        c.execute("INSERT INTO qa_log (user, question, answer, created_at) VALUES (?,?,?,datetime('now','localtime'))",
                  ("u", "Quy trình pha màu Pantone nội bộ?", "Mình không tìm thấy thông tin này."))
    c.commit()
    c.close()
    # chạy workflow bảo trì như scheduler (không có ai chat)
    wid = workflow.start("MAINTENANCE", {})
    res = workflow.run(wid)
    assert res["status"] == "done", res
    st = res["state"]
    assert st["graph"]["relations"] > 0 and st["gaps"]["new"] > 0
    c = connect()
    kinds = {r["kind"] for r in c.execute("SELECT kind FROM knowledge_gaps WHERE status='open'")}
    assert {"conflict", "duplicate_files", "single_person_dependency", "no_canonical_spec", "orphan_document",
            "unanswered_question"} <= kinds, kinds
    assert c.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0] >= 1, "tự tạo việc nghiên cứu"
    # chạy lại không sinh gap trùng
    n_before = c.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0]
    workflow.run(workflow.start("MAINTENANCE", {}))
    assert c.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0] == n_before
    # dataset huấn luyện ứng viên được dựng từ tri thức đã kiểm chứng — và cổng chất lượng chặn train khi chưa đủ
    m = learning.build_dataset()
    assert m["gate"]["pass"] is False and "chưa đủ" in m["gate"]["reason"]
    from brain import train
    t = train.train(m["version"])
    assert t["started"] is False and t["status"] == "blocked" and t["blockers"], "không giả vờ train"
