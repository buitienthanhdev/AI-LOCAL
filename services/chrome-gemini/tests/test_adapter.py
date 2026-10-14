"""Adapter and transaction tests against a scripted fake page. No browser, no Gemini, no network."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from chrome_gemini.adapter import ChromeGeminiAdapter
from chrome_gemini.cdp_page import load_selectors
from chrome_gemini.contract import ChatRequest, ErrorClass, TransactionStatus
from chrome_gemini.page import PageError, PageState, SessionInfo
from chrome_gemini.server import build_app


class FakePage:
    """Scripted page. `states` are returned by read_state in order; the last one repeats."""

    def __init__(self, states=None, *, logged_in=True, baseline=0, submit_error=None, session_error=None,
                 read_error_after=None, model="Pro"):
        self.states = list(states or [])
        self.logged_in = logged_in
        self.baseline = baseline
        self.submit_error = submit_error
        self.session_error = session_error
        self.read_error_after = read_error_after
        self.model = model
        self.submits: list[str] = []
        self.reads = 0
        self.new_conversations = 0

    async def ensure_session(self):
        if self.session_error:
            raise self.session_error
        return SessionInfo(logged_in=self.logged_in, model=self.model)

    async def read_model(self):
        from chrome_gemini.page import ModelReading

        return ModelReading(self.model, "verified" if self.model else "unknown")

    async def new_conversation(self):
        self.new_conversations += 1
        return self.baseline

    async def submit(self, prompt):
        self.submits.append(prompt)
        if self.submit_error:
            raise self.submit_error

    async def read_state(self):
        self.reads += 1
        if self.read_error_after is not None and self.reads > self.read_error_after:
            raise PageError(ErrorClass.CONNECTION_LOST, "socket closed", True)
        index = min(self.reads - 1, len(self.states) - 1)
        return self.states[index]


def done(text="xin chào"):
    return PageState(turns=1, text=text, generating=False)


def generating(text=""):
    return PageState(turns=1 if text else 0, text=text, generating=True)


def adapter(page, **kw):
    return ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, queue_wait_s=0.05, **kw)


def run(coro):
    return asyncio.run(coro)


def test_completed_needs_stable_finished_answer():
    page = FakePage([generating(), generating("xin"), done("xin chào"), done("xin chào")])
    result = run(adapter(page).chat(ChatRequest("hi", "t1", timeout_s=5)))
    assert result.status is TransactionStatus.COMPLETED
    assert result.content == "xin chào"
    assert result.model_verified == "Pro"
    assert page.submits == ["hi"]
    assert page.new_conversations == 1


def test_changing_text_is_not_stable_until_it_stops():
    page = FakePage([done("a"), done("ab"), done("abc"), done("abc")])
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=5)))
    assert result.status is TransactionStatus.COMPLETED and result.content == "abc"


def test_unproven_completion_stays_generating():
    # The page never says "not generating": text may be final but this cannot be proven.
    page = FakePage([PageState(turns=1, text="done?", generating=None)])
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=0.05)))
    assert result.status is TransactionStatus.GENERATING
    assert result.error_class is ErrorClass.INCOMPLETE
    assert result.content == ""  # a partial answer is never handed out as the answer
    assert page.submits == ["hi"]


def test_timeout_without_any_page_reaction_is_submission_uncertain():
    page = FakePage([PageState(turns=0, text="", generating=False)])
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=0.05)))
    assert result.status is TransactionStatus.SUBMISSION_UNCERTAIN
    assert len(page.submits) == 1


def test_submit_error_after_click_is_uncertain_and_never_resent():
    page = FakePage(submit_error=PageError(ErrorClass.CONNECTION_LOST, "dropped", submitted_possible=True))
    a = adapter(page)
    first = run(a.chat(ChatRequest("hi", "same", timeout_s=1)))
    second = run(a.chat(ChatRequest("hi", "same", timeout_s=1)))
    assert first.status is TransactionStatus.SUBMISSION_UNCERTAIN
    assert second is first  # recorded outcome returned
    assert page.submits == ["hi"]  # exactly one attempt at the page


def test_submit_error_before_click_is_not_submitted_and_may_retry():
    page = FakePage(submit_error=PageError(ErrorClass.SUBMIT_FAILED, "no input", submitted_possible=False))
    a = adapter(page)
    first = run(a.chat(ChatRequest("hi", "x", timeout_s=1)))
    assert first.status is TransactionStatus.NOT_SUBMITTED
    page.submit_error = None
    page.states = [done()]
    second = run(a.chat(ChatRequest("hi", "x", timeout_s=1)))
    assert second.status is TransactionStatus.COMPLETED


def test_connection_lost_while_waiting():
    page = FakePage([generating("par")], read_error_after=2)
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=5)))
    assert result.status is TransactionStatus.GENERATING
    assert result.error_class is ErrorClass.CONNECTION_LOST
    assert len(page.submits) == 1


def test_connection_lost_before_any_confirmation_is_uncertain():
    page = FakePage([PageState(0, "", False)], read_error_after=0)
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=5)))
    assert result.status is TransactionStatus.SUBMISSION_UNCERTAIN


def test_page_error_banner_after_submit_is_failed():
    page = FakePage([PageState(turns=1, text="", generating=False, error="Something went wrong")])
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=5)))
    assert result.status is TransactionStatus.FAILED
    assert result.error_class is ErrorClass.PAGE_ERROR


@pytest.mark.parametrize("page,expected", [
    (FakePage(logged_in=False), ErrorClass.NOT_LOGGED_IN),
    (FakePage(baseline=1), ErrorClass.CONVERSATION_NOT_CLEAN),
    (FakePage(session_error=PageError(ErrorClass.BROWSER_UNAVAILABLE, "no chrome")), ErrorClass.BROWSER_UNAVAILABLE),
    (FakePage(session_error=PageError(ErrorClass.SELECTORS_UNVERIFIED, "unverified")), ErrorClass.SELECTORS_UNVERIFIED),
])
def test_refusals_before_sending_never_touch_the_prompt(page, expected):
    result = run(adapter(page).chat(ChatRequest("hi", "t", timeout_s=1)))
    assert result.status is TransactionStatus.NOT_SUBMITTED
    assert result.error_class is expected
    assert page.submits == []


def test_attachments_are_refused_not_dropped():
    page = FakePage([done()])
    result = run(adapter(page).chat(ChatRequest("describe", "t", attachments=["a.png"])))
    assert result.status is TransactionStatus.NOT_SUBMITTED
    assert result.error_class is ErrorClass.UNSUPPORTED
    assert page.submits == [] and page.new_conversations == 0


def test_concurrent_requests_are_serialised_and_do_not_mix():
    class SlowPage(FakePage):
        async def read_state(self):
            await asyncio.sleep(0.01)
            return PageState(turns=1, text=f"answer to {self.submits[-1]}", generating=False)

    async def scenario():
        page = SlowPage()
        a = ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, queue_wait_s=5)
        results = await asyncio.gather(*(a.chat(ChatRequest(f"q{i}", f"t{i}", timeout_s=5)) for i in range(4)))
        return page, results

    page, results = run(scenario())
    assert [r.status for r in results] == [TransactionStatus.COMPLETED] * 4
    assert [r.content for r in results] == [f"answer to q{i}" for i in range(4)]
    assert page.submits == ["q0", "q1", "q2", "q3"]


def test_busy_when_the_lock_cannot_be_taken():
    async def scenario():
        page = FakePage([generating()])  # never finishes
        a = ChromeGeminiAdapter(page, poll_s=0.001, queue_wait_s=0.02)
        long = asyncio.create_task(a.chat(ChatRequest("one", "a", timeout_s=0.3)))
        await asyncio.sleep(0.05)
        other = await a.chat(ChatRequest("two", "b", timeout_s=1))
        await long
        return page, other

    page, other = run(scenario())
    assert other.status is TransactionStatus.NOT_SUBMITTED and other.error_class is ErrorClass.BUSY
    assert page.submits == ["one"]


def test_same_id_different_prompt_is_refused():
    page = FakePage([done()])
    a = adapter(page)
    run(a.chat(ChatRequest("one", "id", timeout_s=1)))
    other = run(a.chat(ChatRequest("two", "id", timeout_s=1)))
    assert other.error_class is ErrorClass.DUPLICATE and page.submits == ["one"]


def test_cancellation_keeps_the_record_and_does_not_resend():
    async def scenario():
        page = FakePage([generating("part")])
        a = ChromeGeminiAdapter(page, poll_s=0.001, queue_wait_s=0.05)
        task = asyncio.create_task(a.chat(ChatRequest("hi", "c", timeout_s=30)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        again = await a.chat(ChatRequest("hi", "c", timeout_s=1))
        return page, again

    page, again = run(scenario())
    assert page.submits == ["hi"]
    assert again.status is TransactionStatus.GENERATING  # honest: never proven complete


def test_selectors_must_be_verified(tmp_path):
    path = tmp_path / "selectors.json"
    with pytest.raises(PageError) as missing:
        load_selectors(path)
    assert missing.value.error_class is ErrorClass.SELECTORS_UNVERIFIED
    base = {"input": "a", "send": "b", "answer": "c", "generating": "d", "login_marker": "e"}
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(PageError):
        load_selectors(path)
    path.write_text(json.dumps({**base, "verified": True}), encoding="utf-8")
    assert load_selectors(path)["input"] == "a"
    path.write_text(json.dumps({"verified": True, "input": "a"}), encoding="utf-8")
    with pytest.raises(PageError):
        load_selectors(path)


def test_wire_shape_matches_the_rust_contract():
    result = run(adapter(FakePage([done()])).chat(ChatRequest("hi", "w", timeout_s=2)))
    wire = result.to_dict()
    assert wire["status"] == "COMPLETED" and wire["error"] is None
    assert set(wire["metadata"]) >= {"provider", "model_verified", "started_at", "finished_at", "signals"}
    refused = run(adapter(FakePage(logged_in=False)).chat(ChatRequest("hi", "w2"))).to_dict()
    assert refused["error"] == {"class": "not_logged_in", "message": "Gemini is not signed in"}


def test_http_layer():
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        page = FakePage([done("ok")])
        async with TestClient(TestServer(build_app(adapter(page), chat_token="chat-tok"))) as client:
            health = await (await client.get("/health")).json()
            auth = {"Authorization": "Bearer chat-tok"}
            good = await (await client.post("/v1/chat", headers=auth, json={"prompt": "hi", "transaction_id": "h1", "timeout_s": 5})).json()
            bad = await client.post("/v1/chat", headers=auth, json={"nope": 1})
            return health, good, bad.status, page

    health, good, bad_status, page = run(scenario())
    assert health["state"] == "ready" and health["capabilities"]["images"] is False
    assert good["status"] == "COMPLETED" and good["content"] == "ok"
    assert bad_status == 400
    assert page.submits == ["hi"]


# ---------------------------------------------------------------- phase E: fail-closed on the real driver
VERIFIED = {"verified": True, "input": "rich-textarea div[role=textbox]", "send": "button.send",
            "answer": "model-response", "generating": "button.stop", "login_marker": "a.account"}


class RecordingDriver:
    """Stands in for the CDP transport of CdpGeminiPage: records every call, answers from a table."""

    def __init__(self, page, answers):
        self.calls: list[tuple[str, str]] = []
        self.answers = answers  # substring of the JS -> value
        page._connect = self._connect
        page._call = self._call
        page._eval = self._eval

    async def _connect(self, launch=True):
        return object()

    async def _call(self, method, params=None, **kw):
        self.calls.append((method, json.dumps(params or {})))
        return {}

    async def _eval(self, expression, **kw):
        self.calls.append(("Runtime.evaluate", expression))
        for needle, value in self.answers.items():
            if needle in expression:
                return value
        return None

    def methods(self):
        return [m for m, _ in self.calls]


def make_page(tmp_path, selectors):
    from chrome_gemini.cdp_page import CdpGeminiPage

    path = tmp_path / "selectors.json"
    if selectors is not None:
        path.write_text(json.dumps(selectors), encoding="utf-8")
    return CdpGeminiPage(None, tmp_path / "profile", tmp_path / "logs", path)


@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("chrome_gemini.cdp_page.asyncio.sleep", instant)


@pytest.mark.parametrize("selectors", [None, {**VERIFIED, "verified": False}])
def test_missing_or_unverified_selectors_never_start_chrome_or_send(tmp_path, selectors):
    page = make_page(tmp_path, selectors)
    started = []

    async def forbidden():
        started.append(True)

    page._ensure_chrome = forbidden
    driver = RecordingDriver(page, {})
    result = run(ChromeGeminiAdapter(page, poll_s=0.001).chat(ChatRequest("hi", "e5a", timeout_s=1)))
    assert result.status is TransactionStatus.NOT_SUBMITTED
    assert result.error_class is ErrorClass.SELECTORS_UNVERIFIED
    assert started == [] and driver.calls == []  # not even a read, let alone input


def test_selector_that_matches_nothing_never_sends(tmp_path, no_sleep):
    page = make_page(tmp_path, VERIFIED)
    driver = RecordingDriver(page, {"a.account": True, "document.querySelector": False})
    result = run(ChromeGeminiAdapter(page, poll_s=0.001).chat(ChatRequest("secret prompt", "e5b", timeout_s=1)))
    assert result.status is TransactionStatus.NOT_SUBMITTED
    assert "Input.insertText" not in driver.methods()
    assert not any("click()" in c for _, c in driver.calls)


def test_missing_input_box_at_submit_is_not_submitted(tmp_path):
    page = make_page(tmp_path, VERIFIED)
    driver = RecordingDriver(page, {"focus()": False})
    page._selectors = dict(VERIFIED)
    with pytest.raises(PageError) as caught:
        run(page.submit("secret prompt"))
    assert caught.value.error_class is ErrorClass.SUBMIT_FAILED and caught.value.submitted_possible is False
    assert "Input.insertText" not in driver.methods()


def test_unreadable_completion_signal_is_never_completed(tmp_path):
    # Answer text is present and stable, but the page exposes no "generating" element reading at all.
    page = make_page(tmp_path, VERIFIED)
    RecordingDriver(page, {"turns:a.length": {"turns": 1, "text": "final?", "generating": None, "error": None}})
    page._selectors = dict(VERIFIED)
    result = run(ChromeGeminiAdapter(FakeWrap(page), poll_s=0.001, stable_polls=2)._await_answer(
        __import__("chrome_gemini.contract", fromlist=["TransactionResult"]).TransactionResult("e5d", TransactionStatus.SUBMISSION_UNCERTAIN), 0.05))
    assert result.status is TransactionStatus.GENERATING and result.status is not TransactionStatus.COMPLETED


class FakeWrap:
    """Adapter-facing view of CdpGeminiPage whose read_state maps a missing signal to generating=None."""

    def __init__(self, page):
        self._page = page

    async def read_state(self):
        state = await self._page.read_state()
        value = await self._page._eval("turns:a.length")
        generating = value.get("generating") if isinstance(value, dict) else None
        return PageState(state.turns, state.text, generating, state.error)


def test_model_label_whitespace_is_collapsed(tmp_path):
    page = make_page(tmp_path, {**VERIFIED, "model_label": "bard-mode-switcher button"})
    RecordingDriver(page, {"a.account": True, "readyState": {"ready": "complete", "label": "Gemini\nFlash"}})
    session = run(page.ensure_session())
    assert session.model == "Gemini Flash" and session.logged_in is True


def test_shipped_selector_file_is_complete():
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "configs" / "chrome-gemini.selectors.json"
    selectors = load_selectors(path)
    assert {"input", "send", "answer", "generating", "login_marker"} <= set(selectors)


# ------------------------------------------------------------------ phase G: ownership and shutdown
class FakeChrome:
    def __init__(self, pid=4242, alive=True, exits_on_close=True):
        self.pid, self._alive, self.exits_on_close = pid, alive, exits_on_close
        self.cdp_url = "http://127.0.0.1:1"
        self.launched_at = 1.0

    def is_alive(self):
        return self._alive


@pytest.fixture
def fake_chrome_mod(monkeypatch):
    """Replace lga_worker.browser.chrome's process functions; record what the adapter does to Chrome."""
    from lga_worker.browser import chrome as chrome_mod

    state = {"found": None, "launched": [], "closed": []}

    def find_running(profile):
        return state["found"] if state["found"] and state["found"].is_alive() else None

    def launch(executable, profile, headless, timeout, log_dir):
        chrome = FakeChrome(pid=7000 + len(state["launched"]))
        state["launched"].append(chrome.pid)
        state["found"] = chrome
        return chrome

    async def close_gracefully(cdp_url, timeout_s):
        chrome = state["found"]
        state["closed"].append(chrome.pid)
        if chrome.exits_on_close:
            chrome._alive = False
        return True

    monkeypatch.setattr(chrome_mod, "find_running", find_running)
    monkeypatch.setattr(chrome_mod, "launch", launch)
    monkeypatch.setattr(chrome_mod, "close_gracefully", close_gracefully)
    monkeypatch.setattr(chrome_mod, "page_targets", lambda url: [])
    return state


