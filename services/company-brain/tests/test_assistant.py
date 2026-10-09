"""Trí nhớ hội thoại dài, hẹn giờ/việc nền/thông báo, kỹ năng do người dùng dạy. Không gọi model thật, không bật thông báo."""
import time
from datetime import datetime, timedelta

import pytest

from brain import convo, skills, tasks

ADMIN = ["admin", "all"]


@pytest.fixture
def quiet(env, monkeypatch):
    shown = []
    monkeypatch.setattr(tasks, "toast", lambda t, b: shown.append((t, b)))
    return shown


# ---------------------------------------------------------------- thời gian
def test_parse_when_vietnamese_formats():
    now = datetime(2026, 10, 7, 14, 0)
    assert tasks.parse_when("17:00", now) == datetime(2026, 10, 7, 17, 0)
    assert tasks.parse_when("17h", now) == datetime(2026, 10, 7, 17, 0)
    assert tasks.parse_when("9h30", now) == datetime(2026, 10, 8, 9, 30), "qua giờ hôm nay => ngày mai"
    assert tasks.parse_when("+30m", now) == now + timedelta(minutes=30)
    assert tasks.parse_when("+2h", now) == now + timedelta(hours=2)
    assert tasks.parse_when("2026-12-24 20:00", now) == datetime(2026, 12, 24, 20, 0)
    assert tasks.parse_when("lúc nào đó", now) is None


def test_next_run_for_repeats():
    t0 = datetime(2026, 10, 9, 8, 0)  # thứ sáu
    assert tasks.next_after({"repeat": "daily", "at_time": "08:00"}, t0) == datetime(2026, 10, 10, 8, 0)
    assert tasks.next_after({"repeat": "weekdays", "at_time": "08:00"}, t0) == datetime(2026, 10, 12, 8, 0), "bỏ T7, CN"
    assert tasks.next_after({"repeat": "weekly", "at_time": "17:00", "weekdays": "4"}, t0) == datetime(2026, 10, 9, 17, 0)
    assert tasks.next_after({"repeat": "minutes", "interval_min": 15}, t0) == t0 + timedelta(minutes=15)
    assert tasks.next_after({"repeat": "once"}, t0) is None


# ---------------------------------------------------------------- hẹn giờ
def test_reminder_fires_once_and_notifies(quiet):
    r = tasks.create("admin", ADMIN, "reminder", "gọi khách hàng", "+1m")
    assert r["task_id"] and not tasks.tick()
    assert tasks.tick(datetime.now() + timedelta(minutes=2)) == 1
    for _ in range(50):
        if tasks.unread("admin"):
            break
        time.sleep(0.05)
    n = tasks.notifications("admin")
    assert n[0]["body"] == "gọi khách hàng" and quiet, "có thông báo trong tab Việc + thông báo Windows"
    t = tasks.list_tasks("admin")[0]
    assert t["active"] == 0 and t["next_run"] is None, "việc một lần không chạy lại"
    assert tasks.tick(datetime.now() + timedelta(days=1)) == 0
    tasks.mark_read("admin")
    assert tasks.unread("admin") == 0


def test_repeating_task_is_rescheduled_not_duplicated(quiet):
    tasks.create("admin", ADMIN, "reminder", "uống nước", "+1m", repeat="minutes", interval_min=30)
    later = datetime.now() + timedelta(minutes=2)
    assert tasks.tick(later) == 1 and tasks.tick(later) == 0, "đã dời lịch trước khi chạy => không chạy trùng"
    t = tasks.list_tasks("admin")[0]
    assert t["active"] == 1 and datetime.fromisoformat(t["next_run"]) > later


def test_prompt_task_runs_agent_and_keeps_buttons(quiet, monkeypatch):
    from brain import actions, agent
    pa = actions.add_pending("run_command", "Chạy lệnh: x", {"command": "x"}, "admin")["pending_action"]
    monkeypatch.setattr(agent, "ask", lambda q, g, user="": {"answer": f"đã làm: {q}", "actions": [pa], "sources": []})
    t = tasks.create("admin", ADMIN, "prompt", "tóm tắt file mới", "+1m")
    tasks.run_task(tasks.list_tasks("admin")[0] | {"task_id": t["task_id"]})
    n = tasks.notifications("admin")[0]
    assert n["data"]["answer"] == "đã làm: tóm tắt file mới" and n["data"]["actions"][0]["id"] == pa["id"]
    assert actions._pending[pa["id"]]["ttl"] == 24 * 3600


def test_scheduled_command_needs_one_confirmation(quiet):
    from brain.actions import execute
    r = tasks.schedule_tool({"kind": "command", "payload": "Get-Date", "when": "+1h", "repeat": "daily"}, "admin", ADMIN, True)
    assert "pending_action" in r and not tasks.list_tasks("admin")
    assert execute(r["pending_action"]["id"], "admin", True)["ok"]
    assert tasks.list_tasks("admin")[0]["kind"] == "command"
    assert "error" in tasks.schedule_tool({"kind": "reminder", "payload": "x", "when": "+1h"}, "u", ["all"], False)


