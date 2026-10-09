"""Kỹ năng thao tác máy tính: đọc/tìm/ghi/sửa file, chạy lệnh, an toàn (chỉ-đọc chạy ngay, thay đổi phải xác nhận)."""
from pathlib import Path

import pytest

from brain import computer
from brain.actions import execute


@pytest.mark.parametrize("cmd", ["Get-Process | Sort-Object CPU -Descending | Select-Object -First 5", "ipconfig",
                                 "git status", "nvidia-smi", "Get-ChildItem C:\\ | Format-Table", "ollama list"])
def test_read_only_commands_run_immediately(cmd):
    assert computer.is_read_only(cmd)


@pytest.mark.parametrize("cmd", ["Remove-Item C:\\x", "Stop-Process -Name chrome", "Get-Date > C:\\x.txt",
                                 "Get-Date; Remove-Item x", "git push", "pip install foo", "iex (iwr http://x)",
                                 "Get-ChildItem | Where-Object { [IO.File]::Delete('x') }", "shutdown /s",
                                 "python script.py", "Get-Process | % { $_.Kill() }", "reg add HKCU\\x"])
def test_changing_commands_need_confirmation(cmd):
    assert not computer.is_read_only(cmd)


def test_everything_is_admin_only(env, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    for name, args in (("read_file", {"path": str(f)}), ("find_files", {"pattern": "a"}), ("run_command", {"command": "ipconfig"}),
                       ("write_file", {"path": str(f), "content": "y"}), ("web_search", {"query": "x"}),
                       ("screenshot", {}), ("media_control", {"action": "mute"}), ("system_status", {})):
        assert "error" in computer.call(name, args, "u", False), name


def test_read_find_and_grep(env, tmp_path):
    d = tmp_path / "Báo cáo"
    d.mkdir()
    f = d / "ghi chú.py"
    f.write_text("dong 1\nPASSWORD_HASH = 'abc'\ndong 3\n", encoding="utf-8")
    r = computer.read_file({"path": str(f), "start_line": 2, "max_lines": 1}, True)
    assert r["total_lines"] == 3 and r["content"] == "2\tPASSWORD_HASH = 'abc'" and r["truncated"]
    m = computer.find_files({"pattern": "ghi chu", "root": str(tmp_path)}, True)  # không dấu vẫn khớp
    assert [x["path"] for x in m["matches"]] == [str(f)]
    g = computer.search_in_files({"regex": "password_hash", "root": str(tmp_path), "file_glob": "*.py"}, True)
    assert g["lines"][0]["line"] == 2
    assert "error" in computer.read_file({"path": str(tmp_path / "khong_co.txt")}, True)


def test_write_and_edit_need_confirmation_and_back_up(env, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("xin chao\nthe gioi\n", encoding="utf-8")
    r = computer.propose_edit({"path": str(f), "old_text": "the gioi", "new_text": "Viet Nam"}, "admin", True)
    pa = r["pending_action"]
    assert "-the gioi" in pa["detail"] and "+Viet Nam" in pa["detail"]
    assert f.read_text(encoding="utf-8") == "xin chao\nthe gioi\n", "chưa bấm Thực hiện thì chưa được sửa"
    assert execute(pa["id"], "admin", True)["ok"]
    assert f.read_text(encoding="utf-8") == "xin chao\nViet Nam\n"
    backups = list((Path(computer.DATA_DIR) / "backups").glob("*_note.txt"))
    assert backups and backups[-1].read_text(encoding="utf-8") == "xin chao\nthe gioi\n"
    # sửa ngoài luồng sau khi đề xuất => không ghi đè mù
    pa = computer.propose_write({"path": str(f), "content": "moi"}, "admin", True)["pending_action"]
    f.write_text("nguoi dung vua sua", encoding="utf-8")
    assert not execute(pa["id"], "admin", True)["ok"] and f.read_text(encoding="utf-8") == "nguoi dung vua sua"
    assert "error" in computer.propose_edit({"path": str(f), "old_text": "khong co", "new_text": "x"}, "admin", True)
    assert "error" in computer.propose_write({"path": r"C:\Windows\x.txt", "content": "x"}, "admin", True)


def test_run_command_read_only_vs_confirmed(env, tmp_path):
    r = computer.run_command({"command": "Get-ChildItem -Name", "cwd": str(tmp_path)}, "admin", True)
    assert r["exit_code"] == 0 and "pending_action" not in r
    target = tmp_path / "made.txt"
    r = computer.run_command({"command": f"New-Item -ItemType File '{target}'"}, "admin", True)
    assert "pending_action" in r and not target.exists()
    out = execute(r["pending_action"]["id"], "admin", True)
    assert out["ok"] and "output" in out and target.exists()


def test_intent_routing_for_machine_and_web_questions():
    from brain.agent import _intent
    from brain.facts import norm
    tools = lambda q: (_intent(norm(q)) or (None, ()))[1]  # noqa: E731
    assert "run_command" in tools("IP máy là gì") and "system_status" in tools("máy còn bao nhiêu RAM")
    assert "web_search" in tools("giá card RTX 5090 ở Việt Nam hiện nay") and "web_search" in tools("thời tiết Hà Nội")
    assert "open_app" in tools("mở word") and "run_command" in tools("đóng notepad")
    assert tools("ai đóng góp nhiều nhất cho dự án M500") == ("contribution_report",) and tools("tóm tắt file README") == ()
    assert tools("file M500_Formula đã đổi gì qua các phiên bản") == ("file_history",)
    assert "graph_query" in tools("Acronal PS 673 liên quan tới ai")
    user = lambda q: (_intent(norm(q), admin=False) or (None, ()))[1]  # noqa: E731
    assert user("IP máy là gì") == () and user("mở word") == (), "người dùng thường không có công cụ máy"
    assert user("ai đóng góp nhiều nhất") == ("contribution_report",)


def test_markdown_is_read_as_text(env, tmp_path):
    f = tmp_path / "README.md"
    f.write_text("# Tiêu đề\nnội dung\n", encoding="utf-8")
    assert "nội dung" in computer.read_file({"path": str(f)}, True)["content"]


def test_specific_intents_win_over_generic_ones():
    from brain.agent import _intent
    from brain.facts import norm
    first = lambda q: (_intent(norm(q)) or (None, (None,)))[1][0]  # noqa: E731
    assert first("dạy kỹ năng dọn desktop: chuyển các file .exe sang D:\Setup") == "save_skill"
    assert first("nhắc tôi mở word lúc 5h") == "schedule_task"
    assert first("tôi đang hẹn những việc gì") == "schedule_task"
    assert first("gom ảnh tháng 9 vào thư mục mới") == "batch_files"
    assert first("mở word") == "open_app"