def page_with_profile(tmp_path):
    from chrome_gemini.cdp_page import CdpGeminiPage

    chrome_exe = tmp_path / "chrome.exe"
    chrome_exe.write_text("x")
    return CdpGeminiPage(chrome_exe, tmp_path / "profile", tmp_path / "logs", tmp_path / "selectors.json")


def test_ownership_record_only_matches_its_own_pid(tmp_path):
    from chrome_gemini import ownership

    assert ownership.owns(tmp_path, 1) is False
    ownership.claim(tmp_path, 1)
    assert ownership.owns(tmp_path, 1) is True
    assert ownership.owns(tmp_path, 2) is False  # a stale record never extends to another browser
    ownership.release(tmp_path)
    assert ownership.owns(tmp_path, 1) is False


def test_external_chrome_is_reused_and_never_closed(tmp_path, fake_chrome_mod):
    page = page_with_profile(tmp_path)
    fake_chrome_mod["found"] = FakeChrome(pid=100)  # started by someone else
    run(page._ensure_chrome())
    assert fake_chrome_mod["launched"] == []
    outcome = run(page.close_owned())
    assert outcome["closed"] is False and "external" in outcome["reason"]
    assert fake_chrome_mod["closed"] == [] and fake_chrome_mod["found"].is_alive()


