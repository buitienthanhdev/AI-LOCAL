"""Gateway -> adapter -> CDP -> real Chrome -> local stand-in page (phase H4A). Opt-in: LGA_RUN_LIFECYCLE=1,
needs `cargo build -p lga-gateway --example gateway_host`. Uses runtime/e2e-test-profile only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("e2e_probe.py")

pytestmark = pytest.mark.skipif(os.environ.get("LGA_RUN_LIFECYCLE") != "1", reason="set LGA_RUN_LIFECYCLE=1")


@pytest.fixture(scope="module")
def report() -> dict:
    done = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True, timeout=900,
                          env={**os.environ, "PYTHONUTF8": "1"})
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_s1_completed_through_the_gateway_with_verified_model(report):
    s1 = report["S1-6"]["S1"]
    assert s1["reply"]["ok"] is True and s1["reply"]["backend"] == "chrome_gemini"
    assert s1["reply"]["content"] == "ECHO: hello one" and s1["reply"]["model"] == "Gemini Pro"
    assert s1["reply"]["transaction_status"] == "Completed" and s1["reply"]["attempts"] == 1
    assert s1["prompts"] == ["hello one"]


def test_s2_nothing_reaches_the_page_without_the_chat_token(report):
    s2 = report["S1-6"]["S2"]
    assert s2["statuses"] == [403, 403, 403] and s2["submissions_added"] == 0


def test_s3_duplicate_transaction_id_is_sent_once(report):
    s3 = report["S1-6"]["S3"]
    assert (s3["first"], s3["second"], s3["same_content"]) == ("COMPLETED", "COMPLETED", True)
    assert s3["different_prompt"] == ["NOT_SUBMITTED", "duplicate"] and s3["submissions_added"] == 1


def test_s4_concurrent_requests_are_serialised_and_never_mixed(report):
    s4 = report["S1-6"]["S4"]
    done = {k: v for k, v in s4["results"].items() if v["status"] == "COMPLETED"}
    for key, value in done.items():  # each answer belongs to its own question
        assert value["content"] == f"ECHO: question {key[1:]}"
    others = {k: v for k, v in s4["results"].items() if k not in done}
    assert all(v["status"] == "NOT_SUBMITTED" and v["class"] == "busy" for v in others.values())
    assert sorted(s4["submitted"]) == sorted(f"question {k[1:]}" for k in done) and len(done) >= 1


def test_s5_timeout_while_generating_is_uncertain_and_sent_once(report):
    s5 = report["S1-6"]["S5"]
    assert s5["reply"]["ok"] is False and s5["reply"]["kind"] == "submission_uncertain"
    assert s5["reply"]["attempts"] == 1 and s5["submissions_added"] == 1


def test_s6_without_a_completion_signal_it_is_never_completed(report):
    s6 = report["S1-6"]["S6"]
    assert s6["reply"]["ok"] is False and s6["reply"]["kind"] == "submission_uncertain" and s6["submissions_added"] == 1


def test_s7_shutdown_during_generation_ends_uncertain_and_cleans_up(report):
    s7 = report["S7"]
    assert s7["chat"]["status"] == "GENERATING" and s7["chat"]["class"] == "shutdown_interrupted" and s7["chat"]["content"] == ""
    assert s7["submissions_added"] == 1 and s7["quit_seconds"] < 10
    assert s7["after"] == {"chrome": [], "adapters": [], "listening": False}


def test_s8_chrome_dying_mid_generation_is_reported_and_never_resent(report):
    s8 = report["S8"]
    assert s8["chat"]["status"] == "GENERATING" and s8["chat"]["class"] in ("connection_lost", "browser_unavailable")
    assert s8["chat"]["content"] == "" and s8["submissions_added"] == 1
    assert s8["chrome_relaunched"] is False  # neither the read path nor the health probe starts a browser


def test_s9_signed_out_page_sends_nothing(report):
    s9 = report["S9"]
    assert s9["reply"]["ok"] is False and s9["reply"]["kind"] == "config" and s9["submissions_added"] == 0


def test_s10_a_late_answer_is_recorded_beside_the_terminal_result(report):
    s10 = report["S10"]
    assert s10["terminal"]["status"] in ("GENERATING", "SUBMISSION_UNCERTAIN") and s10["terminal"]["content"] == ""
    assert s10["terminal"]["late_at_that_moment"] is None
    assert s10["lookup_while_generating"]["late"] is None  # still generating: nothing to record
    later = s10["lookup_after_answer"]
    assert later["late"]["state"] == "COMPLETED_LATE" and later["late"]["content"] == "ECHO: late answer please"
    # the terminal outcome is exactly what the caller was told, not upgraded after the fact
    assert later["status"] == s10["terminal"]["status"] and later["content"] == "" and later["finished_at_unchanged"] is True
    assert s10["lookup_requires_chat_token"] == [403, 403, 404]
    assert s10["same_id_again"]["has_late"] is True and s10["submissions_added"] == 1


def test_every_prompt_the_page_saw_was_intended_and_nothing_is_left(report):
    intended = {"hello one", "dup prompt", "question 0", "question 1", "question 2", "will not finish",
                "signals are missing", "interrupted by shutdown", "chrome will die", "late answer please"}
    seen = report["all_prompts_seen_by_the_page"]
    assert set(seen) <= intended  # nothing unintended ever reached the page
    assert seen.count("dup prompt") == 1 and seen.count("hello one") == 1
    assert report["leftovers"] == {"chrome": [], "adapters": []}
    assert report["production_profile_chrome_pids"]["before"] == report["production_profile_chrome_pids"]["after"]
