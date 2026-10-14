"""Phase H2: graceful shutdown with a transaction in flight. Fake page, events instead of sleeps, no browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import chrome_gemini.server as server
from chrome_gemini.adapter import ChromeGeminiAdapter
from chrome_gemini.contract import ChatRequest, ErrorClass, TransactionStatus
from chrome_gemini.page import ModelReading, PageState, SessionInfo

TOKEN = "t0k3n-for-tests-only"


class GatedPage:
    """Page whose steps can be held at a gate, and which records everything done to it."""

    def __init__(self, *, hold_new_conversation=False, hold_submit=False):
        self.log: list[str] = []
        self.submits: list[str] = []
        self.close_calls = 0
        self.state = PageState(0, "", False)
        self.in_new_conversation = asyncio.Event()
        self.release_new_conversation = asyncio.Event()
        self.hold_new_conversation = hold_new_conversation
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.hold_submit = hold_submit

    async def ensure_session(self):
        return SessionInfo(True, "Gemini Pro", "verified")

    async def read_model(self):
        return ModelReading("Gemini Pro", "verified")

    async def new_conversation(self):
        self.in_new_conversation.set()
        if self.hold_new_conversation:
            await self.release_new_conversation.wait()
        return 0

    async def submit(self, prompt):
        self.log.append("submit")
        self.submits.append(prompt)
        self.submit_started.set()
        if self.hold_submit:
            await self.release_submit.wait()

    async def read_state(self):
        return self.state

    async def close_owned(self, timeout_s=8.0):
        self.close_calls += 1
        self.log.append("close_chrome")
        return {"closed": True, "pid": 1}


def make(page, **kw):
    return ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, queue_wait_s=kw.pop("queue_wait_s", 5), **kw)


def req(prompt, tid, timeout_s=30):
    return ChatRequest(prompt, tid, timeout_s=timeout_s)


def go(coro):
    return asyncio.run(coro)


async def settle(times=5):
    for _ in range(times):
        await asyncio.sleep(0)


# A -------------------------------------------------------------------------------------------
def test_A_shutdown_with_no_request_closes_owned_chrome_and_refuses_later_chats():
    async def scenario():
        page = GatedPage()
        a = make(page)
        outcome = await a.shutdown(grace_s=1)
        late = await a.chat(req("hi", "late"))
        return page, a, outcome, late

    page, a, outcome, late = go(scenario())
    assert outcome["done"] is True and outcome["chrome"] == {"closed": True, "pid": 1}
    assert page.close_calls == 1 and a.state == "closed"
    assert late.status is TransactionStatus.NOT_SUBMITTED and late.error_class is ErrorClass.SHUTTING_DOWN
    assert page.submits == []


# B -------------------------------------------------------------------------------------------
def test_B_shutdown_before_the_prompt_is_sent_refuses_it_and_sends_nothing():
    async def scenario():
        page = GatedPage(hold_new_conversation=True)
        a = make(page)
        chat = asyncio.create_task(a.chat(req("hi", "b1")))
        await page.in_new_conversation.wait()  # the exchange is prepared but has not sent anything
        down = asyncio.create_task(a.shutdown(grace_s=5))
        await settle()
        assert not down.done()  # waits for the exchange; does not close Chrome under it
        page.release_new_conversation.set()
        result = await chat
        outcome = await down
        return page, result, outcome

    page, result, outcome = go(scenario())
    assert result.status is TransactionStatus.NOT_SUBMITTED and result.error_class is ErrorClass.SHUTTING_DOWN
    assert page.submits == []
    assert outcome["done"] is True and page.close_calls == 1


# C -------------------------------------------------------------------------------------------
def test_C_shutdown_after_send_before_answer_ends_uncertain_and_is_not_resent():
    async def scenario():
        page = GatedPage()
        a = make(page)
        chat = asyncio.create_task(a.chat(req("hi", "c1")))
        await page.submit_started.wait()
        page.state = PageState(1, "partial", True)  # generating: sent and confirmed, no final answer
        outcome = await a.shutdown(grace_s=0.05)
        result = await chat
        again = await a.chat(req("hi", "c1"))  # the same id: the recorded outcome, never the page
        return page, result, outcome, again

    page, result, outcome, again = go(scenario())
    assert result.status is TransactionStatus.GENERATING and result.error_class is ErrorClass.SHUTDOWN_INTERRUPTED
    assert result.content == ""  # partial text is not handed out as an answer
    assert page.submits == ["hi"]
    assert outcome["done"] is True
    assert again is result
    assert page.log == ["submit", "close_chrome"]  # Chrome closed only after the transaction was final


def test_C2_shutdown_after_send_without_any_page_reaction_is_submission_uncertain():
    async def scenario():
        page = GatedPage()
        a = make(page)
        chat = asyncio.create_task(a.chat(req("hi", "c2")))
        await page.submit_started.wait()
        await a.shutdown(grace_s=0.05)
        return page, await chat

    page, result = go(scenario())
    assert result.status is TransactionStatus.SUBMISSION_UNCERTAIN and result.error_class is ErrorClass.SHUTDOWN_INTERRUPTED
    assert page.submits == ["hi"]


# D -------------------------------------------------------------------------------------------
def test_D_answer_completing_during_shutdown_is_delivered_exactly_once():
    async def scenario():
        page = GatedPage()
        a = make(page)
        chat = asyncio.create_task(a.chat(req("hi", "d1")))
        await page.submit_started.wait()
        page.state = PageState(1, "4", False)  # complete and stable
        outcome = await a.shutdown(grace_s=5)
        result = await chat
        return page, result, outcome

    page, result, outcome = go(scenario())
    assert result.status is TransactionStatus.COMPLETED and result.content == "4"
    assert result.error_class is ErrorClass.NONE
    assert outcome["done"] is True and page.close_calls == 1 and page.submits == ["hi"]
    assert page.log == ["submit", "close_chrome"]


# E -------------------------------------------------------------------------------------------
def test_E_competing_chats_and_shutdown_never_run_two_transactions():
    async def scenario():
        page = GatedPage()
        a = make(page, queue_wait_s=10)
        first = asyncio.create_task(a.chat(req("first", "e1")))
        await page.submit_started.wait()
        page.state = PageState(1, "part", True)
        second = asyncio.create_task(a.chat(req("second", "e2")))  # queued behind the lock
        await settle()
        assert a.manager.busy()
        outcome = await a.shutdown(grace_s=0.05)
        results = [await first, await second]
        third = await a.chat(req("third", "e3"))  # arrives after shutdown began
        return page, results, third, outcome

    page, (first, second), third, outcome = go(scenario())
    assert first.error_class is ErrorClass.SHUTDOWN_INTERRUPTED
    for refused in (second, third):
        assert refused.status is TransactionStatus.NOT_SUBMITTED and refused.error_class is ErrorClass.SHUTTING_DOWN
    assert page.submits == ["first"]  # one transaction on the profile, ever
    assert outcome["done"] is True and page.close_calls == 1


# F -------------------------------------------------------------------------------------------
def test_F_shutdown_timeout_is_bounded_reports_state_and_closes_nothing():
    async def scenario():
        page = GatedPage(hold_submit=True)  # submit never returns
        a = make(page)
        chat = asyncio.create_task(a.chat(req("hi", "f1")))
        await page.submit_started.wait()
        started = asyncio.get_running_loop().time()
        outcome = await a.shutdown(grace_s=0.05, margin_s=0.05)
        took = asyncio.get_running_loop().time() - started
        health = await a.health()
        refused = await a.chat(req("more", "f2"))
        page.release_submit.set()  # let the stuck exchange end so the loop closes cleanly
        await chat
        return page, a, outcome, took, health, refused

    page, a, outcome, took, health, refused = go(scenario())
    assert took < 2.0
    assert outcome["done"] is False and outcome["in_flight"] is True and "in flight" in outcome["reason"]
    assert outcome["chrome"]["closed"] is False and page.close_calls == 0
    assert a.state == "shutting_down" and health["state"] == "shutting_down"
    assert refused.error_class is ErrorClass.SHUTTING_DOWN


def test_F2_http_shutdown_that_times_out_does_not_exit_the_process(monkeypatch):
    exits = []
    monkeypatch.setattr(server, "_exit", lambda: exits.append(True))

    async def scenario():
        page = GatedPage(hold_submit=True)
        a = ChromeGeminiAdapter(page, poll_s=0.001, queue_wait_s=5)
        async with TestClient(TestServer(server.build_app(a, TOKEN, grace_s=0.05))) as client:
            chat = asyncio.create_task(a.chat(req("hi", "f3")))
            await page.submit_started.wait()
            reply = await client.post("/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
            body = await reply.json()
            await asyncio.sleep(0.4)
            page.release_submit.set()
            await chat
            return body

    body = go(scenario())
    assert body["shutdown"] is False and body["in_flight"] is True and body["pid"] > 0
    assert exits == []  # the adapter did not claim to have stopped


# G -------------------------------------------------------------------------------------------
def test_G_external_chrome_is_not_closed_by_shutdown(tmp_path, monkeypatch):
    from lga_worker.browser import chrome as chrome_mod
    from chrome_gemini.cdp_page import CdpGeminiPage

    class Chrome:
        pid, cdp_url, launched_at = 777, "http://127.0.0.1:1", 1.0

        def is_alive(self):
            return True

    closed = []

    async def never(cdp_url, timeout_s):
        closed.append(cdp_url)
        return True

    external = Chrome()
    monkeypatch.setattr(chrome_mod, "find_running", lambda profile: external)
    monkeypatch.setattr(chrome_mod, "close_gracefully", never)
    page = CdpGeminiPage(None, tmp_path / "profile", tmp_path / "logs", tmp_path / "selectors.json")

    async def scenario():
        await page._ensure_chrome()
        return await make(page).shutdown(grace_s=0.1)

    outcome = go(scenario())
    assert outcome["done"] is True  # the adapter stopped...
    assert outcome["chrome"]["closed"] is False and "external" in outcome["chrome"]["reason"]  # ...Chrome did not
    assert closed == []


# H -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("header,configured", [
    (None, TOKEN), ("Bearer wrong", TOKEN), (f"Basic {TOKEN}", TOKEN), (f"Bearer {TOKEN}", ""),  # last: no token set
])
def test_H_shutdown_without_the_right_token_is_refused_and_changes_nothing(header, configured, monkeypatch):
    exits = []
    monkeypatch.setattr(server, "_exit", lambda: exits.append(True))

    async def scenario():
        page = GatedPage()
        a = make(page)
        async with TestClient(TestServer(server.build_app(a, configured, grace_s=0.1, chat_token="chat-tok"))) as client:
            headers = {"Authorization": header} if header else {}
            reply = await client.post("/shutdown", headers=headers)
            text = await reply.text()
            chat = await (await client.post("/v1/chat", headers={"Authorization": "Bearer chat-tok"},
                                            json={"prompt": "still works", "transaction_id": "h1", "timeout_s": 1})).json()
            return reply.status, text, a.state, page.close_calls, chat

    status, text, state, closes, chat = go(scenario())
    assert status == 403 and TOKEN not in text
    assert state == "running" and closes == 0 and exits == []
    assert chat["error"] is None or chat["error"]["class"] != "shutting_down"


def test_H_correct_token_shuts_down_once_and_never_echoes_the_token(monkeypatch):
    exits = []
    monkeypatch.setattr(server, "_exit", lambda: exits.append(True))

    async def scenario():
        page = GatedPage()
        a = make(page)
        async with TestClient(TestServer(server.build_app(a, TOKEN, grace_s=0.5))) as client:
            reply = await client.post("/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
            text = await reply.text()
            again = await client.post("/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
            await asyncio.sleep(0.5)
            return reply.status, text, again.status, page.close_calls

    status, text, again_status, closes = go(scenario())
    assert status == 200 and again_status == 200 and TOKEN not in text
    assert closes == 1  # idempotent: the second call repeats the outcome, it does not close twice
    assert exits  # the process ends only after a completed shutdown


def test_token_is_taken_from_the_environment_and_removed(monkeypatch):
    import os

    monkeypatch.setenv(server.TOKEN_ENV, TOKEN)
    assert os.environ.pop(server.TOKEN_ENV) == TOKEN and server.TOKEN_ENV not in os.environ


# ------------------------------------------------------------------ H3: /v1/chat authentication
CHAT = "c4at-tok3n-for-tests-only"


def _serve(page, **tokens):
    a = make(page)
    return a, TestClient(TestServer(server.build_app(a, **tokens)))


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {CHAT}", f"Bearer {TOKEN}", "Bearer "])
def test_chat_without_the_chat_token_is_refused_and_never_reaches_the_page(header):
    async def scenario():
        page = GatedPage()
        a, client = _serve(page, shutdown_token=TOKEN, chat_token=CHAT)
        async with client:
            headers = {"Authorization": header} if header else {}
            reply = await client.post("/v1/chat", headers=headers, json={"prompt": "secret question", "transaction_id": "a1"})
            return reply.status, await reply.text(), page

    status, text, page = go(scenario())
    assert status == 403 and CHAT not in text and TOKEN not in text
    assert page.submits == [] and page.in_new_conversation.is_set() is False  # the page was never touched


def test_chat_with_no_token_configured_is_disabled_not_open():
    async def scenario():
        page = GatedPage()
        _, client = _serve(page, shutdown_token=TOKEN, chat_token="")
        async with client:
            reply = await client.post("/v1/chat", headers={"Authorization": "Bearer "}, json={"prompt": "q", "transaction_id": "a2"})
            return reply.status, page

    status, page = go(scenario())
    assert status == 403 and page.submits == []


def test_chat_token_and_shutdown_token_do_not_authorise_each_other(monkeypatch):
    exits = []
    monkeypatch.setattr(server, "_exit", lambda: exits.append(True))

    async def scenario():
        page = GatedPage()
        a, client = _serve(page, shutdown_token=TOKEN, chat_token=CHAT, grace_s=0.1)
        async with client:
            stop_with_chat = await client.post("/shutdown", headers={"Authorization": f"Bearer {CHAT}"})
            chat_with_stop = await client.post("/v1/chat", headers={"Authorization": f"Bearer {TOKEN}"},
                                               json={"prompt": "q", "transaction_id": "a3"})
            return stop_with_chat.status, chat_with_stop.status, a.state

    stop_status, chat_status, state = go(scenario())
    assert (stop_status, chat_status) == (403, 403) and state == "running" and exits == []


def test_chat_with_the_right_token_works_and_health_stays_open():
    async def scenario():
        page = GatedPage()
        page.state = PageState(1, "4", False)
        _, client = _serve(page, shutdown_token=TOKEN, chat_token=CHAT)
        async with client:
            health = await client.get("/health")  # no header: read-only status for the gateway's probe
            reply = await client.post("/v1/chat", headers={"Authorization": f"Bearer {CHAT}"},
                                      json={"prompt": "2+2?", "transaction_id": "a4", "timeout_s": 2})
            return health.status, await reply.json(), page

    health_status, body, page = go(scenario())
    assert health_status == 200 and body["status"] == "COMPLETED" and body["content"] == "4"
    assert CHAT not in str(body) and page.submits == ["2+2?"]


def test_failed_shutdown_replies_503_so_the_gateway_can_act_at_once(monkeypatch):
    monkeypatch.setattr(server, "_exit", lambda: None)

    async def scenario():
        page = GatedPage(hold_submit=True)
        a, client = _serve(page, shutdown_token=TOKEN, chat_token=CHAT, grace_s=0.05)
        async with client:
            chat = asyncio.create_task(client.post("/v1/chat", headers={"Authorization": f"Bearer {CHAT}"},
                                                   json={"prompt": "q", "transaction_id": "a5", "timeout_s": 5}))
            await page.submit_started.wait()
            reply = await client.post("/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
            status, body = reply.status, await reply.json()
            page.release_submit.set()
            await chat
            return status, body

    status, body = go(scenario())
    assert status == 503 and body["shutdown"] is False and body["in_flight"] is True


# ------------------------------------------------------------------ H4A: health observes, it does not launch
def test_health_never_launches_chrome_and_reports_degraded_when_none_runs():
    class Page(GatedPage):
        def __init__(self):
            super().__init__()
            self.peeks = 0
            self.ensures = 0

        async def peek_session(self):
            from chrome_gemini.page import PageError

            self.peeks += 1
            raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "Chrome is not running on the profile (not relaunching)")

        async def ensure_session(self):
            self.ensures += 1  # would start Chrome
            return await super().ensure_session()

    async def scenario():
        page = Page()
        a = make(page)
        return page, await a.health()

    page, health = go(scenario())
    assert health["state"] == "degraded" and "started by the first request" in health["detail"]
    assert page.peeks == 1 and page.ensures == 0


# ------------------------------------------------------------------ H4B gates: exact model, pre-send check
def _model_page(label):
    class P(GatedPage):
        async def read_model(self):
            return ModelReading(label, "verified" if label else "unknown")
    return P()


@pytest.mark.parametrize("shown", ["Gemini 3.5 Flash-Lite", "Gemini 3.1 Pro", "gemini pro", None])
def test_required_model_must_match_exactly_or_nothing_is_sent(shown):
    async def scenario():
        page = _model_page(shown)
        a = ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, require_model="Gemini Pro")
        return page, await a.chat(req("2 + 2?", "m1"))

    page, result = go(scenario())
    assert result.status is TransactionStatus.NOT_SUBMITTED and result.error_class is ErrorClass.MODEL_MISMATCH
    assert page.submits == [] and result.model_verified == shown


def test_required_model_present_sends_once_and_records_the_timings():
    async def scenario():
        page = _model_page("Gemini Pro")
        page.state = PageState(1, "4", False)
        a = ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, require_model="Gemini Pro")
        return page, await a.chat(req("2 + 2?", "m2"))

    page, result = go(scenario())
    assert result.status is TransactionStatus.COMPLETED and page.submits == ["2 + 2?"]
    s = result.signals
    assert s["submit_calls"] == 1 and s["positive_done_signal"] is True
    assert s["submitted_at"] <= s["first_confirmed_at"] <= s["completed_at"]


def test_driver_refuses_to_click_when_composer_or_send_button_is_not_exactly_right(tmp_path):
    from chrome_gemini.cdp_page import CdpGeminiPage
    from chrome_gemini.page import PageError

    VERIFIED = {"verified": True, "input": "i", "send": "s", "answer": "a", "generating": "g", "login_marker": "l"}
    for check in ({"text": "something else", "buttons": 1, "enabled": 1}, {"text": "2 + 2?", "buttons": 2, "enabled": 2},
                  {"text": "2 + 2?", "buttons": 1, "enabled": 0}, {"text": "2 + 2?", "buttons": 0, "enabled": 0}):
        page = CdpGeminiPage(None, tmp_path / "p", tmp_path / "l", tmp_path / "s.json")
        page._selectors = dict(VERIFIED)
        clicks = []

        async def fake_eval(expression, **kw):
            if "e.focus()" in expression:
                return True
            if "click()" in expression:
                clicks.append(True)
                return True
            return check

        async def fake_call(method, params=None, **kw):
            return {}

        page._eval, page._call = fake_eval, fake_call
        with pytest.raises(PageError) as caught:
            go(page.submit("2 + 2?"))
        assert caught.value.error_class is ErrorClass.SUBMIT_FAILED and caught.value.submitted_possible is False
        assert clicks == []  # never clicked


# ------------------------------------------------------------------ last observed state / pending signal
def _signals_after(states, timeout_s=0.1, **kw):
    class P(GatedPage):
        def __init__(self):
            super().__init__()
            self.script = list(states)

        async def read_state(self):
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    async def scenario():
        page = P()
        a = ChromeGeminiAdapter(page, poll_s=0.001, stable_polls=2, **kw)
        return page, await a.chat(req("q", "o1", timeout_s=timeout_s))

    return go(scenario())


def test_pending_page_is_observed_as_generating_but_never_completes():
    page, result = _signals_after([PageState(0, "", None, pending=True)])
    assert result.status is TransactionStatus.GENERATING  # pending is UI evidence that the prompt was accepted
    assert result.error_class is ErrorClass.INCOMPLETE and result.content == ""
    assert result.signals["last_observed_state"] == "GENERATING" and "completed_at" not in result.signals
    assert page.submits == ["q"] and result.signals["submit_calls"] == 1


def test_no_signal_at_all_leaves_the_observation_at_submitted():
    _, result = _signals_after([PageState(0, "", False)])
    assert result.status is TransactionStatus.SUBMISSION_UNCERTAIN
    assert result.signals["last_observed_state"] == "SUBMITTED"


def test_completed_is_observed_as_completed_with_a_timestamp():
    _, result = _signals_after([PageState(1, "4", False)], timeout_s=2)
    assert result.status is TransactionStatus.COMPLETED
    assert result.signals["last_observed_state"] == "COMPLETED" and result.signals["last_observed_at"] > 0


def test_page_error_is_observed_as_failed():
    _, result = _signals_after([PageState(1, "", False, error="boom")], timeout_s=2)
    assert result.status is TransactionStatus.FAILED and result.signals["last_observed_state"] == "FAILED"


def test_read_failure_marks_an_observation_gap():
    from chrome_gemini.page import PageError

    class P(GatedPage):
        async def read_state(self):
            raise PageError(ErrorClass.CONNECTION_LOST, "gone", True)

    async def scenario():
        a = ChromeGeminiAdapter(P(), poll_s=0.001, stable_polls=2)
        return await a.chat(req("q", "o2", timeout_s=1))

    result = go(scenario())
    assert result.signals["observation_gap"] is True and result.signals["last_observed_state"] == "SUBMITTED"


def test_driver_reports_pending_from_the_page(tmp_path):
    from chrome_gemini.cdp_page import CdpGeminiPage

    page = CdpGeminiPage(None, tmp_path / "p", tmp_path / "l", tmp_path / "s.json")
    page._selectors = {"input": "i", "send": "s", "answer": "a", "generating": "g", "login_marker": "l",
                       "pending": "pending-response"}
    seen = []

    async def fake_eval(expression, **kw):
        seen.append(expression)
        return {"pending": True, "turns": 0, "text": "", "generating": False, "error": None}

    page._eval = fake_eval
    state = go(page.read_state())
    assert state.pending is True and "pending-response" in seen[0]


# ------------------------------------------------------------------ late answers (after the transaction ended)
class LatePage(GatedPage):
    """read_state returns `self.state` plus the user turn the real page shows for the last prompt sent.
    Set `user` to (turns, text) to show something else."""

    user = "same"

    async def read_state(self):
        import dataclasses

        if self.user == "same":
            turns, text = 1, f"Bạn đã nói {self.submits[-1] if self.submits else ''}"
        else:
            turns, text = self.user
        return dataclasses.replace(self.state, user_turns=turns, user_text=text)


def test_late_answer_is_recorded_beside_the_terminal_result_not_over_it():
    async def scenario():
        page = LatePage()
        a = make(page)
        page.state = PageState(0, "", None, pending=True)
        ended = await a.chat(req("q", "late-1", timeout_s=0.05))
        terminal = (ended.status, ended.error_class, ended.finished_at)
        waiting_obs = (await a.reconcile("late-1")).late_observation  # nothing yet
        page.state = PageState(1, "Tôi không thể giúp", False)  # Gemini answers, after the deadline
        reconciled = await a.reconcile("late-1")
        again = await a.chat(req("q", "late-1"))  # the same id afterwards
        return page, ended, terminal, waiting_obs, reconciled, again

    page, ended, terminal, waiting_obs, reconciled, again = go(scenario())
    assert terminal[0] is TransactionStatus.GENERATING and terminal[1] is ErrorClass.INCOMPLETE
    assert waiting_obs is None
    late = reconciled.late_observation
    assert late["state"] == "COMPLETED_LATE" and late["content"] == "Tôi không thể giúp" and late["seconds_after_terminal"] >= 0
    # the terminal outcome is exactly what was reported to the caller
    assert (reconciled.status, reconciled.error_class, reconciled.finished_at) == terminal
    assert reconciled.content == "" and reconciled.status is not TransactionStatus.COMPLETED
    assert again is reconciled and page.submits == ["q"]  # no new transaction, no retry, the page was not touched


def test_a_new_chat_first_notes_a_late_answer_then_proceeds_without_overwriting_it():
    async def scenario():
        page = LatePage()
        a = make(page)
        page.state = PageState(0, "", None, pending=True)
        first = await a.chat(req("first", "t1", timeout_s=0.05))
        page.state = PageState(1, "late answer", False)  # arrives before anyone asked
        second = await a.chat(req("second", "t2", timeout_s=2))  # navigates away from the old conversation
        return page, first, second, a.manager.get("t1")

    page, first, second, stored = go(scenario())
    assert stored is first and first.late_observation["content"] == "late answer"
    assert first.status is TransactionStatus.GENERATING
    assert second.status is TransactionStatus.COMPLETED and second.transaction_id == "t2"
    assert page.submits == ["first", "second"]  # the late answer was recorded BEFORE the next exchange touched the page


def test_ambiguous_page_states_are_never_recorded_as_a_late_answer():
    async def scenario():
        page = LatePage()
        a = make(page)
        page.state = PageState(0, "", None, pending=True)
        await a.chat(req("q", "amb-1", timeout_s=0.05))
        results = []
        for state in (PageState(1, "text", None), PageState(2, "text", False), PageState(1, "", False),
                      PageState(1, "text", False, error="boom"), PageState(1, "text", False, pending=True),
                      PageState(1, "text", True)):
            page.state = state
            record = await a.reconcile("amb-1")
            results.append(record.late_observation)
        return results

    assert go(scenario()) == [None] * 6


def test_reconcile_never_runs_beside_an_exchange_in_flight():
    async def scenario():
        page = LatePage(hold_submit=True)
        a = make(page)
        chat = asyncio.create_task(a.chat(req("q", "busy-1", timeout_s=5)))
        await page.submit_started.wait()
        record = await a.reconcile("busy-1")  # exchange holds the lock: just the record
        page.release_submit.set()
        page.state = PageState(1, "4", False)
        await chat
        return record

    record = go(scenario())
    assert record is not None and record.late_observation is None


def test_unknown_transaction_and_a_completed_one_are_left_alone():
    async def scenario():
        page = LatePage()
        a = make(page)
        page.state = PageState(1, "4", False)
        done_ = await a.chat(req("q", "done-1", timeout_s=2))
        page.state = PageState(1, "something else entirely", False)
        return await a.reconcile("nope"), await a.reconcile("done-1"), done_

    unknown, reconciled, done_ = go(scenario())
    assert unknown is None and reconciled is done_ and done_.late_observation is None and done_.content == "4"


def test_http_transaction_lookup_is_authenticated_and_read_only(monkeypatch):
    async def scenario():
        page = LatePage()
        a = make(page)
        async with TestClient(TestServer(server.build_app(a, TOKEN, chat_token=CHAT))) as client:
            page.state = PageState(0, "", None, pending=True)
            await a.chat(req("q", "http-1", timeout_s=0.05))
            page.state = PageState(1, "late", False)
            no_auth = await client.get("/v1/transactions/http-1")
            wrong = await client.get("/v1/transactions/http-1", headers={"Authorization": f"Bearer {TOKEN}"})
            ok = await client.get("/v1/transactions/http-1", headers={"Authorization": f"Bearer {CHAT}"})
            body = await ok.json()
            missing = await client.get("/v1/transactions/other", headers={"Authorization": f"Bearer {CHAT}"})
            return page, no_auth.status, wrong.status, ok.status, body, missing.status

    page, no_auth, wrong, ok, body, missing = go(scenario())
    assert (no_auth, wrong, ok, missing) == (403, 403, 200, 404)
    assert body["status"] == "GENERATING" and body["late_observation"]["content"] == "late"
    assert page.submits == ["q"]


def test_a_late_answer_is_not_attributed_when_the_prompt_cannot_be_matched():
    cases = [(1, "Bạn đã nói something completely different"), (2, "Bạn đã nói q"), (0, ""), (None, None)]

    async def scenario():
        out = []
        for turns_text in cases:
            page = LatePage()
            a = make(page)
            page.state = PageState(0, "", None, pending=True)
            await a.chat(req("q", "attr-1", timeout_s=0.05))
            page.state = PageState(1, "an answer", False)
            page.user = turns_text
            record = await a.reconcile("attr-1")
            out.append(record.late_observation)
        return out

    assert go(scenario()) == [None] * 4  # a person typed in the tab, or the prompt cannot be seen: nothing is claimed


def test_late_observation_is_marked_unofficial_and_reconcile_never_navigates_or_sends():
    async def scenario():
        page = LatePage()
        a = make(page)
        page.state = PageState(0, "", None, pending=True)
        ended = await a.chat(req("q", "official-1", timeout_s=0.05))
        navigations_before, submits_before = page.in_new_conversation.is_set(), list(page.submits)
        navigations_before = False  # the exchange itself navigated once; what matters is what reconcile adds
        page.in_new_conversation.clear()
        page.state = PageState(1, "late", False)
        record = await a.reconcile("official-1")
        await a.reconcile("official-1")
        await a.reconcile("official-1")
        return page, ended, record, submits_before

    page, ended, record, submits_before = go(scenario())
    late = record.late_observation
    assert late["official"] is False and "not part of its result" in late["note"]
    assert record.to_dict()["status"] == "GENERATING" and record.to_dict()["content"] == ""
    assert page.submits == submits_before  # reconcile never sent anything
    assert page.in_new_conversation.is_set() is False  # ...and never navigated to a new conversation