def test_chrome_launched_by_the_adapter_is_closed_gracefully_and_released(tmp_path, fake_chrome_mod):
    from chrome_gemini import ownership

    page = page_with_profile(tmp_path)
    run(page._ensure_chrome())
    pid = fake_chrome_mod["launched"][0]
    assert ownership.owns(tmp_path / "profile", pid)
    outcome = run(page.close_owned(timeout_s=1))
    assert outcome == {"closed": True, "pid": pid}
    assert fake_chrome_mod["closed"] == [pid]
    assert not ownership.owns(tmp_path / "profile", pid)


def test_restarted_adapter_still_owns_chrome_it_launched_before(tmp_path, fake_chrome_mod):
    first = page_with_profile(tmp_path)
    run(first._ensure_chrome())
    pid = fake_chrome_mod["launched"][0]
    second = page_with_profile(tmp_path)  # a new adapter process: no in-memory state
    run(second._ensure_chrome())
    assert fake_chrome_mod["launched"] == [pid - 0] and second._owned is True  # reused, not relaunched
    assert run(second.close_owned(timeout_s=1))["closed"] is True


def test_chrome_that_does_not_exit_is_reported_not_killed(tmp_path, fake_chrome_mod):
    page = page_with_profile(tmp_path)
    run(page._ensure_chrome())
    fake_chrome_mod["found"].exits_on_close = False
    outcome = run(page.close_owned(timeout_s=0.3))
    assert outcome["closed"] is False and "did not exit" in outcome["reason"]
    assert fake_chrome_mod["found"].is_alive()  # no kill was attempted


