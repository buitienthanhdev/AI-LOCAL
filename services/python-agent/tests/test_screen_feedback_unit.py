"""Unit tests for the screen-action feedback helpers (pure functions; no window is touched)."""

from __future__ import annotations

from lga_worker.windows.feedback import no_change_note, observation_signature, pointer_line, pointer_target


def _obs(**over):
    base = {
        "working": {"hwnd": 10, "title": "Untitled - Notepad", "state": "normal"},
        "foreground": {"hwnd": 10, "title": "Untitled - Notepad", "state": "normal"},
        "focused": {"control_type": "EditControl", "name": "Text editor", "text": "hello", "value": None},
        "texts": ["hello"],
        "elements": [{"control_type": "ButtonControl", "name": "Save", "value": None, "state": None, "center": {"x": 1, "y": 2}}],
        "surprise": None,
    }
    base.update(over)
    return base


def test_identical_screens_have_the_same_signature_and_get_a_note():
    a, b = observation_signature(_obs()), observation_signature(_obs())
    assert a == b
    note = no_change_note(a, b)
    assert note and "nothing visible changed" in note


def test_first_observation_and_real_changes_get_no_note():
    a = observation_signature(_obs())
    assert no_change_note(None, a) is None
    for changed in (
        _obs(texts=["hello", "world"]),
        _obs(focused={"control_type": "EditControl", "name": "Text editor", "text": "hello!", "value": None}),
        _obs(foreground={"hwnd": 11, "title": "Save As", "state": "normal"}),
        _obs(surprise="a dialog opened"),
        _obs(elements=[{"control_type": "ButtonControl", "name": "Save", "value": None, "state": "disabled"}]),
    ):
        assert no_change_note(a, observation_signature(changed)) is None


def test_a_moved_window_is_not_a_change():
    moved = _obs()
    moved["elements"][0]["center"] = {"x": 500, "y": 600}
    assert observation_signature(_obs()) == observation_signature(moved)


def test_named_control_is_described_and_not_weak():
    text, weak = pointer_target({"name": "Save", "control_type": "ButtonControl", "automation_id": "btnSave"})
    assert text == "button 'Save' #btnSave" and not weak
    assert "locate it again" not in pointer_line(5, 6, {"name": "Save", "control_type": "ButtonControl", "automation_id": ""})


def test_unnamed_pane_or_nothing_is_weak_and_says_how_to_recover():
    for element in (None, {"name": "", "control_type": "PaneControl", "automation_id": ""}, {"name": "  ", "control_type": "", "automation_id": ""}):
        line = pointer_line(100, 200, element)
        assert line.startswith("Under the pointer at (100,200):")
        assert "locate it again" in line and "desktop.find_element" in line


def test_unnamed_but_meaningful_control_is_not_weak():
    # an unnamed edit box is still a real target
    _, weak = pointer_target({"name": "", "control_type": "EditControl", "automation_id": ""})
    assert not weak
