"""The adapter: turns one provider request into one recorded browser transaction.

Rules it enforces (see docs/provider-contract.md):
  * the prompt is sent at most once per transaction id; after the point of no return the status is
    SUBMISSION_UNCERTAIN until the page confirms it, and no path here re-sends;
  * the whole exchange runs under one lock;
  * every exchange starts in an empty conversation and is refused if the page still shows an answer;
  * COMPLETED needs the answer present, the page reporting "not generating", and the same text on
    `stable_polls` consecutive reads. Without an explicit "not generating" signal it stays GENERATING.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .contract import (
    Capabilities,
    ChatRequest,
    ErrorClass,
    TransactionResult,
    TransactionStatus,
)
from .page import GeminiPage, PageError, PageState, SessionInfo
from .transaction import TransactionManager

logger = logging.getLogger(__name__)

CAPABILITIES = Capabilities(text=True, images=False, files=False, streaming=False)


class ChromeGeminiAdapter:
    def __init__(
        self,
        page: GeminiPage,
        manager: TransactionManager | None = None,
        *,
        queue_wait_s: float = 2.0,
        poll_s: float = 0.5,
        stable_polls: int = 3,
        require_model: str | None = None,
    ) -> None:
        self.page = page
        self.manager = manager or TransactionManager()
        self.queue_wait_s = queue_wait_s
        self.poll_s = poll_s
        self.stable_polls = max(1, stable_polls)
        # When set, a prompt is only sent if the page's model label equals this EXACTLY (never inferred).
        self.require_model = require_model
        # Lifecycle: running -> shutting_down -> closed. Only ever moves forward.
        self.state = "running"
        self._stop_at: float | None = None  # loop time after which an in-flight exchange is cut short
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_outcome: dict[str, Any] | None = None
        # The latest transaction that ended without an answer after its prompt was sent. Its conversation may
        # still complete on the page; the next exchange would navigate away from it, so it is looked at first.
        self._watch: str | None = None
        self._watch_prompt: str | None = None
        self._current_prompt: str | None = None

    # ---------------------------------------------------------------- shutdown
    async def shutdown(self, grace_s: float = 10.0, margin_s: float = 3.0) -> dict[str, Any]:
        """Stop taking work, let the exchange in flight finish within `grace_s`, then close owned Chrome.

        1. New chats are refused from this moment (nothing is queued).
        2. An exchange in flight gets `grace_s` to complete; if it cannot, it ends as GENERATING /
           SUBMISSION_UNCERTAIN (`shutdown_interrupted`) and its caller receives that. It is never re-sent.
        3. Only when no exchange holds the lock does Chrome get closed, and only an owned one.
        4. When the exchange cannot be brought to an end (a page call that hangs), nothing is closed and the
           outcome says so: `done` is False and the adapter must not be reported as stopped.

        Returns {"done": bool, "in_flight": bool, "chrome": {...}, "state": ...}. Idempotent.
        """
        async with self._shutdown_lock:  # a second /shutdown waits for, then repeats, the first one's outcome
            if self.state == "closed" and self._shutdown_outcome is not None:
                return self._shutdown_outcome
            loop = asyncio.get_running_loop()
            self.state = "shutting_down"
            if self._stop_at is None:
                self._stop_at = loop.time() + max(0.0, grace_s)
            async with self.manager.exclusive(max(0.0, grace_s) + margin_s) as quiet:
                if not quiet:
                    return {"done": False, "in_flight": True, "state": self.state,
                            "reason": f"a transaction was still in flight after {grace_s + margin_s:.0f}s; "
                                      "nothing was closed",
                            "chrome": {"closed": False, "reason": "transaction in flight"}}
                chrome = await self._close_owned()
            self.state = "closed"
            self._shutdown_outcome = {"done": True, "in_flight": False, "state": self.state, "chrome": chrome}
            return self._shutdown_outcome

    async def _close_owned(self) -> dict[str, Any]:
        close = getattr(self.page, "close_owned", None)
        if close is None:
            return {"closed": False, "reason": "page has no owned browser"}
        try:
            return await close()
        except Exception as exc:  # noqa: BLE001 - shutdown must still finish
            logger.warning("closing Chrome failed: %s", exc)
            return {"closed": False, "reason": f"error: {exc}"}

    # ------------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        """Read-only. Never sends a prompt and never touches an exchange in progress."""
        base: dict[str, Any] = {"provider": "chrome_gemini", "capabilities": CAPABILITIES.to_dict(),
                                "busy": self.manager.busy()}
        if self.state != "running":
            return {**base, "state": self.state, "detail": "the adapter is stopping"}
        if self.manager.busy():
            return {**base, "state": "ready", "detail": "a transaction is in progress"}
        # A health probe observes; it does not start Chrome (a window popping up every probe interval, or
        # coming back after someone closed it, is a side effect). The first request starts it on demand.
        look = getattr(self.page, "peek_session", None) or self.page.ensure_session
        try:
            session = await look()
        except PageError as exc:
            if exc.error_class is ErrorClass.BROWSER_UNAVAILABLE:
                # Not a failure of the adapter: nothing is running yet / any more. Degraded keeps the gateway's
                # breaker closed so the next request can start the browser.
                return {**base, "state": "degraded", "detail": f"{exc}; it is started by the first request",
                        "error_class": exc.error_class.value}
            state = "degraded" if exc.error_class in (ErrorClass.NOT_LOGGED_IN, ErrorClass.SELECTORS_UNVERIFIED) else "unavailable"
            return {**base, "state": state, "detail": str(exc), "error_class": exc.error_class.value}
        if not session.logged_in:
            return {**base, "state": "degraded", "detail": "Gemini is not signed in", "error_class": ErrorClass.NOT_LOGGED_IN.value}
        return {**base, "state": "ready", "detail": "signed in", "model_verified": session.model,
                "model_status": session.model_status}

    # -------------------------------------------------------------------- chat
    async def chat(self, request: ChatRequest) -> TransactionResult:
        tid = request.transaction_id or self.manager.new_id()
        if (earlier := self.manager.recorded(tid, request.prompt)) is not None:
            return earlier
        if self.state != "running":
            return self._refuse(tid, ErrorClass.SHUTTING_DOWN, "the adapter is shutting down; nothing was sent")
        if request.attachments:
            return self._refuse(tid, ErrorClass.UNSUPPORTED, "this provider cannot take images or files; none were sent")
        if not request.prompt.strip():
            return self._refuse(tid, ErrorClass.UNSUPPORTED, "empty prompt")
        async with self.manager.exclusive(self.queue_wait_s) as acquired:
            if not acquired:
                return self._refuse(tid, ErrorClass.BUSY, "another transaction owns the browser")
            if (earlier := self.manager.recorded(tid, request.prompt)) is not None:
                return earlier  # an identical call finished while this one waited for the lock
            if self.state != "running":  # shutdown began while this one was queued behind the lock
                return self._refuse(tid, ErrorClass.SHUTTING_DOWN, "the adapter is shutting down; nothing was sent")
            await self._reconcile_locked()  # an answer that arrived late must be noted before we navigate away
            return await self._exchange(tid, request)

    async def _exchange(self, tid: str, request: ChatRequest) -> TransactionResult:
        started = self.manager.now()
        result = TransactionResult(transaction_id=tid, status=TransactionStatus.NOT_SUBMITTED, started_at=started)
        try:
            try:
                session: SessionInfo = await self.page.ensure_session()
                if not session.logged_in:
                    return self._finish(result, TransactionStatus.NOT_SUBMITTED, ErrorClass.NOT_LOGGED_IN, "Gemini is not signed in")
                if await self.page.new_conversation() != 0:
                    return self._finish(result, TransactionStatus.NOT_SUBMITTED, ErrorClass.CONVERSATION_NOT_CLEAN,
                                        "the page still shows an earlier answer; refusing to mix conversations")
                # Read after the navigation: the label that matters is the one of the conversation we use.
                reading = await self.page.read_model()
                result.model_verified = reading.label
                result.signals["model_status"] = reading.status
                if self.require_model is not None and reading.label != self.require_model:
                    return self._finish(result, TransactionStatus.NOT_SUBMITTED, ErrorClass.MODEL_MISMATCH,
                                        f"the page shows {reading.label!r} (status {reading.status}); "
                                        f"{self.require_model!r} is required; nothing was sent")
            except PageError as exc:
                return self._finish(result, TransactionStatus.NOT_SUBMITTED, exc.error_class, str(exc))

            if self.state != "running":  # no await since the check: shutdown began during the page preparation
                return self._finish(result, TransactionStatus.NOT_SUBMITTED, ErrorClass.SHUTTING_DOWN,
                                    "the adapter is shutting down; nothing was sent")
            # Point of no return: from here the prompt may reach Gemini even if a call below raises.
            result.status = TransactionStatus.SUBMISSION_UNCERTAIN
            self.manager.record(request.prompt, result)
            result.signals["submit_calls"] = 1
            self._current_prompt = request.prompt
            self._observe(result, "UNKNOWN")  # the click is about to happen; until the page confirms, it is not known
            try:
                await self.page.submit(request.prompt)
                result.signals["submitted_at"] = self.manager.now()
                self._observe(result, "SUBMITTED")
            except PageError as exc:
                if not exc.submitted_possible:
                    result.status = TransactionStatus.NOT_SUBMITTED
                    self.manager.forget(tid)
                    return self._finish(result, TransactionStatus.NOT_SUBMITTED, exc.error_class, str(exc))
                return self._finish(result, TransactionStatus.SUBMISSION_UNCERTAIN, exc.error_class, str(exc))
            return await self._await_answer(result, request.timeout_s)
        except asyncio.CancelledError:
            # The caller went away mid-exchange: keep the honest status and never resubmit.
            result.finished_at = self.manager.now()
            if result.error_class is ErrorClass.NONE and result.status is not TransactionStatus.NOT_SUBMITTED:
                result.error_class = ErrorClass.CONNECTION_LOST
                result.error_message = "caller cancelled while the transaction was in flight"
            raise

    async def _await_answer(self, result: TransactionResult, timeout_s: float) -> TransactionResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        confirmed = False  # the page showed evidence that this prompt was accepted
        last_text, stable = None, 0
        state: PageState | None = None
        while True:
            try:
                state = await self.page.read_state()
            except PageError as exc:
                status = TransactionStatus.GENERATING if confirmed else TransactionStatus.SUBMISSION_UNCERTAIN
                result.signals["observation_gap"] = True  # what the page shows after this point is unknown
                return self._finish(result, status, ErrorClass.CONNECTION_LOST, str(exc))
            if state.turns > 1:
                return self._finish(result, TransactionStatus.FAILED, ErrorClass.CONVERSATION_NOT_CLEAN,
                                    f"{state.turns} answers in a conversation that should hold one")
            if state.pending:
                # UI evidence that the prompt was accepted and the page waits for the answer. It lifts the
                # observation to GENERATING; it can never produce COMPLETED.
                confirmed = True
                self._observe(result, "GENERATING")
            if state.generating or state.turns == 1:
                confirmed = True
                result.status = TransactionStatus.GENERATING
                self._observe(result, "GENERATING")
                if state.generating:
                    result.signals.setdefault("first_generating_at", self.manager.now())
                result.signals.setdefault("first_confirmed_at", self.manager.now())
            if state.error:
                self._observe(result, "FAILED")
                status = TransactionStatus.FAILED if confirmed else TransactionStatus.SUBMISSION_UNCERTAIN
                return self._finish(result, status, ErrorClass.PAGE_ERROR, state.error[:300])
            if confirmed and state.turns == 1 and state.text and state.generating is False:
                stable = stable + 1 if state.text == last_text else 1
                last_text = state.text
                if stable >= self.stable_polls:
                    result.content = state.text
                    self._observe(result, "COMPLETED")
                    result.signals.update({"turns": 1, "generating": False, "stable_polls": stable,
                                           "completed_at": self.manager.now(), "positive_done_signal": True})
                    return self._finish(result, TransactionStatus.COMPLETED, ErrorClass.NONE, "")
            else:
                last_text, stable = None, 0
            now = loop.time()
            interrupted = self._stop_at is not None and now >= self._stop_at
            if now >= deadline or interrupted:
                status = TransactionStatus.GENERATING if confirmed else TransactionStatus.SUBMISSION_UNCERTAIN
                result.signals.update({"partial_chars": len(state.text), "generating": state.generating, "turns": state.turns})
                if interrupted and now < deadline:
                    return self._finish(result, status, ErrorClass.SHUTDOWN_INTERRUPTED,
                                        "adapter shutdown ended the wait before the answer was complete; not re-sent")
                return self._finish(result, status, ErrorClass.INCOMPLETE, f"no completed answer within {timeout_s:.0f}s")
            await asyncio.sleep(self.poll_s)

    # ----------------------------------------------------------------- helpers
    def _observe(self, result: TransactionResult, state: str) -> None:
        """Record what the PAGE last showed (NOT_SUBMITTED / SUBMITTED / GENERATING / COMPLETED / FAILED / UNKNOWN).

        This is an observation with a timestamp, not the transaction status: the status keeps its meaning and
        its anti-resend rules; the gateway reads this to say what was last seen when it must still report
        `submission_uncertain`.
        """
        result.signals["last_observed_state"] = state
        result.signals["last_observed_at"] = self.manager.now()

    def _refuse(self, tid: str, error_class: ErrorClass, message: str) -> TransactionResult:
        now = self.manager.now()
        return TransactionResult(transaction_id=tid, status=TransactionStatus.NOT_SUBMITTED, error_class=error_class,
                                 error_message=message, started_at=now, finished_at=now)

    # ------------------------------------------------------------ late arrivals
    async def reconcile(self, tid: str) -> TransactionResult | None:
        """The recorded result of `tid`, after one read-only look for a late answer. Never sends, never navigates,
        never waits for an exchange in flight (then it just returns the record)."""
        record = self.manager.get(tid)
        if record is None:
            return None
        if self._watch != tid:
            return record
        async with self.manager.exclusive(0.05) as quiet:
            if quiet and self._watch == tid:
                await self._reconcile_locked()
        return self.manager.get(tid)

    async def _reconcile_locked(self) -> None:
        """Called with the exchange lock held. One read of the page; records the late answer, changes nothing else."""
        tid = self._watch
        if tid is None:
            return
        record = self.manager.get(tid)
        if record is None or record.late_observation is not None:
            self._watch = None
            return
        try:
            state = await self.page.read_state()
        except PageError:
            return  # cannot look right now; keep watching
        if state.error or state.pending or state.generating is not False or state.turns != 1 or not state.text:
            return  # still generating, ambiguous, or nothing to read: not recorded
        # Attribution: the page must show exactly one user turn and it must be THIS transaction's prompt. When that
        # cannot be shown (no user_query selector, another or an extra turn) nothing is recorded.
        wanted = " ".join((self._watch_prompt or "").split())
        shown = " ".join((state.user_text or "").split())
        if state.user_turns != 1 or not wanted or wanted not in shown:
            return
        now = self.manager.now()
        record.late_observation = {
            "official": False,  # supplementary evidence; the transaction's reported outcome is unchanged
            "note": "arrived after the transaction ended; not part of its result",
            "state": "COMPLETED_LATE",
            "observed_at": now,
            "seconds_after_terminal": round(now - record.finished_at, 3),
            "content": state.text,
            "attribution": "the page held one user turn whose text contains this transaction's prompt",
        }
        self.manager.update(record)
        logger.info("late answer observed for transaction %s (%d chars, %.1fs after it ended)",
                    tid, len(state.text), record.late_observation["seconds_after_terminal"])
        self._watch = None

    def _finish(self, result: TransactionResult, status: TransactionStatus, error_class: ErrorClass, message: str) -> TransactionResult:
        if status in (TransactionStatus.GENERATING, TransactionStatus.SUBMISSION_UNCERTAIN) and result.signals.get("submit_calls"):
            self._watch = result.transaction_id  # sent, no answer: it may still arrive
            self._watch_prompt = self._current_prompt
        elif self._watch == result.transaction_id:
            self._watch = None
        result.status = status
        result.error_class = error_class
        result.error_message = message
        result.finished_at = self.manager.now()
        self.manager.update(result)
        return result