# ------------------------------------------------------------------ H1: model label polling
class ScriptedEval:
    """_eval answers from a list (exception instances are raised); records the number of polls."""

    def __init__(self, page, script):
        self.script, self.polls = list(script), 0
        page._eval = self._eval

    async def _eval(self, expression, **kw):
        self.polls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fast_model_polling(monkeypatch):
    monkeypatch.setattr("chrome_gemini.cdp_page.MODEL_POLL_S", 0.01)


def label_page(tmp_path):
    page = make_page(tmp_path, {**VERIFIED, "model_label": "bard-mode-switcher button"})
    page._selectors = {**VERIFIED, "model_label": "bard-mode-switcher button"}
    return page


def test_model_label_present_immediately(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    stub = ScriptedEval(page, [{"ready": "complete", "label": "Gemini\nPro"}])
    reading = run(page.read_model(wait_s=1))
    assert (reading.label, reading.status) == ("Gemini Pro", "verified") and stub.polls == 1


def test_model_label_appearing_late_is_waited_for(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    stub = ScriptedEval(page, [{"ready": "loading", "label": ""}, {"ready": "interactive", "label": ""},
                               {"ready": "complete", "label": "Gemini Pro"}])
    reading = run(page.read_model(wait_s=2))
    assert (reading.label, reading.status) == ("Gemini Pro", "verified") and stub.polls == 3


def test_model_label_that_never_appears_is_unknown_not_a_default(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    ScriptedEval(page, [{"ready": "complete", "label": ""}])
    started = time.monotonic()
    reading = run(page.read_model(wait_s=0.15))
    assert reading.label is None and reading.status == "unknown"
    assert time.monotonic() - started < 1.0  # the deadline is honoured; no unbounded wait


def test_model_peek_with_no_label_is_pending_not_unknown(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    ScriptedEval(page, [{"ready": "loading", "label": ""}])
    reading = run(page.read_model(wait_s=0))
    assert reading.label is None and reading.status == "pending"


def test_model_read_survives_navigation_errors_then_reads(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    err = PageError(ErrorClass.CONNECTION_LOST, "execution context destroyed")
    stub = ScriptedEval(page, [err, err, {"ready": "complete", "label": "Gemini Flash"}])
    reading = run(page.read_model(wait_s=2))
    assert (reading.label, reading.status) == ("Gemini Flash", "verified") and stub.polls == 3


def test_model_read_with_permanently_failing_page_ends_unknown(tmp_path, fast_model_polling):
    page = label_page(tmp_path)
    ScriptedEval(page, [PageError(ErrorClass.CONNECTION_LOST, "gone")])
    reading = run(page.read_model(wait_s=0.1))
    assert (reading.label, reading.status) == (None, "unknown")


def test_no_model_selector_means_unknown_without_touching_the_page(tmp_path):
    page = make_page(tmp_path, VERIFIED)
    page._selectors = dict(VERIFIED)
    stub = ScriptedEval(page, [None])
    assert run(page.read_model(wait_s=1)).status == "unknown" and stub.polls == 0


def test_adapter_reports_the_model_status_and_never_defaults_to_pro():
    unknown = FakePage([done()], model=None)
    result = run(adapter(unknown).chat(ChatRequest("hi", "m1", timeout_s=2)))
    assert result.status is TransactionStatus.COMPLETED  # an unknown label does not block, it is reported
    assert result.model_verified is None and result.signals["model_status"] == "unknown"
    known = run(adapter(FakePage([done()], model="Gemini Pro")).chat(ChatRequest("hi", "m2", timeout_s=2)))
    assert known.model_verified == "Gemini Pro" and known.signals["model_status"] == "verified"
