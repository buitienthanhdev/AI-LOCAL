"""Điều khiển chuột/bàn phím: quyền có thời hạn, dừng khẩn cấp, không gõ mật khẩu. Mọi thao tác thật đều bị mock."""
import pytest

from brain import gui
from brain.actions import execute


@pytest.fixture
def g(env, monkeypatch):
    log = []
    monkeypatch.setattr(gui, "click_at", lambda x, y, button="left", double=False: log.append(("click", x, y, button, double)))
    monkeypatch.setattr(gui, "type_text", lambda t: log.append(("type", t)))
    monkeypatch.setattr(gui, "press", lambda k, times=1: log.append(("key", k, times)))
    monkeypatch.setattr(gui, "scroll", lambda n: log.append(("scroll", n)))
    monkeypatch.setattr(gui, "_cursor", lambda: (500, 500))
    monkeypatch.setattr(gui, "_virtual_origin", lambda: (0, 0))
    monkeypatch.setattr(gui, "_focused_is_password", lambda: False)
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    gui.revoke()
    gui._grant.pop("pending", None)
    gui._view.update(left=100, top=50, scale=0.5, elements=[
        {"type": "nút", "name": "Lưu", "rect": (110, 60, 150, 80), "enabled": True, "password": False},
        {"type": "ô nhập", "name": "Mật khẩu", "rect": (200, 100, 400, 120), "enabled": True, "password": True},
        {"type": "ô nhập", "name": "Tìm kiếm", "rect": (200, 140, 400, 160), "enabled": True, "password": False}])
    yield log
    gui.revoke()


def test_first_action_asks_for_time_limited_permission(g):
    r = gui.call("gui_click", {"element": 1, "purpose": "lưu file"}, "admin", True)
    assert "pending_action" in r and not g, "chưa được cấp quyền => không bấm gì"
    again = gui.call("gui_key", {"keys": "ctrl+s"}, "admin", True)
    assert "error" in again and "pending_action" not in again, "không xin quyền trùng khi đang chờ"
    out = execute(r["pending_action"]["id"], "admin", True)
    assert out["ok"] and "Đã làm" in out["message"]
    assert g == [("click", 130, 70, "left", False)], "cấp quyền xong => làm luôn thao tác đang chờ (bấm GIỮA element 1)"
    assert gui.status()["active"] and gui.status()["seconds_left"] > 500
    gui.call("gui_key", {"keys": "ctrl+s"}, "admin", True)
    assert g[-1] == ("key", "ctrl+s", 1) and gui.status()["actions"] == 2


class _FakeClipboard:
    """Clipboard giả trong bộ nhớ: test không đụng clipboard THẬT của người dùng (không ghi đè thứ họ đang copy,
    không chập chờn khi app khác đang giữ clipboard — từng gây 'OpenClipboard: Access is denied')."""
    CF_UNICODETEXT = 13

    def __init__(self, text=None):
        self.text, self.opened = text, False

    def OpenClipboard(self):
        self.opened = True

    def CloseClipboard(self):
        self.opened = False

    def IsClipboardFormatAvailable(self, fmt):
        return self.text is not None

    def GetClipboardData(self, fmt):
        return self.text

    def EmptyClipboard(self):
        self.text = None

    def SetClipboardText(self, text, fmt=None):
        self.text = text


def test_typing_pastes_via_clipboard_and_restores_it(env, monkeypatch):
    import sys
    cb = _FakeClipboard("clipboard của người dùng")
    monkeypatch.setitem(sys.modules, "win32clipboard", cb)
    pasted = []
    monkeypatch.setattr(gui, "press", lambda keys, times=1: pasted.append((keys, cb.text)))
    monkeypatch.setattr(gui.time, "sleep", lambda s: None)
    gui.type_text("Xin chào, mình là Company Brain — điều khiển")
    assert pasted == [("ctrl+v", "Xin chào, mình là Company Brain — điều khiển")]
    assert cb.text == "clipboard của người dùng", "trả lại clipboard cũ"