def test_manage_tasks_tool(quiet):
    tid = tasks.create("admin", ADMIN, "reminder", "họp", "+1h")["task_id"]
    assert "họp" in tasks.manage_tool({"action": "list"}, "admin", True)["tasks"][0]
    assert tasks.manage_tool({"action": "pause", "task_id": tid}, "admin", True)["ok"]
    assert tasks.list_tasks("admin")[0]["active"] == 0
    assert tasks.manage_tool({"action": "cancel", "task_id": tid}, "admin", True)["ok"] and not tasks.list_tasks("admin")
    assert not tasks.set_active(tid, "nguoi_khac", 1)


def test_background_command_notifies_when_done(quiet):
    r = tasks.run_background("Write-Output xong-roi", None, 60, "admin")
    assert r["background"]
    for _ in range(200):
        if tasks.unread("admin"):
            break
        time.sleep(0.1)
    n = tasks.notifications("admin")[0]
    assert "xong-roi" in n["data"]["output"] and n["data"]["exit_code"] == 0


# ---------------------------------------------------------------- kỹ năng
def test_save_last_turn_as_parameterised_skill_and_replay(env):
    skills.record_turn("admin", [{"tool": "open_url", "args": {"query": "âm thầm bên em", "site": "youtube",
                                                               "browser": "edge"}},
                                 {"tool": "manage_skills", "args": {"action": "list"}}])
    r = skills.save({"name": "Mở nhạc trên Edge", "from_last_turn": True, "parameters": {"bai_hat": "âm thầm bên em"},
                     "triggers": ["bật bài"]}, "admin", True)
    assert r["saved"] and r["parameters"] == ["bai_hat"] and len(r["steps"]) == 1, "công cụ quản lý không bị ghi vào"
    called = []
    out = skills.run({"name": "mo nhac tren edge", "params": {"bai_hat": "nhạc thiền"}}, "admin", ADMIN, True,
                     lambda n, a, g, u: called.append((n, a)) or {"opened_url": "x"})
    assert called == [("open_url", {"query": "nhạc thiền", "site": "youtube", "browser": "edge"})] and out["ran"]
    assert skills.matching("mở nhạc trên edge bài lạc trôi")[0]["name"] == "Mở nhạc trên Edge"
    assert skills.list_all()[0]["uses"] == 1


def test_skill_depending_on_runtime_data_is_model_guided(env):
    skills.record_turn("admin", [{"tool": "open_file_with", "args": {"file_id": 42, "app": "media player"}}])
    skills.save({"name": "nghe file", "from_last_turn": True}, "admin", True)
    out = skills.run({"name": "nghe file"}, "admin", ADMIN, True, lambda *a: pytest.fail("không được chạy file_id cũ"))
    assert "example_steps" in out and "instructions" in out


def test_skill_taught_by_instructions(env):
    r = skills.save({"name": "dọn Downloads", "instructions": "chuyển file .exe trong Downloads sang D:\\Setup"}, "admin", True)
    assert r["saved"]
    out = skills.run({"name": "dọn downloads"}, "admin", ADMIN, True, lambda *a: pytest.fail("phải để model làm"))
    assert "D:\\Setup" in out["instructions"]
    assert "error" in skills.save({"name": "x"}, "admin", True), "không có bước lẫn hướng dẫn"
    assert "error" in skills.save({"name": "y", "instructions": "z"}, "u", False), "chỉ admin"
    assert skills.manage({"action": "delete", "name": "dọn Downloads"}, True)["deleted"]


# ---------------------------------------------------------------- trí nhớ hội thoại
def test_long_conversation_is_summarised_in_cached_blocks(env, monkeypatch):
    from brain import llm
    calls = []
    monkeypatch.setattr(llm, "chat", lambda msgs, **k: calls.append(1) or {"message": {"content": f"- tóm tắt {len(calls)}"}})
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"tin {i}"} for i in range(30)]
    summary, recent = convo.compact(hist)
    assert summary and len(calls) == 2 and recent == hist[20:], "2 khối x 10 tin đã tóm tắt, phần còn lại giữ nguyên văn"
    convo.compact(hist + [{"role": "user", "content": "tin 30"}, {"role": "assistant", "content": "tin 31"}])
    assert len(calls) == 2, "khối cũ đã có trong cache => không gọi model lại"
    assert convo.compact(hist[:8]) == (None, hist[:8])


def test_intents_for_scheduling_and_skills():
    from brain.agent import _intent
    from brain.facts import norm
    tools = lambda q: (_intent(norm(q)) or (None, ()))[1]  # noqa: E731
    assert "schedule_task" in tools("nhắc tôi 5h chiều gọi khách") and "schedule_task" in tools("mỗi sáng 8h tóm tắt email")
    assert "schedule_task" in tools("sau 30 phút nhắc uống nước")
    assert "save_skill" in tools("lưu cách làm vừa rồi thành kỹ năng mở nhạc")
    assert "open_app" in tools("mở word")
