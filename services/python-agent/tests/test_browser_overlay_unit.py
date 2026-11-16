"""Unit tests of the browser overlay logic (no Chrome)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lga_worker.browser import overlay as ov
from lga_worker.errors import ErrorCode, ToolError


def test_progress_before_any_step() -> None:
    assert ov.describe_progress(None) == ("Đang chuẩn bị…", False)
    assert ov.describe_progress({"status": "planning", "steps": []}) == ("Đang lập kế hoạch…", False)


def test_progress_running_step_shows_tool_and_thought() -> None:
    record = {"status": "executing", "steps": [
        {"tool": "browser.open", "finished_at": "x", "thought": "a"},
        {"tool": "browser.click", "finished_at": None, "thought": "(continuing the batch)  Click   the search box"},
    ]}
    line, done = ov.describe_progress(record)
    assert not done
    assert line == "Bước 2 · browser.click · đang chạy — Click the search box"


def test_progress_between_steps_and_truncation() -> None:
    record = {"status": "planning", "steps": [{"tool": "browser.navigate", "finished_at": "x", "thought": "y" * 500}]}
    line, done = ov.describe_progress(record)
    assert line.startswith("Bước 1 xong · đang nghĩ bước tiếp")
    assert len(line) == ov.PROGRESS_CHARS and line.endswith("…") and not done


@pytest.mark.parametrize("status,text", [("completed", "Đã xong"), ("cancelled", "Đã dừng"),
                                         ("failed", "Tác vụ thất bại"), ("needs_user", "Đang chờ bạn trả lời")])
def test_progress_terminal(status: str, text: str) -> None:
    assert ov.describe_progress({"status": status, "steps": [{"tool": "x"}]}) == (text, True)


def test_read_record_tolerates_missing_and_partial_files(tmp_path: Path) -> None:
    assert ov.read_record(tmp_path, "t1") is None
    (tmp_path / "t1.json").write_text('{"status": "exec', encoding="utf-8")  # being rewritten by the core
    assert ov.read_record(tmp_path, "t1") is None
    (tmp_path / "t1.json").write_text(json.dumps({"status": "executing"}), encoding="utf-8")
    assert ov.read_record(tmp_path, "t1") == {"status": "executing"}


def test_cancel_marker_matches_core_name(tmp_path: Path) -> None:
    path = ov.write_cancel_marker(tmp_path / "tasks", "20261022-1")
    assert path == tmp_path / "tasks" / "20261022-1.cancel" and path.read_text(encoding="utf-8")


def test_overlay_script_is_excluded_from_browser_use_and_has_binding() -> None:
    assert 'data-browser-use-exclude", "true"' in ov.OVERLAY_JS
    assert ov.BINDING in ov.OVERLAY_JS and "__BINDING__" not in ov.OVERLAY_JS


def test_stop_cancels_calls_writes_marker_and_is_idempotent(tmp_path: Path) -> None:
    stopped: list[str] = []

    async def scenario() -> None:
        ctl = ov.OverlayController(tmp_path, stopped.append)
        await ctl.stop()  # no task: nothing happens
        assert not stopped
        ctl._task_id = "t9"
        await ctl.stop()
        await ctl.stop()
        assert stopped == ["t9"] and ctl.stopped_task == "t9"
        assert (tmp_path / "t9.cancel").exists()

    asyncio.run(scenario())


def test_provider_refuses_calls_of_a_stopped_task(tmp_path: Path) -> None:
    from lga_common.config import load_config
    from lga_worker.browser.provider import BrowserProvider, BrowserSettings

    settings = BrowserSettings.from_config(load_config())
    provider = BrowserProvider(settings)

    async def scenario() -> None:
        provider.task_id = "t1"
        provider.overlay.stopped_task = "t1"
        with pytest.raises(ToolError) as exc:
            async with provider.attached(5.0):
                pass
        assert exc.value.code == ErrorCode.CANCELLED

    asyncio.run(scenario())