def test_typing_falls_back_to_keys_when_clipboard_busy(env, monkeypatch):
    import sys
    cb = _FakeClipboard()

    def busy():
        raise OSError("OpenClipboard: Access is denied")  # app khác đang giữ clipboard
    cb.OpenClipboard = busy
    monkeypatch.setitem(sys.modules, "win32clipboard", cb)
    typed = []
    monkeypatch.setattr(gui, "_type_keys", lambda t: typed.append(t))
    gui.type_text("abc")
    assert typed == ["abc"]


def test_click_by_text_and_by_image_coordinates(g):
    gui.grant(10, "test", "admin")
    gui.call("gui_click", {"text": "luu"}, "admin", True)  # không dấu vẫn khớp "Lưu"
    assert g[-1][1:3] == (130, 70)
    gui.call("gui_click", {"x": 100, "y": 40, "double": True}, "admin", True)  # tọa độ trên ảnh thu nhỏ 50%
    assert g[-1] == ("click", 300, 130, "left", True)
    assert "error" in gui.call("gui_click", {"element": 9}, "admin", True)


def test_never_types_into_password_fields(g, monkeypatch):
    gui.grant(10, "test", "admin")
    r = gui.call("gui_type", {"element": 2, "text": "bimat123"}, "admin", True)
    assert "error" in r and not any(x[0] == "type" for x in g)
    monkeypatch.setattr(gui, "_focused_is_password", lambda: True)
    assert "error" in gui.call("gui_type", {"text": "bimat123"}, "admin", True)
    monkeypatch.setattr(gui, "_focused_is_password", lambda: False)
    gui.call("gui_type", {"element": 3, "text": "Âm thầm bên em", "enter": True}, "admin", True)
    assert ("type", "Âm thầm bên em") in g and ("key", "enter", 1) in g


def test_emergency_stop_mouse_in_top_left_corner(g, monkeypatch):
    gui.grant(10, "test", "admin")
    monkeypatch.setattr(gui, "_cursor", lambda: (0, 1))
    r = gui.call("gui_key", {"keys": "enter"}, "admin", True)
    assert "DỪNG KHẨN CẤP" in r["error"] and not gui.status()["active"] and not g


def test_grant_expires_and_can_be_revoked(g, monkeypatch):
    gui.grant(10, "test", "admin")
    gui.revoke()
    assert "pending_action" in gui.call("gui_scroll", {"direction": "down"}, "admin", True)
    gui._grant.pop("pending", None)
    gui.grant(10, "test", "admin")
    gui._grant["until"] = 0  # hết hạn
    assert not gui.status()["active"]


def test_admin_only_and_key_parsing(g):
    assert "error" in gui.call("gui_look", {}, "u", False)
    assert "error" in gui.call("gui_click", {"element": 1}, "u", False)
    assert gui._vk("khongco") is None
    assert gui._vk("ctrl") == 0x11 and gui._vk("s") == ord("S") and gui._vk("f5") == 0x74


def test_optional_tool_groups_only_when_needed(env):
    from brain.agent import _optional_tools
    names = lambda q: {t["function"]["name"] for t in _optional_tools(q)}  # noqa: E731
    gui.revoke()
    assert names("ai đóng góp nhiều nhất cho M500") == set(), "câu hỏi tri thức: không kèm nhóm ít dùng"
    assert "gui_click" in names("bấm nút Lưu") and "schedule_task" in names("nhắc tôi 5h chiều")
    assert "save_skill" in names("lưu cách làm vừa rồi thành kỹ năng")
    gui.grant(10, "x", "admin")
    assert "gui_look" in names("tiếp đi"), "đang có quyền điều khiển => luôn kèm công cụ chuột/phím"
    gui.revoke()


def test_gui_intent():
    from brain.agent import _intent
    from brain.facts import norm
    tools = lambda q: (_intent(norm(q)) or (None, ()))[1]  # noqa: E731
    assert "gui_click" in tools("bấm nút Lưu trong Word") and "gui_type" in tools("điền vào ô tìm kiếm chữ xin chào")
