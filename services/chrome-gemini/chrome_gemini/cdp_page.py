"""The real Gemini page driver: the dedicated Chrome profile, driven over CDP.

Chrome itself is started, found and killed by lga_worker.browser.chrome (the one Chrome launcher of
this repository); this module only owns the profile `runtime/gemini-profile` through it, so the
Python worker (profile runtime/chrome-profile) and this service never drive the same browser.

Selectors are NOT built in: Gemini's markup changes and none has been verified against the live page
by this repository. They come from a JSON file whose `"verified": true` flag a human sets after
checking each one. Without it every operation fails with SELECTORS_UNVERIFIED before Chrome is even
started, so no prompt can be sent by accident.

Nothing here reads cookies, storage or tokens: the page is only asked for the visible state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from . import ownership
from .contract import ErrorClass
from .page import MODEL_PENDING, MODEL_UNKNOWN, MODEL_VERIFIED, ModelReading, PageError, PageState, SessionInfo

logger = logging.getLogger(__name__)

REQUIRED_SELECTORS = ("input", "send", "answer", "generating", "login_marker")
# answer_done: matches ONLY once the answer is complete (a positive signal). When configured, the driver
# reports generating=False only on that match and None (unknown) otherwise, so a generating selector that
# silently stops matching can never make a half-written answer look finished.
# pending: matches while the page waits for the answer (not a completion signal).
OPTIONAL_SELECTORS = ("model_label", "error_banner", "answer_done", "pending", "user_query")
GEMINI_URL = "https://gemini.google.com/app"


def validate_gemini_url(url: str) -> str:
    """Only Gemini itself, or a loopback http page (the integration tests' stand-in), may be driven."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname == "gemini.google.com":
        return url
    if parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port:
        return url
    raise ValueError(f"refusing to drive {url!r}: only https://gemini.google.com or http://127.0.0.1:<port>")


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
# How long the model label may take to render after a page load, and how often it is polled.
MODEL_WAIT_S = 6.0
MODEL_POLL_S = 0.25
# One bounded attempt to re-attach to a Chrome whose CDP socket dropped (reads only, never relaunches).
RECONNECT_TIMEOUT_S = 10.0


def load_selectors(path: Path) -> dict[str, str]:
    """The verified selector table, or PageError(SELECTORS_UNVERIFIED)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PageError(ErrorClass.SELECTORS_UNVERIFIED, f"selector file {path} is missing or unreadable: {exc}") from exc
    if data.get("verified") is not True:
        raise PageError(ErrorClass.SELECTORS_UNVERIFIED, f"selectors in {path} are not marked \"verified\": true")
    missing = [k for k in REQUIRED_SELECTORS if not isinstance(data.get(k), str) or not data[k].strip()]
    if missing:
        raise PageError(ErrorClass.SELECTORS_UNVERIFIED, f"selector file lacks: {', '.join(missing)}")
    return {k: data[k] for k in (*REQUIRED_SELECTORS, *OPTIONAL_SELECTORS) if isinstance(data.get(k), str)}


async def _drop(socket: Any) -> None:
    """Close a CDP socket without waiting on a browser that may be frozen.

    websockets' close handshake waits up to 10 s for the peer; a hung Chrome would add that to every
    shutdown or reconnect. One second, then the transport is cut.
    """
    if socket is None:
        return
    try:
        await asyncio.wait_for(socket.close(), timeout=1.0)
    except Exception:  # noqa: BLE001 - timeout, or the tab is already gone
        transport = getattr(socket, "transport", None)
        if transport is not None:
            transport.abort()


class CdpGeminiPage:
    def __init__(self, chrome_path: Path | None, profile_dir: Path, log_dir: Path, selectors_path: Path,
                 launch_timeout_s: float = 30.0, gemini_url: str = GEMINI_URL) -> None:
        self._chrome_path = chrome_path
        self._profile_dir = profile_dir
        self._log_dir = log_dir
        self._selectors_path = selectors_path
        self._launch_timeout_s = launch_timeout_s
        self._gemini_url = validate_gemini_url(gemini_url)
        self._origin = _origin(self._gemini_url)
        self._selectors: dict[str, str] = {}
        self._chrome: Any = None
        self._owned = False  # True: this adapter launched the Chrome (or recorded it as its own)
        self._socket: Any = None
        self._next_id = 0

    # ------------------------------------------------------------- plumbing
    async def _ensure_chrome(self, launch: bool = True) -> None:
        from lga_worker.browser import chrome as chrome_mod

        if self._chrome is not None and self._chrome.is_alive():
            return
        self._chrome = await asyncio.to_thread(chrome_mod.find_running, self._profile_dir)
        if self._chrome is not None:
            # Found, not launched: ours only if our own record names this very browser process.
            self._owned = ownership.owns(self._profile_dir, self._chrome.pid)
        else:
            if not launch:
                raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "Chrome is not running on the profile (not relaunching)")
            if self._chrome_path is None or not self._chrome_path.is_file():
                raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "chrome.exe path is not configured")
            try:
                self._chrome = await asyncio.to_thread(
                    chrome_mod.launch, self._chrome_path, self._profile_dir, False, self._launch_timeout_s, self._log_dir)
            except Exception as exc:  # ToolError from the launcher
                raise PageError(ErrorClass.BROWSER_UNAVAILABLE, f"cannot start Chrome: {exc}") from exc
            self._owned = True
            ownership.claim(self._profile_dir, self._chrome.pid, self._chrome.launched_at)
        self._socket = None

    async def _connect(self, launch: bool = True) -> Any:
        from lga_worker.browser import chrome as chrome_mod
        from websockets.asyncio.client import connect

        if self._socket is not None:
            return self._socket
        await self._ensure_chrome(launch)
        try:
            targets = await asyncio.to_thread(chrome_mod.page_targets, self._chrome.cdp_url)
            gemini = [t for t in targets if str(t.get("url", "")).startswith(self._origin)]
            if not gemini and not launch:
                raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "no Gemini tab is open (not opening a new one)")
            if not gemini:
                await asyncio.to_thread(chrome_mod._http, f"{self._chrome.cdp_url}/json/new?{self._gemini_url}", "PUT")
                await asyncio.sleep(2.0)
                targets = await asyncio.to_thread(chrome_mod.page_targets, self._chrome.cdp_url)
                gemini = [t for t in targets if str(t.get("url", "")).startswith(self._origin)]
            if not gemini:
                raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "no Gemini tab could be opened")
            self._socket = await connect(gemini[0]["webSocketDebuggerUrl"], max_size=None)
        except PageError:
            raise
        except (OSError, ValueError) as exc:
            raise PageError(ErrorClass.BROWSER_UNAVAILABLE, f"cannot attach to Chrome: {exc}") from exc
        return self._socket

    async def _call(self, method: str, params: dict[str, Any] | None = None, *, submitted_possible: bool = False,
                    reconnect: bool = True, launch: bool = True) -> Any:
        if self._socket is None and not reconnect:
            # Mid-submit: nothing is re-sent over a new socket; the caller reports the loss.
            raise PageError(ErrorClass.CONNECTION_LOST, f"CDP {method}: connection was lost", submitted_possible)
        socket = await self._connect(launch)
        self._next_id += 1
        call_id = self._next_id
        try:
            await socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if message.get("id") == call_id:
                    break
        except (OSError, ValueError, TimeoutError, Exception) as exc:  # noqa: BLE001 - any transport failure
            self._socket = None
            raise PageError(ErrorClass.CONNECTION_LOST, f"CDP {method} failed: {exc}", submitted_possible) from exc
        if "error" in message:
            raise PageError(ErrorClass.PAGE_ERROR, f"CDP {method}: {message['error'].get('message', '')}", submitted_possible)
        return message.get("result", {})

    async def _eval(self, expression: str, *, submitted_possible: bool = False, reconnect: bool = True,
                    launch: bool = True) -> Any:
        result = await self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                                  submitted_possible=submitted_possible, reconnect=reconnect, launch=launch)
        if "exceptionDetails" in result:
            raise PageError(ErrorClass.PAGE_ERROR, "page script failed", submitted_possible)
        return result.get("result", {}).get("value")

    def _sel(self, key: str) -> str:
        return json.dumps(self._selectors[key])

    async def close_owned(self, timeout_s: float = 8.0) -> dict[str, Any]:
        """Close the Chrome this adapter owns, gracefully (Browser.close, so the profile is flushed).

        External Chrome is left alone. A Chrome that does not exit is reported, not killed: the gateway's job
        still ends it when the gateway exits, and nobody here guesses at process trees.
        """
        from lga_worker.browser import chrome as chrome_mod

        chrome = self._chrome
        if chrome is None or not chrome.is_alive():
            found = await asyncio.to_thread(chrome_mod.find_running, self._profile_dir)
            if found is not None and ownership.owns(self._profile_dir, found.pid):
                chrome, self._owned = found, True
            else:
                chrome = None
        if chrome is None:
            return {"closed": False, "reason": "no chrome running on the profile"}
        if not self._owned:
            return {"closed": False, "reason": "external chrome, left running", "pid": chrome.pid}
        socket, self._socket = self._socket, None
        await _drop(socket)
        # One budget for the whole close: the Browser.close acknowledgement may take at most half of it, and
        # the process then has the rest of it to exit. A frozen browser costs `timeout_s`, not twice that.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        acknowledged = await chrome_mod.close_gracefully(chrome.cdp_url, timeout_s / 2)
        while chrome.is_alive() and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if chrome.is_alive():
            return {"closed": False, "reason": "chrome did not exit after Browser.close", "acknowledged": acknowledged, "pid": chrome.pid}
        ownership.release(self._profile_dir)
        self._chrome, self._owned = None, False
        return {"closed": True, "pid": chrome.pid}

    # ----------------------------------------------------------- GeminiPage
    async def peek_session(self) -> SessionInfo:
        """Like ensure_session, for health checks: attaches to a Chrome that is already running but never
        starts one and never opens a tab. Raises PageError(BROWSER_UNAVAILABLE) when there is nothing to
        attach to; the browser is then started by the first request that needs it."""
        return await self._session(launch=False)

    async def ensure_session(self) -> SessionInfo:
        return await self._session(launch=True)

    async def _session(self, launch: bool) -> SessionInfo:
        self._selectors = load_selectors(self._selectors_path)  # before Chrome: unverified means inert
        await self._connect(launch)
        signed_in = await self._eval(f"!!document.querySelector({self._sel('login_marker')})")
        reading = await self.read_model()
        return SessionInfo(logged_in=bool(signed_in), model=reading.label, model_status=reading.status)

    async def read_model(self, wait_s: float = MODEL_WAIT_S) -> ModelReading:
        """The model label the page shows, polled until `wait_s`; never guessed, never clicked.

        verified: label read. While polling the state is "pending" (page loading / label not rendered);
        unknown: the deadline passed without a label, or no `model_label` selector is configured.
        """
        if "model_label" not in self._selectors:
            return ModelReading(None, MODEL_UNKNOWN)
        script = ("(()=>{const e=document.querySelector(" + self._sel("model_label") + ");"
                  "return {ready:document.readyState,label:e?(e.innerText||''):''}})()")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_s)
        while True:
            try:
                value = await self._eval(script)
            except PageError:
                value = None  # mid-navigation or a dropped socket: the label is simply not there yet (pending)
            if isinstance(value, dict):
                # The label spans two lines (name, tier); collapse them into one stable string.
                label = " ".join(str(value.get("label", "")).split())
                if label:
                    return ModelReading(label, MODEL_VERIFIED)
            if loop.time() >= deadline:
                # A peek (wait_s == 0) cannot conclude anything: the label is only "not there yet".
                return ModelReading(None, MODEL_PENDING if wait_s <= 0 else MODEL_UNKNOWN)
            await asyncio.sleep(MODEL_POLL_S)

    async def reconnect(self, timeout_s: float = RECONNECT_TIMEOUT_S) -> None:
        """Re-attach to the Gemini tab of the Chrome already running on the profile, once, within `timeout_s`.

        Never launches Chrome and never opens a tab; raises PageError(BROWSER_UNAVAILABLE) when there is
        nothing to attach to, so the caller reports it instead of retrying.
        """
        socket, self._socket = self._socket, None
        await _drop(socket)
        try:
            await asyncio.wait_for(self._connect(launch=False), timeout=timeout_s)
        except TimeoutError as exc:
            raise PageError(ErrorClass.BROWSER_UNAVAILABLE, f"reconnect did not finish within {timeout_s:.0f}s") from exc

    async def new_conversation(self) -> int:
        await self._call("Page.navigate", {"url": self._gemini_url})
        for _ in range(40):  # up to ~10 s for the new page to expose its input
            await asyncio.sleep(0.25)
            ready = await self._eval(f"!!document.querySelector({self._sel('input')})")
            if ready:
                break
        else:
            raise PageError(ErrorClass.BROWSER_UNAVAILABLE, "the Gemini input did not appear")
        return int(await self._eval(f"document.querySelectorAll({self._sel('answer')}).length") or 0)

    async def submit(self, prompt: str) -> None:
        focused = await self._eval(
            f"(()=>{{const e=document.querySelector({self._sel('input')});if(!e)return false;e.focus();return true}})()")
        if not focused:
            raise PageError(ErrorClass.SUBMIT_FAILED, "input box not found", submitted_possible=False)
        await self._call("Input.insertText", {"text": prompt}, reconnect=False)
        # Before the click: the composer must hold exactly the prompt and exactly one usable send button.
        check = await self._eval(
            "(()=>{const e=document.querySelector(" + self._sel("input") + ");"
            "const b=[...document.querySelectorAll(" + self._sel("send") + ")];"
            "return {text:e?e.innerText:null,buttons:b.length,enabled:b.filter(x=>!x.disabled).length}})()",
            reconnect=False)
        wanted = " ".join(prompt.split())
        got = " ".join(str((check or {}).get("text") or "").split())
        if got != wanted or (check or {}).get("buttons") != 1 or (check or {}).get("enabled") != 1:
            raise PageError(ErrorClass.SUBMIT_FAILED,
                            f"pre-send check failed (composer matches prompt: {got == wanted}, "
                            f"send buttons: {(check or {}).get('buttons')}); nothing was sent", submitted_possible=False)
        # From the click on, the prompt may be on its way: failures are SUBMISSION_UNCERTAIN, never retried.
        clicked = await self._eval(
            f"(()=>{{const b=document.querySelector({self._sel('send')});if(!b||b.disabled)return false;b.click();return true}})()",
            submitted_possible=True, reconnect=False)
        if not clicked:
            raise PageError(ErrorClass.SUBMIT_FAILED, "send button not found or disabled", submitted_possible=False)

    async def read_state(self) -> PageState:
        script = (
            "(()=>{const a=[...document.querySelectorAll(" + self._sel("answer") + ")];"
            "const g=document.querySelector(" + self._sel("generating") + ");"
            + ("const d=document.querySelector(" + self._sel("answer_done") + ");" if "answer_done" in self._selectors else "")
            + ("const p=!!document.querySelector(" + self._sel("pending") + ");" if "pending" in self._selectors else "const p=null;")
            + ("const u=[...document.querySelectorAll(" + self._sel("user_query") + ")];" if "user_query" in self._selectors else "const u=null;")
            + ("const x=document.querySelector(" + self._sel("error_banner") + ");" if "error_banner" in self._selectors else "const x=null;")
            + "return {pending:p,userTurns:u?u.length:null,userText:u&&u.length?u[u.length-1].innerText:null,turns:a.length,text:a.length?a[a.length-1].innerText:'',generating:"
            + ("(g?true:(d?false:null))" if "answer_done" in self._selectors else "!!g") + ","
            "error:x?x.innerText:null}})()"
        )
        try:
            value = await self._eval(script, submitted_possible=True, launch=False)
        except PageError as exc:
            if exc.error_class is not ErrorClass.CONNECTION_LOST:
                raise
            # Reading has no side effects, so one bounded reconnect is safe (Chrome is never relaunched here).
            await self.reconnect()
            value = await self._eval(script, submitted_possible=True, launch=False)
        if not isinstance(value, dict):
            raise PageError(ErrorClass.CONNECTION_LOST, "unreadable page state", True)
        generating = value.get("generating")  # True / False / None (unknown): never coerced
        pending = value.get("pending")
        return PageState(turns=int(value.get("turns", 0)), text=str(value.get("text", "")),
                         generating=generating if isinstance(generating, bool) else None, error=value.get("error"),
                         pending=pending if isinstance(pending, bool) else None,
                         user_turns=value.get("userTurns") if isinstance(value.get("userTurns"), int) else None,
                         user_text=value.get("userText") if isinstance(value.get("userText"), str) else None)
