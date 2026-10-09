"""Thực thi theo ý định: mở web bằng trình duyệt chỉ định, mở file bằng app chỉ định, quy tắc loại-file -> app.
Mọi thao tác mở thật (Popen/startfile/webbrowser/PowerShell) đều bị mock — test không bật app nào trên máy."""
import time

import pytest

from brain import apps, launcher

FAKE_APPS = [
    {"name": "Media Player", "id": "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", "exe": False},
    {"name": "Google Chrome", "id": "Chrome", "exe": False, "target": r"C:\x\chrome.exe"},
    {"name": "Microsoft Edge", "id": "MSEdge", "exe": False, "target": r"C:\x\msedge.exe"},
    {"name": "Photos", "id": "Microsoft.Windows.Photos_8wekyb3d8bbwe!App", "exe": False},
    {"name": "Notepad", "id": "notepad.exe", "exe": True},
]


@pytest.fixture
def opened(env, monkeypatch, tmp_path):
    apps._cache.update(at=time.time(), apps=FAKE_APPS)
    log = {"popen": [], "start": [], "web": [], "packaged": []}
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, *a, **k: log["popen"].append(cmd))
    monkeypatch.setattr(launcher.os, "startfile", lambda p: log["start"].append(p), raising=False)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: log["web"].append(u))
    monkeypatch.setattr(launcher, "activate_packaged", lambda aumid, path: (log["packaged"].append((aumid, path)) or True, "OK"))
    monkeypatch.setattr(launcher, "app_path", lambda exe: r"C:\x\\" + exe)
    monkeypatch.setattr(launcher, "youtube_first", lambda q: {"id": "abcdefghijk", "title": q})
    log["tmp"] = tmp_path
    return log


def _file(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def test_file_types_understood_in_vietnamese():
    assert ".png" in launcher.exts_from(["ảnh"]) and ".mp4" in launcher.exts_from("video")
    assert ".mp3" in launcher.exts_from(["nhạc"]) and launcher.exts_from([".PNG"]) == [".png"]
    assert launcher.exts_from(["pdf"]) == [".pdf"] and launcher.exts_from(["khong hieu gi"]) == []


def test_site_and_browser_slang():
    assert launcher.pick_site("utb") == "youtube" and launcher.pick_site(None) == "google"
    assert launcher.pick_browser("gg") == "chrome" and launcher.pick_browser("Edge") == "edge"
    assert launcher.pick_browser("") is None


def test_open_url_youtube_in_chosen_browser(opened):
    r = launcher.open_url({"query": "nhạc phật", "site": "utb", "browser": "edge"}, True)
    assert r["browser"] == "edge" and r["opened_url"].endswith("watch?v=abcdefghijk")
    assert opened["popen"][-1][0].endswith("msedge.exe")
    r = launcher.open_url({"query": "âm thầm bên em", "browser": "gg", "site": "youtube", "play": False}, True)
    assert "results?search_query=" in r["opened_url"] and opened["popen"][-1][0].endswith("chrome.exe")
    assert "error" in launcher.open_url({"query": "x"}, False)
    r = launcher.open_url({"query": "github.com/foo"}, True)  # địa chỉ trần -> https + trình duyệt mặc định
    assert r["opened_url"] == "https://github.com/foo" and opened["web"] == ["https://github.com/foo"]


def test_open_file_with_store_app_and_win32_app(opened):
    f = _file(opened["tmp"], "bai hat.mp3")
    r = launcher.open_file(f, "media player", True)
    assert r["with"] == "Media Player" and opened["packaged"] == [(FAKE_APPS[0]["id"], f)]
    r = launcher.open_file(f, "chrome", True)
    assert r["with"] == "Google Chrome" and opened["popen"][-1] == [r"C:\x\chrome.exe", f]
    assert "error" in launcher.open_file(f, "app khong ton tai xyz", True) and not opened["start"]
    assert "error" in launcher.open_file(f, None, False), "chỉ admin"


def test_executables_are_never_opened_as_documents(opened):
    f = _file(opened["tmp"], "setup.exe")
    assert "error" in launcher.open_file(f, None, True) and not opened["start"] and not opened["popen"]


def test_learned_rule_applies_and_persists(opened):
    r = launcher.set_rules(["ảnh", "video"], "media player", "admin", True)
    assert r["saved"] and r["app"] == "Media Player" and ".png" in r["extensions"] and ".mp4" in r["extensions"]
    assert launcher.rule_for(".jpg") == "Media Player" and launcher.rule_for(".pdf") is None
    img = _file(opened["tmp"], "a.png")
    out = launcher.open_file(img, None, True)  # không chỉ định app => dùng quy tắc đã học
    assert out["with"] == "Media Player" and out["chosen_by"] == "quy tắc đã học" and opened["packaged"][-1][1] == img
    doc = _file(opened["tmp"], "b.pdf")
    out = launcher.open_file(doc, None, True)  # chưa có quy tắc => app mặc định Windows
    assert out["chosen_by"] == "mặc định" and opened["start"] == [doc]
    listing = launcher.manage_rules({"action": "list"}, True)
    assert listing["rules"][0]["app"] == "Media Player" and "anh" in listing["rules"][0]["groups"]
    assert launcher.manage_rules({"action": "remove", "types": ["video"]}, True)["removed"] > 5
    assert launcher.rule_for(".mp4") is None and launcher.rule_for(".png") == "Media Player"


def test_rule_rejects_unknown_app_and_non_admin(opened):
    assert "error" in launcher.set_rules(["anh"], "app khong co xyz", "u", True)
    assert "error" in launcher.set_rules(["anh"], "media player", "u", False)
    assert "error" in launcher.set_rules(["gì đó lạ"], "media player", "u", True)


def test_file_action_open_is_immediate_and_uses_rule(opened):
    from brain.actions import propose
    from brain.catalog import connect
    f = _file(opened["tmp"], "c.mp4")
    c = connect()
    c.execute("INSERT INTO files (path, name, ext, status, acl) VALUES (?,?,?,?,?)", (f, "c.mp4", ".mp4", "active", "admin"))
    fid = c.execute("SELECT file_id FROM files WHERE path=?", (f,)).fetchone()[0]
    c.commit()
    c.close()
    launcher.set_rules(["video"], "media player", "admin", True)
    r = propose({"action": "open", "file_id": fid}, "admin", True)
    assert r.get("opened") == "c.mp4" and r["with"] == "Media Player" and "pending_action" not in r
    assert "error" in propose({"action": "open", "file_id": fid}, "u", False)


def test_open_with_defaults_to_last_opened_file(opened):
    from brain.catalog import connect
    f = _file(opened["tmp"], "d.mp3")
    c = connect()
    c.execute("INSERT INTO files (path, name, ext, status, acl) VALUES (?,?,?,?,?)", (f, "d.mp3", ".mp3", "active", "admin"))
    fid = c.execute("SELECT file_id FROM files WHERE path=?", (f,)).fetchone()[0]
    c.commit()
    c.close()
    launcher._last.clear()
    assert "error" in launcher.open_file_by_id(None, "chrome", True), "chưa mở gì thì không đoán file"
    launcher.open_file_by_id(fid, "media player", True)
    r = launcher.open_file_by_id(None, "chrome", True)  # "mở bằng app chrome nè"
    assert r["opened"] == "d.mp3" and r["with"] == "Google Chrome"
