"""Thao tác file hàng loạt theo điều kiện: lọc đúng, một lần xác nhận, chạy cả loạt, hoàn tác được."""
import os
from datetime import datetime
from pathlib import Path

from brain import batch
from brain.actions import execute


def _mk(folder: Path, name: str, when: datetime) -> Path:
    p = folder / name
    p.write_bytes(b"x" * 10)
    ts = when.timestamp()
    os.utime(p, (ts, ts))
    return p


def test_gather_images_of_sep_oct_into_new_folder_then_undo(env, tmp_path):
    src = tmp_path / "Downloads"
    src.mkdir()
    keep = [_mk(src, "a.png", datetime(2026, 9, 3)), _mk(src, "b.JPG", datetime(2026, 10, 31, 23)),
            _mk(src, "c.webp", datetime(2026, 10, 1))]
    other = [_mk(src, "old.png", datetime(2026, 8, 31)), _mk(src, "doc.pdf", datetime(2026, 9, 15)),
             _mk(src, "nov.jpg", datetime(2026, 11, 1))]
    args = {"source": str(src), "types": ["ảnh"], "date_from": "2026-09-01", "date_to": "2026-10-31",
            "date_field": "modified", "destination": "Ảnh tháng 9-10"}
    r = batch.propose(args, "admin", True)
    assert r["found"] == 3 and "pending_action" in r and "a.png" in r["pending_action"]["detail"]
    assert all(p.exists() for p in keep), "chưa bấm Thực hiện thì chưa chuyển"
    out = execute(r["pending_action"]["id"], "admin", True)
    dst = src / "Ảnh tháng 9-10"
    assert out["ok"] and "3 file" in out["message"]
    assert sorted(x.name for x in dst.iterdir()) == ["a.png", "b.JPG", "c.webp"]
    assert all(p.exists() for p in other), "file ngoài điều kiện không bị đụng"
    u = batch.propose_undo({}, "admin", True)
    assert execute(u["pending_action"]["id"], "admin", True)["ok"]
    assert all(p.exists() for p in keep) and not dst.exists(), "hoàn tác: file về chỗ cũ, thư mục trống bị dọn"
    assert "error" in batch.propose_undo({}, "admin", True), "không hoàn tác hai lần"


def test_name_conflicts_copy_and_guards(env, tmp_path):
    src = tmp_path / "src"
    (src / "out").mkdir(parents=True)
    _mk(src, "x.txt", datetime(2026, 9, 9))
    (src / "out" / "x.txt").write_text("đã có", encoding="utf-8")
    r = batch.propose({"source": str(src), "name_pattern": "x", "action": "copy", "destination": "out"}, "admin", True)
    execute(r["pending_action"]["id"], "admin", True)
    assert (src / "out" / "x (1).txt").exists() and (src / "x.txt").exists(), "copy: giữ bản gốc, không ghi đè trùng tên"
    assert batch.propose({"source": str(src), "types": ["video"], "destination": "v"}, "admin", True)["found"] == 0
    assert "error" in batch.propose({"source": str(src), "destination": r"C:\Windows\x"}, "admin", True)
    assert "error" in batch.propose({"source": str(src), "destination": "y"}, "u", False)
    assert "error" in batch.propose({"source": str(src), "date_from": "hôm qua", "destination": "y"}, "admin", True)


def test_destination_starting_with_source_name_is_not_nested(env, tmp_path):
    src = tmp_path / "Downloads"
    src.mkdir()
    _mk(src, "a.png", datetime(2026, 9, 3))
    r = batch.select({"source": str(src), "destination": "Downloads\\Anh_9_10"})
    assert r[1] == src / "Anh_9_10", "không tạo Downloads\\Downloads\\..."


def test_month_only_dates_and_known_folders():
    assert batch._day("2026-10", end=True) == datetime(2026, 10, 31, 23, 59, 59, 999999)
    assert batch._day("9/2026") == datetime(2026, 9, 1)
    assert batch._folder("downloads") == Path.home() / "Downloads" and batch._folder("tải về") == Path.home() / "Downloads"


def test_bulk_intent():
    from brain.agent import _intent
    from brain.facts import norm
    q = "tạo thư mục ở phần dowload rồi bỏ tất cả các ảnh đã tải về trong tháng 9 và tháng 10 2026 vô"
    assert _intent(norm(q))[1][0] == "batch_files"
