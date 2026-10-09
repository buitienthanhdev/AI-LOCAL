"""Parity Rust (brain_core) == Python. Bỏ qua nếu chưa build brain_core (máy chưa cài Rust)."""
import hashlib

import pytest

bc = pytest.importorskip("brain_core")

CASES = ["Báo Cáo Tuần.docx", "  Đường  ống —  pH 8,5%  ", "Ａｂｃ１２３", "İstanbul", "ΣΊΣΥΦΟΣ", "ß straße", "ﬁle", "①②",
         "٣٤", "Ǆ", "ẞ", "x\u0301\u0323", "Ð", "đĐ", "ⅷ", "㎏", "\u00a0nbsp", "tab\tnew\nline", "", "   ", "🎉 emoji",
         "K (kelvin)", "M500_Formula_v14.xlsx", "C:/Users/TIEN THANH/Downloads/Âm Thầm Bên Em.mp3", "Kế hoạch Q4 — 15/11"]


def test_norm_and_plain_identical_to_python():
    from brain.facts import _py_norm
    from brain.vn import _py_plain
    for s in CASES:
        assert bc.norm(s) == _py_norm(s), s
        assert bc.plain(s) == _py_plain(s), s
    assert bc.norm_many(CASES) == [_py_norm(s) for s in CASES]


def test_public_norm_uses_rust_and_accepts_non_str():
    from brain.facts import norm
    assert norm(8.5) == "8.5" and norm(None) == "none" and norm("Đà Lạt") == "da lat"


def test_telex_syllables_identical():
    from brain.vn import _syllable_variants
    for w in ("ửod", "ẽcel", "ưỏd", "bảng", "tính", "đường", "nghiêng", "abc"):
        assert set(bc.syllable_variants(w)) == _syllable_variants(w), w
    assert "word" in bc.telex_variants("ửod")


def test_hash_identical(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * 3_000_001)
    want = hashlib.sha256(p.read_bytes()).hexdigest()
    assert bc.hash_file(str(p)) == want
    assert bc.hash_files([str(p), str(tmp_path / "missing")], 2) == [want, None]
    with pytest.raises(OSError):
        bc.hash_file(str(tmp_path / "missing"))


def _build_tree(shared):
    import os
    import time
    from conftest import write_docx
    write_docx(shared / "HR" / "luong.docx", "Ly", ["Bảng lương tháng 9"])
    write_docx(shared / "RND" / "M500" / "spec.docx", "Hùng", ["pH 8.5"])
    (shared / "RND" / "M500" / "note.md").write_text("# ghi chú\nnội dung", encoding="utf-8")
    (shared / "RND" / "code.py").write_text("print(1)\n", encoding="utf-8")
    (shared / "RND" / "~$spec.docx").write_text("lock", encoding="utf-8")
    (shared / "RND" / "app.exe").write_bytes(b"MZ")
    (shared / "repo" / ".git").mkdir(parents=True, exist_ok=True)
    (shared / "repo" / ".git" / "x.md").write_text("bỏ qua", encoding="utf-8")
    (shared / "repo" / "README.md").write_text("repo", encoding="utf-8")
    (shared / "$Recycle.Bin").mkdir(exist_ok=True)
    (shared / "$Recycle.Bin" / "old.md").write_text("rác", encoding="utf-8")
    old = time.time() - 3600
    for p in shared.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))


def _snapshot():
    from brain.catalog import connect
    c = connect()
    cols = ("path, name, ext, size, sha256, fs_modified_at, doc_author, doc_last_editor, title, department, project, acl, "
            "tier, status, extract_error, version_no, name_key")
    files = sorted(tuple(r) for r in c.execute(f"SELECT {cols} FROM files"))
    events = sorted((r[0], r[1], r[2]) for r in c.execute(
        "SELECT f.path, e.event, e.actor FROM file_events e JOIN files f USING(file_id)"))
    versions = sorted(tuple(r) for r in c.execute(
        "SELECT f.path, v.version_no, v.sha256, v.size, v.editor FROM file_versions v JOIN files f USING(file_id)"))
    contribs = sorted(tuple(r) for r in c.execute(
        "SELECT f.path, c.kind, c.weight FROM contributions c JOIN files f USING(file_id)"))
    repos = sorted(r[0] for r in c.execute("SELECT path FROM repos"))
    c.close()
    return files, events, versions, contribs, repos


def _wipe():
    from brain.catalog import connect
    c = connect()
    for t in ("files", "file_events", "file_versions", "contributions", "repos"):
        c.execute(f"DELETE FROM {t}")
    c.commit()
    c.close()


def _run(shared, rust: bool, monkeypatch):
    import os
    from brain import config, scanner
    monkeypatch.setattr(config, "USE_RUST_SCANNER", rust)
    s1 = scanner.scan(shared)  # lần 1: mọi file mới
    note = shared / "RND" / "M500" / "note.md"
    note.write_text("# ghi chú\nđã sửa", encoding="utf-8")  # sửa nội dung
    fixed = 1_790_000_000  # mtime CỐ ĐỊNH: hai lượt (Rust, Python) chạy ở hai thời điểm khác nhau phải cho cùng dữ liệu
    os.utime(note, (fixed, fixed))
    p = shared / "RND" / "code.py"
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))  # chỉ đổi mtime (sha giữ nguyên)
    (shared / "HR" / "luong.docx").unlink()  # xóa
    s2 = scanner.scan(shared)
    s3 = scanner.scan(shared)  # không đổi gì
    return (s1, s2, s3), _snapshot()


def test_rust_scanner_identical_to_python(env, monkeypatch):
    import shutil
    _build_tree(env)
    backup = env.parent / (env.name + "_orig")
    shutil.copytree(env, backup)
    stats_rs, snap_rs = _run(env, True, monkeypatch)
    _wipe()
    shutil.rmtree(env)
    shutil.copytree(backup, env)
    stats_py, snap_py = _run(env, False, monkeypatch)
    assert stats_rs == stats_py
    for name, a, b in zip(("files", "events", "versions", "contribs", "repos"), snap_rs, snap_py):
        assert a == b, name
    files = {r[0]: r for r in snap_rs[0]}
    assert any(r[11] == "hr,board" for r in files.values()), "ACL theo phòng ban HR phải giữ"
    assert stats_rs[2]["unchanged"] >= 3 and stats_rs[2]["new"] == stats_rs[2]["modified"] == 0
