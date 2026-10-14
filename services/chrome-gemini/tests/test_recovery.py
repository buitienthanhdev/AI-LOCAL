"""Real-Chrome recovery regression (phase H1). Opt-in: LGA_RUN_LIFECYCLE=1, needs
`cargo build -p lga-gateway --example gateway_host`. Uses runtime/recovery-test-profile only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("recovery_probe.py")

pytestmark = pytest.mark.skipif(os.environ.get("LGA_RUN_LIFECYCLE") != "1", reason="set LGA_RUN_LIFECYCLE=1")


@pytest.fixture(scope="module")
def report() -> dict:
    done = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True, timeout=600,
                          env={**os.environ, "PYTHONUTF8": "1"})
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def _adapter_roots(entries: list[dict]) -> int:
    """Adapter processes whose parent is not itself an adapter entry (the venv launcher has one child)."""
    pids = {e["pid"] for e in entries}
    return sum(1 for e in entries if e["ppid"] not in pids)


def test_adapter_crash_is_restarted_by_the_gateway_on_the_same_chrome(report):
    b = report["B"]
    assert b["before"]["chrome_browser_pids"], "Chrome must have been running before the crash"
    assert b["restart"]["new_adapter_pid"] not in (None, b["killed_adapter_pid"]) and b["restart"]["seconds"] < 30
    assert b["after"]["health"]["state"] == "ready" and b["after"]["same_chrome"] is True
    assert _adapter_roots(b["after"]["adapters"]) == 1
    assert b["after_second_crash"]["same_chrome"] is True and _adapter_roots(b["after_second_crash"]["adapters"]) == 1


def test_chat_during_an_adapter_crash_is_not_retried(report):
    chat = report["B"]["chat_after_second_kill"]
    assert chat["ok"] is False and chat["attempts"] == 1


def test_gateway_quit_leaves_no_adapter_chrome_or_listener(report):
    after = report["B"]["after_gateway_quit"]
    assert after == {"chrome": [], "adapters": [], "adapter_listening": False}


def test_second_adapter_on_the_same_profile_is_refused(report):
    b2 = report["B2"]
    assert b2["second_exited"] == 3
    assert sorted(b2["listening"].values()) == [False, True] and len(b2["chrome_browser_pids"]) == 1


def test_cdp_client_drop_reconnects_to_the_same_chrome(report):
    c = report["C"]
    assert c["after_client_disconnect"] == {"chrome_alive": True, "cdp_port_serves": True}
    assert c["reconnect_read"]["same_chrome"] and c["reconnect_read"]["same_port"] and c["reconnect_read"]["seconds"] < 10
    assert c["second_drop"]["same_chrome"] is True


def test_chrome_exit_is_reported_once_and_not_relaunched(report):
    exit_ = report["C"]["after_chrome_exit"]
    assert exit_["raised"] == "browser_unavailable" and exit_["chrome_relaunched"] is False
    assert exit_["second_read"]["raised"] == "browser_unavailable" and exit_["second_read"]["chrome_relaunched"] is False
    assert exit_["seconds"] < 2 and report["C"]["cleanup_after"] == []


def test_production_profile_untouched_and_nothing_left(report):
    assert report["production_profile_chrome_pids"]["before"] == report["production_profile_chrome_pids"]["after"]
    assert report["leftovers"] == {"chrome": [], "adapters": []}
