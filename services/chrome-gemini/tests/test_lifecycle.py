"""Real-Chrome lifecycle regression (phase G). Opt-in: LGA_RUN_LIFECYCLE=1, needs
`cargo build -p lga-gateway --example lifecycle_host`. Uses runtime/lifecycle-test-profile only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("lifecycle_probe.py")

pytestmark = pytest.mark.skipif(os.environ.get("LGA_RUN_LIFECYCLE") != "1", reason="set LGA_RUN_LIFECYCLE=1")


@pytest.fixture(scope="module")
def report() -> dict:
    env = {**os.environ, "LGA_GRACEFUL": "1", "PYTHONUTF8": "1"}
    done = subprocess.run([sys.executable, str(PROBE), "A", "B", "C", "DE"], capture_output=True, text=True, env=env, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_chrome_was_really_started_in_every_scenario(report):
    # Guards the "nothing left behind" checks below against passing because no browser ever ran.
    assert report["A"][2]["chrome_browser"]["pid"] and report["B"][0]["chrome_browser"]["pid"] and report["C"][0]["chrome_browser"]["pid"]


def test_normal_shutdown_leaves_nothing_behind(report):
    shutdown = report["A"][3]
    assert shutdown["chrome_pids_still_alive"] == [] and shutdown["after_normal_shutdown"]["chrome_processes"] == 0


def test_adapter_stop_closes_the_chrome_it_owns(report):
    after = report["B"][1]["after_adapter_stop"]
    assert after["adapter"]["alive"] is False and after["chrome_processes"] == 0


def test_gateway_crash_leaves_no_orphans(report):
    crash = report["C"][1]
    assert crash["adapter"]["alive"] is False and crash["chrome_pids_still_alive"] == []


def test_external_chrome_survives_adapter_stop_restart_and_host_exit(report):
    steps = {k: v for item in report["DE"] for k, v in item.items()}
    assert steps["D_after_adapter_stop"]["external_alive"] is True
    assert steps["E_restart"]["same_browser_pid"] is True and steps["E_restart"]["same_port"] is True
    assert steps["D_after_host_quit"]["external_alive"]["alive"] is True


def test_nothing_is_left_on_the_test_or_production_profile(report):
    assert report["test_profile_leftovers"] == [] and report["test_profile_listeners"] == []
    assert report["production_profile_chrome_pids"]["before"] == report["production_profile_chrome_pids"]["after"]
