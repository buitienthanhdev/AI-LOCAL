"""Các lỗi chất lượng phát hiện khi chạy trên DỮ LIỆU THẬT (không thấy được bằng dữ liệu tổng hợp) — khóa lại bằng test."""
from conftest import scan_shared, write_docx


def test_code_files_and_bulk_imports_do_not_inflate_contributions(env):
    """Trước đây: 9.294 file code tải về + giải nén bị tính là 'tạo' => một người 31.533 điểm."""
    from brain import contrib
    from brain.catalog import connect
    # (a) file code do 'chủ sở hữu NTFS' => không tính (mã nguồn chỉ tính qua Git)
    (env / "RND" / "P").mkdir(parents=True)
    for i in range(5):
        (env / "RND" / "P" / f"mod{i}.py").write_text(f"x = {i}\n" * 50, encoding="utf-8")
    # (b) giải nén hàng loạt: 40 tài liệu cùng người cùng phút
    for i in range(40):
        write_docx(env / "RND" / "P" / "Zip" / f"d{i}.docx", "Người Tải", [f"tài liệu giải nén số {i}"])
    # (c) tài liệu thật của Ly, tác giả khai báo
    write_docx(env / "RND" / "P" / "Ly_real.docx", "Trần Thị Ly", ["báo cáo thật"])
    (env / "RND" / "P" / "anh_ai_sinh.png").write_bytes(b"\x89PNG" + bytes(range(256)) * 400)   # ảnh sinh ra: không phải đóng góp
    scan_shared(env)
    c = connect()
    contrib.backfill(c)
    c.commit()
    rep = contrib.report(c, ["admin"], True, project="P", since_days=None)
    assert rep["bulk_events_ignored"] >= 40, "nhập hàng loạt bị bỏ qua và được báo rõ"
    names = {r["person"] for r in rep["ranking"]}
    assert "Người Tải" not in names and any("Ly" in n for n in names)
    assert not any("mã nguồn" in k.lower() for r in rep["ranking"] for k in r["kinds"]), "code không tính qua file"
    assert c.execute("SELECT COUNT(*) FROM contributions WHERE kind LIKE '%code%'").fetchone()[0] == 0
    assert c.execute("""SELECT COUNT(*) FROM contributions c JOIN files f ON f.file_id=c.file_id
                        WHERE f.ext='.png'""").fetchone()[0] == 0, "ảnh không phải đóng góp"


def test_owner_only_authorship_counts_half(env):
    from brain import contrib
    from brain.catalog import connect
    (env / "RND" / "P").mkdir(parents=True)
    (env / "RND" / "P" / "no_author.md").write_text("tài liệu không khai tác giả\n" * 5, encoding="utf-8")
    scan_shared(env)
    c = connect()
    contrib.backfill(c)
    w = c.execute("SELECT weight FROM contributions WHERE kind='created_doc'").fetchone()
    if w:  # (chủ sở hữu NTFS có thể không đọc được trên một số máy => không có dòng nào)
        assert w[0] == contrib.WEIGHTS["created_doc"] * contrib.OWNER_ONLY_FACTOR


def test_index_dependent_gaps_wait_until_index_is_ready(env):
    """Trước đây: kho chỉ mục trống => hàng loạt 'chủ đề chưa có tài liệu' giả."""
    from brain import gaps, index
    from brain.catalog import connect
    write_docx(env / "RND" / "a.docx", "A", ["nội dung " * 300])
    scan_shared(env)
    c = connect()
    for _ in range(3):
        c.execute("INSERT INTO qa_log (user, question, answer, created_at) VALUES (?,?,?,datetime('now','localtime'))",
                  ("u", "quy trình pantone desktop", "ok 1"))
    c.commit()
    kinds = {g["kind"] for g in gaps.detect(c)}
    assert "undocumented_topic" not in kinds and "orphan_document" not in kinds, "chưa index => không kết luận thiếu tài liệu"
    index.index_all()
    assert "orphan_document" in {g["kind"] for g in gaps.detect(c)}, "đã index => dò được"
