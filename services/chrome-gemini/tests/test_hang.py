"""Real-Chrome hang regression (phase H3). Opt-in: LGA_RUN_LIFECYCLE=1, needs
`cargo build -p lga-gateway --example gateway_host`. Uses runtime/hang-test-profile only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("hang_probe.py")

pytestmark = pytest.mark.skipif(os.environ.get("LGA_RUN_LIFECYCLE") != "1", reason="set LGA_RUN_LIFECYCLE=1")


@pytest.fixture(scope="module")
def report() -> dict:
    done = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True, timeout=600,
                          env={**os.environ, "PYTHONUTF8": "1"})
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_frozen_owned_chrome_with_a_chat_in_flight_is_cut_at_the_deadline(report):
    a = report["A"]
    assert a["before"]["chrome_browser_pids"] and a["suspended_chrome_pid"] in a["before"]["chrome_browser_pids"]
    # grace 1 s + lock margin 3 s, then the gateway terminates the adapter; never the full wait.
    assert a["gateway_quit"]["seconds"] < 10 and a["gateway_quit"]["host_exit"] == 0
    assert a["after"] == {"adapter": False, "chrome_browser": False, "external_chrome": False,
                          "adapter_listening": False, "profile_chrome_processes": 0}
    assert "reply" not in a["chat_during_shutdown"] or a["chat_during_shutdown"]["reply"].get("status") != "COMPLETED"
    assert a["cleanup_leftovers_before_cleanup"] == []  # nothing needed cleaning: the job did it


def test_frozen_external_chrome_is_never_closed_or_killed_by_the_adapter(report):
    b = report["B"]
    assert b["after"]["adapter"] is False and b["after"]["adapter_listening"] is False
    assert b["after"]["external_chrome"] is True and b["after"]["chrome_browser"] is True
    assert b["gateway_quit"]["seconds"] < 10


def test_idle_shutdown_of_a_frozen_owned_chrome_is_bounded(report):
    c = report["C"]
    assert c["gateway_quit"]["seconds"] < 20  # close budget (8 s) + exit; production waits grace + 15 s
    assert c["after"]["adapter"] is False and c["after"]["adapter_listening"] is False
    assert c["after"]["profile_chrome_processes"] == 0


def test_hang_probe_left_nothing_and_production_is_untouched(report):
    assert report["leftovers"] == {"chrome": [], "adapters": []}
    assert report["production_profile_chrome_pids"]["before"] == report["production_profile_chrome_pids"]["after"]
