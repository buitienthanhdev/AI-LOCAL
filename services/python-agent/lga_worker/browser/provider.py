"""BrowserProvider: one browser-use `BrowserSession` attached over CDP to a Chrome we control.

Design
* Chrome is launched by `chrome.launch` on the dedicated profile (or, when `browser.cdp_url` is set, an
  already-running Chrome started by the user is attached instead; we never touch the user's default
  profile ourselves). browser-use only attaches (`cdp_url=...`), so it never copies profiles.
* Only browser-use *primitives* are used (session, events, DOM state). Its `Tools`/`Controller`/`Agent`
  layer is not used: it instantiates telemetry and turns exceptions into text results.
* All operations run on the worker's asyncio (Proactor) loop, never on the desktop STA thread, and are
  serialised by one lock: a browser has a single focused tab, so interleaved actions would race.
* `attached()` is the only entry point. It (re)establishes the session and maps every exception to a
  deterministic `ToolError` code. Crash recovery: if our Chrome process exited or the CDP websocket is no
  longer open, the old session is killed, Chrome is relaunched on the same profile and a new session is
  started; the caller is told (`Attached.restarted` / `notice`) that tab ids and element indices are void.
* Element indices are only honoured against the page the planner last saw: `browser.get_state` records a
  snapshot (tab, a per-document JS token, a fingerprint per index). Index-based actions refresh the DOM and
  refuse with STALE_ELEMENT - without acting - when the document was replaced, the index is unknown, or the
  node at that index no longer matches. Once a click has been dispatched it is never reported as STALE.
* After an input action `settle()` watches a Navigation-API probe armed before the action: a navigation that
  has *started* (link click, form submit) is waited for until it commits and loads, even when the server is
  slower than `post_action_settle_ms`; without one, only the short settle window is spent.
* Detaching (`close(keep_open=True)`, or a dropped websocket while Chrome lives) only disconnects
  (`BrowserSession.stop()` after the event bus drained): Chrome and its tabs stay. Attaching verifies a usable
  page with a real JS call; a kept-open Chrome that cannot be attached is closed and relaunched.
* JavaScript dialogs are auto-accepted by browser-use's PopupsWatchdog; their messages are surfaced in
  `browser.get_state`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from lga_common.config import Config

from lga_worker import inventory
from lga_worker.browser import chrome as chrome_mod
from lga_worker.browser.chrome import ChromeProcess
from lga_worker.browser.state import describe_fingerprint, element_fingerprint
from lga_worker.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

# Set before browser_use is imported: no telemetry, no cloud sync, no version check, no extension
# downloads, no logging reconfiguration of our process.
FORCED_ENV = {
    "ANONYMIZED_TELEMETRY": "false",
    "BROWSER_USE_CLOUD_SYNC": "false",
    "BROWSER_USE_VERSION_CHECK": "false",
    "BROWSER_USE_DISABLE_EXTENSIONS": "1",
    "BROWSER_USE_SETUP_LOGGING": "false",
}
BROWSER_USE_CONFIG_SUBDIR = "browser-use"
BROWSER_USE_TEMP_PREFIXES = ("browser-use-user-data-dir-", "browser-use-downloads-")
LIBRARY_LOGGERS = ("browser_use", "bubus", "cdp_use")
LIBRARY_LOG_LEVEL = logging.WARNING

SESSION_TEARDOWN_TIMEOUT_S = 10.0
# Let queued browser-use events (focus changes, tab events) finish before disconnecting, so none runs
# against a torn-down session manager.
EVENT_DRAIN_TIMEOUT_S = 2.0
ATTACH_VERIFY_TIMEOUT_S = 5.0
# Extra time granted around a bubus event so its own `event_timeout` fires first with a precise error.
EVENT_GRACE_S = 2.0
MIN_CALL_TIMEOUT_S = 0.5
PAGE_TEXT_MAX_CHARS = 200_000
READY_STATE_COMPLETE = "complete"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_WIDTH_SLICE = slice(16, 20)
PNG_HEIGHT_SLICE = slice(20, 24)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TEXT_ENTRY_TAGS = frozenset({"input", "textarea", "select"})

PAGE_INFO_JS = "({url: location.href, title: document.title, ready: document.readyState})"
PAGE_TEXT_JS = f"(document.body ? document.body.innerText : '').slice(0, {PAGE_TEXT_MAX_CHARS})"
SCROLL_POS_JS = "({x: window.scrollX, y: window.scrollY, h: document.documentElement.scrollHeight})"
NODE_VALUE_FN = ("function() { if (this.isContentEditable) return this.innerText; "
                 "return ('value' in this) ? String(this.value) : null; }")
DOC_TOKEN_READ_JS = "window.__lgaDocToken || null"
DOC_TOKEN_ENSURE_JS = ("(() => {{ if (!window.__lgaDocToken) window.__lgaDocToken = '{token}';"
                       " return window.__lgaDocToken; }})()")
# Armed before an input action. The Navigation API `navigate` event fires synchronously when the document
# starts a navigation (link click, form submit, location change), long before the URL commits.
NAV_ARM_JS = (
    "(() => {{ const w = window; if (!w.__lgaNavHooked && w.navigation) {{"
    " w.navigation.addEventListener('navigate', e => {{ if (e.downloadRequest) return;"
    " w.__lgaNav = {{url: e.destination.url, same: !!e.destination.sameDocument}}; }});"
    " w.navigation.addEventListener('navigateerror', () => {{ w.__lgaNav = null; }}); w.__lgaNavHooked = true; }}"
    " w.__lgaNav = null; w.__lgaActToken = '{token}';"
    " return {{url: location.href, title: document.title, ready: document.readyState}}; }})()"
)
NAV_PROBE_JS = ("({url: location.href, title: document.title, ready: document.readyState,"
                " token: window.__lgaActToken || null, nav: window.__lgaNav || null})")
NODE_FILES_FN = ("function() { return this.files ? Array.from(this.files).map(f => ({name: f.name, size: f.size}))"
                 " : null; }")

RESTART_NOTICE = ("Browser was restarted ({reason}); earlier tab ids and element indices are invalid - "
                  "call browser.get_state before using indices.")
REATTACH_FAILED_NOTICE = ("The Chrome left open earlier could not be attached ({reason}); it was closed and "
                          "relaunched - earlier tab ids and element indices are invalid.")
NOTHING_DONE = "Nothing was done."
RECONNECT_NOTICE = ("Browser connection was re-established ({reason}); element indices are invalid - "
                    "call browser.get_state before using indices.")


@dataclass(frozen=True)
class BrowserSettings:
    chrome_path: str
    profile_dir: Path
    cdp_url: str
    downloads_dir: Path
    headless: bool
    launch_timeout_s: float
    navigation_timeout_s: float
    action_timeout_s: float
    max_state_elements: int
    post_action_settle_s: float
    poll_interval_s: float
    text_excerpt_chars: int
    observation_max_chars: int
    close_timeout_s: float
    keep_open: bool
    log_dir: Path
    state_dir: Path

    @classmethod
    def from_config(cls, config: Config) -> BrowserSettings:
        return cls(
            chrome_path=str(config.get("browser.chrome_path")),
            profile_dir=config.path("browser.profile_dir").resolve(),
            cdp_url=str(config.get("browser.cdp_url")).strip(),
            downloads_dir=config.path("browser.downloads_dir").resolve(),
            headless=bool(config.get("browser.headless")),
            launch_timeout_s=config.seconds("browser.launch_timeout_ms"),
            navigation_timeout_s=config.seconds("browser.navigation_timeout_ms"),
            action_timeout_s=config.seconds("browser.action_timeout_ms"),
            max_state_elements=int(config.get("browser.max_state_elements")),
            post_action_settle_s=config.seconds("browser.post_action_settle_ms"),
            poll_interval_s=config.seconds("browser.poll_interval_ms"),
            text_excerpt_chars=int(config.get("browser.text_excerpt_chars")),
            observation_max_chars=int(config.get("browser.observation_max_chars")),
            close_timeout_s=config.seconds("browser.close_timeout_ms"),
            keep_open=bool(config.get("browser.keep_open")),
            log_dir=(config.path("paths.logs_dir") / "browser").resolve(),
            state_dir=config.path("paths.state_dir").resolve(),
        )


@dataclass
class Attached:
    """Result of `BrowserProvider.attached()`: the live session plus what happened to get it."""

    session: Any
    launched: bool = False
    restarted: bool = False
    notice: str = ""


@dataclass
class PageInfo:
    url: str
    title: str
    ready_state: str
    pending_url: str | None = None  # a navigation started by the action but not committed yet


@dataclass
class ActionProbe:
    """Page before an input action plus the token of the navigation probe armed in that document."""

    before: PageInfo
    token: str | None


@dataclass
class StateSnapshot:
    """What the planner saw in its last browser.get_state: indices are only valid against this."""

    target_id: str | None
    doc_token: str | None
    url: str
    fingerprints: dict[int, tuple[str, ...]] = field(default_factory=dict)


_BU: SimpleNamespace | None = None


def browser_use_installed() -> bool:
    """Cheap check (no import) used by browser.status."""
    from importlib.util import find_spec

    return find_spec("browser_use") is not None


def browser_use_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("browser-use")
    except PackageNotFoundError:
        return None


def load_browser_use(state_dir: Path) -> SimpleNamespace:
    """Import browser_use once, with telemetry/version-check/extensions disabled beforehand."""
    global _BU
    if _BU is not None:
        return _BU
    os.environ.update(FORCED_ENV)
    os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(state_dir / BROWSER_USE_CONFIG_SUBDIR))
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(LIBRARY_LOG_LEVEL)
    try:
        from browser_use.browser import BrowserSession
        from browser_use.browser import events
        from browser_use.browser.session import DEFAULT_BROWSER_PROFILE
        from browser_use.browser.views import BrowserError
    except ImportError as exc:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"browser-use is not importable: {exc}") from exc
    # The module-level default profile (unused by us) creates an empty %TEMP% downloads dir on import.
    discard_empty_temp_dir(DEFAULT_BROWSER_PROFILE.downloads_path)
    _BU = SimpleNamespace(BrowserSession=BrowserSession, events=events, BrowserError=BrowserError)
    return _BU


def discard_empty_temp_dir(path: str | Path | None) -> None:
    """Remove an EMPTY browser-use scratch dir directly under %TEMP% (never anything else).

    browser-use's pydantic models create `browser-use-user-data-dir-*` / `browser-use-downloads-*` dirs as
    defaults even when we only attach over CDP; nothing uses them in that mode.
    """
    if not path:
        return
    candidate = Path(path)
    try:
        resolved = candidate.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return
    if resolved.parent != temp_root or not resolved.name.startswith(BROWSER_USE_TEMP_PREFIXES):
        return
    try:
        resolved.rmdir()  # fails (and is kept) unless empty
    except OSError as exc:
        logger.debug("kept %s: %s", resolved, exc)


# --------------------------------------------------------------------------- error mapping

_NOT_FOUND_NET_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_ADDRESS_UNREACHABLE", "ERR_CONNECTION_REFUSED",
                         "ERR_FILE_NOT_FOUND", "ERR_INTERNET_DISCONNECTED")
_DISCONNECT_MARKERS = ("websocket", "connection closed", "not connected", "cdp client not initialized",
                       "root cdp client not initialized", "sessionmanager not initialized",
                       "browser session is corrupted", "no valid agent focus", "connectionclosed")


def to_tool_error(exc: BaseException, action: str = "browser action") -> ToolError:
    """Deterministic mapping of browser-use / CDP / OS exceptions to the shared error model."""
    if isinstance(exc, ToolError):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in type(exc).__name__.lower():
        return ToolError(ErrorCode.TIMEOUT, f"{action} timed out: {text}")
    if "net::" in text:
        code = ErrorCode.NOT_FOUND if any(e in text for e in _NOT_FOUND_NET_ERRORS) else ErrorCode.EXECUTION_FAILED
        return ToolError(code, f"{action} failed: {text}")
    if any(marker in lowered for marker in _DISCONNECT_MARKERS) or isinstance(exc, ConnectionError):
        return ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"{action}: browser connection lost: {text}")
    if "not a file input" in lowered:
        return ToolError(ErrorCode.INVALID_ARGUMENT, f"{action}: {exc}")
    if "has detached" in lowered or "no node with given id" in lowered or "could not find node" in lowered \
            or "node with given id" in lowered:
        return ToolError(ErrorCode.STALE_ELEMENT, f"{action}: element is gone from the page ({text}); "
                                                  "call browser.get_state")
    return ToolError(ErrorCode.EXECUTION_FAILED, f"{action} failed: {text}")


# --------------------------------------------------------------------------- provider


class BrowserProvider:
    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._session: Any = None
        self._chrome: ChromeProcess | None = None
        self._mode = ""  # launched | reused | attached
        self._downloads: dict[str, str] = {}  # path -> source url ("" when unknown)
        self._snapshot: StateSnapshot | None = None
        self._start_notice = ""  # set by _start when a kept-open Chrome had to be replaced
        self.restarts = 0

    # ----------------------------------------------------------------- lifecycle

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def chrome_pid(self) -> int | None:
        return self._chrome.pid if self._chrome else None

    @property
    def cdp_url(self) -> str:
        if self.settings.cdp_url:
            return self.settings.cdp_url
        return self._chrome.cdp_url if self._chrome else ""

    def unhealthy_reason(self) -> str | None:
        """Why the current session cannot be used, or None when it looks usable (cheap, no I/O)."""
        session = self._session
        if session is None:
            return "no session"
        if self._chrome is not None and not self._chrome.is_alive():
            return f"Chrome process {self._chrome.pid} exited"
        if not session.is_cdp_connected:
            return "CDP websocket closed"
        if session.session_manager is None or session.agent_focus_target_id is None:
            return "session has no focused tab"
        return None

    @property
    def connected(self) -> bool:
        return self.unhealthy_reason() is None

    @asynccontextmanager
    async def attached(self, timeout_s: float, action: str = "browser action") -> AsyncIterator[Attached]:
        """Serialise one tool call, ensure a live session and map any failure to a ToolError."""
        async with self._lock:
            try:
                yield await self._ensure_connected(timeout_s)
            except (ToolError, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to a deterministic error code, never swallowed
                logger.debug("%s failed", action, exc_info=True)
                raise to_tool_error(exc, action) from exc

    async def _ensure_connected(self, timeout_s: float) -> Attached:
        reason = self.unhealthy_reason()
        if reason is None:
            return Attached(self._session)
        had_session = self._session is not None
        chrome_alive = self._chrome is not None and self._chrome.is_alive()
        if had_session:
            logger.warning("browser session unusable (%s); recovering", reason)
            self._collect_downloads()
            await self._drop_session(detach=chrome_alive)
        if had_session and not chrome_alive and self._chrome is not None:
            await asyncio.to_thread(self._chrome.kill)  # strays (renderers) holding the profile
            self._chrome = None
        launched = await self._start(timeout_s, reuse_chrome=chrome_alive)
        start_notice, self._start_notice = self._start_notice, ""
        if not had_session:
            if start_notice:
                return Attached(self._session, launched=launched, restarted=True, notice=start_notice)
            return Attached(self._session, launched=launched)
        self.restarts += 1
        template = RESTART_NOTICE if launched else RECONNECT_NOTICE
        notice = start_notice or template.format(reason=reason)
        return Attached(self._session, launched=launched, restarted=True, notice=notice)

    async def _start(self, timeout_s: float, reuse_chrome: bool) -> bool:
        """Start a session, launching Chrome if needed. Returns True when a new Chrome was launched.

        A running Chrome of ours (kept open by an earlier run, or still alive after a websocket drop) is
        re-attached first; if that fails it is closed and replaced rather than failing the tool call.
        """
        deadline = time.monotonic() + timeout_s

        def remaining() -> float:
            return max(MIN_CALL_TIMEOUT_S, deadline - time.monotonic())

        if self.settings.cdp_url:
            self._session = await self._attach(self.settings.cdp_url, remaining(), ensure_page=False)
            self._mode = "attached"
            return False
        if not (reuse_chrome and self._chrome is not None):
            self._chrome = await asyncio.to_thread(chrome_mod.find_running, self.settings.profile_dir)
            if self._chrome is not None:
                self._mode = "reused"
        if self._chrome is not None:
            try:
                self._session = await self._attach(self._chrome.cdp_url, remaining(), ensure_page=True)
                self._mode = self._mode or "reused"
                return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - recovered below by relaunching; reason reported to the planner
                reason = f"{type(exc).__name__}: {exc}"
                logger.warning("could not attach to running Chrome pid %s (%s); relaunching", self._chrome.pid, reason)
                await asyncio.to_thread(self._chrome.kill)
                self._chrome = None
                self._start_notice = REATTACH_FAILED_NOTICE.format(reason=reason)
        executable = inventory.find_chrome(self.settings.chrome_path)
        if executable is None:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            "Chrome is not installed (set browser.chrome_path in configs/config.toml)")
        self._chrome = await asyncio.to_thread(
            chrome_mod.launch, executable, self.settings.profile_dir, self.settings.headless,
            min(self.settings.launch_timeout_s, remaining()), self.settings.log_dir)
        try:
            self._session = await self._attach(self._chrome.cdp_url, remaining(), ensure_page=True)
        except BaseException:
            await asyncio.to_thread(self._chrome.kill)
            self._chrome = None
            raise
        self._mode = "launched"
        return True

    async def _attach(self, cdp_url: str, timeout_s: float, ensure_page: bool) -> Any:
        """Connect a new browser-use session to `cdp_url` and prove it can run JS in a page."""
        deadline = time.monotonic() + timeout_s
        bu = load_browser_use(self.settings.state_dir)
        if ensure_page:
            # A Chrome kept open by an earlier run may have no tab left; browser-use's focus logic needs one.
            await asyncio.to_thread(chrome_mod.ensure_page_target, cdp_url)
        self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        host = (urlsplit(cdp_url).hostname or "").lower()
        session = bu.BrowserSession(
            cdp_url=cdp_url, is_local=host in LOCAL_HOSTS, keep_alive=True, highlight_elements=False,
            enable_default_extensions=False, downloads_path=str(self.settings.downloads_dir),
            headless=self.settings.headless if not self.settings.cdp_url else False,
        )
        discard_empty_temp_dir(session.browser_profile.user_data_dir)  # unused when attaching over CDP
        try:
            await asyncio.wait_for(session.start(), timeout=max(MIN_CALL_TIMEOUT_S, deadline - time.monotonic()))
            if session.agent_focus_target_id is None or session.session_manager is None:
                raise RuntimeError("attached, but browser-use has no focused tab")
            self._session = session
            await self.evaluate(PAGE_INFO_JS, ATTACH_VERIFY_TIMEOUT_S)  # the focused tab really answers
        except BaseException:
            self._session = None
            await self._detach_session(session)
            raise
        logger.info("browser session ready (%s)", cdp_url)
        return session

    async def _detach_session(self, session: Any) -> None:
        """Disconnect without touching Chrome or its tabs (keep_alive session: stop() never closes the browser)."""
        try:
            await session.event_bus.wait_until_idle(timeout=EVENT_DRAIN_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - draining is best effort before a disconnect; logged
            logger.debug("event bus did not drain before detach", exc_info=True)
        try:
            await asyncio.wait_for(session.stop(), timeout=SESSION_TEARDOWN_TIMEOUT_S)
        except TimeoutError:
            logger.warning("browser session detach timed out; abandoning it")
        except Exception:  # noqa: BLE001 - disconnecting a broken session; logged with traceback, caller continues
            logger.warning("browser session detach failed", exc_info=True)

    async def _kill_session(self, session: Any) -> None:
        try:
            await asyncio.wait_for(session.kill(), timeout=SESSION_TEARDOWN_TIMEOUT_S)
        except TimeoutError:
            logger.warning("browser session teardown timed out; abandoning it")
        except Exception:  # noqa: BLE001 - teardown of a dead session; logged with traceback, caller continues
            logger.warning("browser session teardown failed", exc_info=True)

    async def _drop_session(self, detach: bool) -> None:
        session, self._session = self._session, None
        self._snapshot = None
        if session is None:
            return
        if detach:
            await self._detach_session(session)
        else:
            await self._kill_session(session)

    async def close(self, keep_open: bool | None = None) -> None:
        """Called by Services.close(). Always disconnects first (so browser-use never reacts to the browser
        going away). Closes the Chrome we own (CDP Browser.close, then kill) unless `keep_open` (default:
        [browser] keep_open): then it stays open and the next run re-attaches via DevToolsActivePort."""
        keep = self.settings.keep_open if keep_open is None else keep_open
        async with self._lock:
            self._collect_downloads()
            await self._drop_session(detach=True)
            chrome, self._chrome, self._mode = self._chrome, None, ""
            if chrome is None:
                return
            if keep:
                chrome.close_log()
                logger.info("leaving Chrome pid %s open (browser.keep_open)", chrome.pid)
                return
            if chrome.is_alive():
                await chrome_mod.close_gracefully(chrome.cdp_url, self.settings.close_timeout_s)
            deadline = time.monotonic() + self.settings.close_timeout_s
            while chrome.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(self.settings.poll_interval_s)
            await asyncio.to_thread(chrome.kill)

    # ----------------------------------------------------------------- low-level helpers

    @property
    def session(self) -> Any:
        if self._session is None:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "browser session is not started")
        return self._session

    async def dispatch(self, event: Any, timeout_s: float) -> Any:
        """Dispatch a browser-use event and return its handler result (exceptions re-raised)."""
        event.event_timeout = max(MIN_CALL_TIMEOUT_S, timeout_s)
        pending = self.session.event_bus.dispatch(event)
        await asyncio.wait_for(pending, timeout=event.event_timeout + EVENT_GRACE_S)
        return await pending.event_result(raise_if_any=True, raise_if_none=False)

    async def evaluate(self, expression: str, timeout_s: float) -> Any:
        """Evaluate JS in the focused tab's main frame and return the JSON value."""
        cdp = await self.session.get_or_create_cdp_session(target_id=None, focus=False)
        result = await asyncio.wait_for(
            cdp.cdp_client.send.Runtime.evaluate(params={"expression": expression, "returnByValue": True},
                                                 session_id=cdp.session_id),
            timeout=max(MIN_CALL_TIMEOUT_S, timeout_s))
        if result.get("exceptionDetails"):
            raise RuntimeError(f"page script failed: {result['exceptionDetails'].get('text', 'exception')}")
        return result.get("result", {}).get("value")

    async def call_on_node(self, node: Any, function: str, timeout_s: float) -> Any:
        cdp = await self.session.cdp_client_for_node(node)

        async def run() -> Any:
            resolved = await cdp.cdp_client.send.DOM.resolveNode(params={"backendNodeId": node.backend_node_id},
                                                                 session_id=cdp.session_id)
            object_id = resolved.get("object", {}).get("objectId")
            if not object_id:
                raise ToolError(ErrorCode.STALE_ELEMENT, "element no longer exists; call browser.get_state")
            result = await cdp.cdp_client.send.Runtime.callFunctionOn(
                params={"functionDeclaration": function, "objectId": object_id, "returnByValue": True},
                session_id=cdp.session_id)
            if result.get("exceptionDetails"):
                raise RuntimeError(f"element script failed: {result['exceptionDetails'].get('text', 'exception')}")
            return result.get("result", {}).get("value")

        return await asyncio.wait_for(run(), timeout=max(MIN_CALL_TIMEOUT_S, timeout_s))

    def _target_info(self) -> PageInfo:
        session = self.session
        target_id = session.agent_focus_target_id
        if target_id is None or session.session_manager is None:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "browser has no focused tab")
        target = session.session_manager.get_target(target_id)
        return PageInfo(url=target.url, title=target.title or "", ready_state="unknown")

    async def page_info(self, timeout_s: float | None = None) -> PageInfo:
        """URL/title/readyState of the focused tab (JS; falls back to CDP target info mid-navigation)."""
        try:
            value = await self.evaluate(PAGE_INFO_JS, timeout_s or self.settings.action_timeout_s)
        except (ToolError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. execution context destroyed by a navigation in flight
            logger.debug("page_info via JS failed (%s); using target info", exc)
            return self._target_info()
        if not isinstance(value, dict):
            return self._target_info()
        url = str(value.get("url", ""))
        # browser-use's AboutBlankWatchdog animates "Starting agent ..." into the title of blank tabs.
        title = "" if url == chrome_mod.INITIAL_URL else str(value.get("title", ""))
        return PageInfo(url=url, title=title, ready_state=str(value.get("ready", "")))

    async def page_text(self, timeout_s: float | None = None) -> str:
        value = await self.evaluate(PAGE_TEXT_JS, timeout_s or self.settings.action_timeout_s)
        return value if isinstance(value, str) else ""

    async def scroll_position(self) -> dict[str, int]:
        value = await self.evaluate(SCROLL_POS_JS, self.settings.action_timeout_s)
        return {k: int(v) for k, v in (value or {}).items()}

    async def wait_ready(self, timeout_s: float) -> PageInfo:
        deadline = time.monotonic() + timeout_s
        info = await self.page_info()
        while info.ready_state != READY_STATE_COMPLETE and time.monotonic() < deadline:
            await asyncio.sleep(self.settings.poll_interval_s)
            info = await self.page_info()
        return info

    async def arm_action(self) -> ActionProbe:
        """Before an input action: remember the page and arm the navigation probe in its document."""
        token = uuid.uuid4().hex
        try:
            value = await self.evaluate(NAV_ARM_JS.format(token=token), self.settings.action_timeout_s)
        except (ToolError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - page mid-navigation: fall back to URL polling only
            logger.debug("navigation probe not armed (%s)", exc)
            return ActionProbe(await self.page_info(), None)
        if not isinstance(value, dict):
            return ActionProbe(await self.page_info(), None)
        url = str(value.get("url", ""))
        title = "" if url == chrome_mod.INITIAL_URL else str(value.get("title", ""))
        return ActionProbe(PageInfo(url=url, title=title, ready_state=str(value.get("ready", ""))), token)

    async def _probe_navigation(self) -> dict[str, Any] | None:
        try:
            value = await self.evaluate(NAV_PROBE_JS, self.settings.action_timeout_s)
        except (ToolError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - execution context destroyed: a navigation is committing right now
            return None
        return value if isinstance(value, dict) else None

    async def settle(self, probe: ActionProbe, timeout_s: float) -> PageInfo:
        """After an input action, report the page it led to.

        * URL changed or the document was replaced -> wait for the load, then (within the short settle window)
          for the title to follow, since client-side routers update it late.
        * A navigation was started (Navigation API) but has not committed -> keep waiting up to the
          navigation timeout: a slow server must not turn a real link click into "URL unchanged".
        * Nothing started within `post_action_settle_ms` -> the action did not navigate.
        """
        start = time.monotonic()
        settle_deadline = start + min(self.settings.post_action_settle_s, timeout_s)
        nav_deadline = start + min(self.settings.navigation_timeout_s, timeout_s)
        before = probe.before
        pending: str | None = None
        while True:
            state = await self._probe_navigation()
            if state is None:
                navigating = True
            else:
                replaced = probe.token is not None and state.get("token") != probe.token
                if str(state.get("url", "")) != before.url or replaced:
                    return await self._finish_navigation(before, settle_deadline, nav_deadline)
                nav = state.get("nav")
                started = isinstance(nav, dict) and nav.get("url") and not nav.get("same")
                pending = str(nav["url"]) if started else None
                navigating = pending is not None
            if time.monotonic() >= (nav_deadline if navigating else settle_deadline):
                info = await self.page_info()
                info.pending_url = pending if navigating else None
                return info
            await asyncio.sleep(self.settings.poll_interval_s)

    async def _finish_navigation(self, before: PageInfo, settle_deadline: float, nav_deadline: float) -> PageInfo:
        info = await self.wait_ready(max(0.0, nav_deadline - time.monotonic()))
        while info.title == before.title and time.monotonic() < settle_deadline:
            await asyncio.sleep(self.settings.poll_interval_s)
            info = await self.page_info()
        return info

    # ----------------------------------------------------------------- primitives

    async def navigate(self, url: str, new_tab: bool, timeout_s: float) -> PageInfo:
        bu = load_browser_use(self.settings.state_dir)
        wait_s = min(self.settings.navigation_timeout_s, timeout_s)
        event = bu.events.NavigateToUrlEvent(url=url, new_tab=new_tab, wait_until="load",
                                             timeout_ms=int(wait_s * 1000))
        await self.dispatch(event, wait_s)
        return await self.page_info()

    async def go_back(self, timeout_s: float) -> None:
        bu = load_browser_use(self.settings.state_dir)
        await self.dispatch(bu.events.GoBackEvent(), min(self.settings.action_timeout_s, timeout_s))

    async def tabs(self) -> tuple[list[Any], str | None]:
        session = self.session
        return await session.get_tabs(), session.agent_focus_target_id

    async def switch_tab(self, target_id: str, timeout_s: float) -> None:
        bu = load_browser_use(self.settings.state_dir)
        await self.dispatch(bu.events.SwitchTabEvent(target_id=target_id), min(self.settings.action_timeout_s,
                                                                               timeout_s))

    async def close_tab(self, target_id: str, timeout_s: float) -> None:
        bu = load_browser_use(self.settings.state_dir)
        session = self.session
        tabs, _ = await self.tabs()
        if len(tabs) == 1:
            # Closing the last tab would end the Chrome process; keep an empty tab instead.
            await session.cdp_client.send.Target.createTarget(params={"url": chrome_mod.INITIAL_URL})
        await self.dispatch(bu.events.CloseTabEvent(target_id=target_id), min(self.settings.action_timeout_s,
                                                                              timeout_s))

    async def browser_state(self, timeout_s: float) -> Any:
        """Fresh DOM state of the focused tab (no screenshot); refreshes the selector map used by actions."""
        return await asyncio.wait_for(self.session.get_browser_state_summary(include_screenshot=False),
                                      timeout=max(MIN_CALL_TIMEOUT_S, timeout_s))

    async def _document_token(self, ensure: bool) -> str | None:
        """Per-document marker: survives SPA route changes, disappears when the document is replaced."""
        expression = DOC_TOKEN_ENSURE_JS.format(token=uuid.uuid4().hex) if ensure else DOC_TOKEN_READ_JS
        try:
            value = await self.evaluate(expression, self.settings.action_timeout_s)
        except (ToolError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - context destroyed mid-navigation means "not the same document"
            logger.debug("document token unavailable (%s)", exc)
            return None
        return value if isinstance(value, str) else None

    async def remember_state(self, state: Any, url: str) -> StateSnapshot:
        """Record what browser.get_state showed the planner; index actions are checked against it."""
        selector_map = state.dom_state.selector_map if state.dom_state else {}
        self._snapshot = StateSnapshot(
            target_id=self.session.agent_focus_target_id, doc_token=await self._document_token(ensure=True), url=url,
            fingerprints={int(i): element_fingerprint(node) for i, node in selector_map.items()})
        return self._snapshot

    def _stale(self, index: int, reason: str, current_url: str = "") -> ToolError:
        snapshot_url = self._snapshot.url if self._snapshot else ""
        return ToolError(ErrorCode.STALE_ELEMENT,
                         f"{NOTHING_DONE} Element index {index} cannot be used: {reason}. "
                         "Call browser.get_state for fresh indices.",
                         details={"index": index, "url": current_url, "state_url": snapshot_url, "performed": False})

    async def element(self, index: int, timeout_s: float) -> Any:
        """The node for `index`, guaranteed to be the element the planner saw in its last browser.get_state.

        Raises STALE_ELEMENT (without acting) when the tab or document changed since that state, the index was
        not in it, or the node at that index is gone or is now a different element (backend node ids can repeat
        across documents, so presence in the fresh selector map alone is not proof).
        """
        snapshot = self._snapshot
        if snapshot is None:
            raise self._stale(index, "no browser.get_state was taken in the current browser session")
        if snapshot.target_id != self.session.agent_focus_target_id:
            raise self._stale(index, "the active tab changed since the last browser.get_state")
        if index not in snapshot.fingerprints:
            raise self._stale(index, f"it was not in the last browser.get_state of {snapshot.url}")

        async def same_document() -> None:
            token = await self._document_token(ensure=False)
            if snapshot.doc_token is None or token != snapshot.doc_token:
                info = await self.page_info()
                raise self._stale(index, f"the page changed since the last browser.get_state (it showed "
                                         f"{snapshot.url}; the tab now shows {info.url})", info.url)

        await same_document()
        state = await self.browser_state(timeout_s)
        await same_document()  # a navigation may have committed while the DOM was being read
        node = (state.dom_state.selector_map if state.dom_state else {}).get(index)
        if node is None:
            raise self._stale(index, "that element is no longer on the page", state.url)
        expected, actual = snapshot.fingerprints[index], element_fingerprint(node)
        if actual != expected:
            raise self._stale(index, f"it now names {describe_fingerprint(actual)} instead of "
                                     f"{describe_fingerprint(expected)}", state.url)
        return node

    async def _after_failed_dispatch(self, action: str, exc: Exception, before: PageInfo) -> dict[str, Any]:
        """An action raised after it may already have reached the page. Never answer STALE at this point: if the
        page navigated the action evidently happened; otherwise report that it may or may not have happened."""
        try:
            after = await self.page_info()
        except ToolError:
            after = None
        if after is not None and after.url != before.url:
            logger.info("%s raised %s, but the page navigated to %s; treating it as performed", action, exc, after.url)
            return {"warning": f"{action} raised {type(exc).__name__} after the page started navigating"}
        error = to_tool_error(exc, f"browser.{action}")
        if error.code == ErrorCode.STALE_ELEMENT:
            error = ToolError(ErrorCode.EXECUTION_FAILED,
                              f"browser.{action} may not have reached the element ({type(exc).__name__}: {exc}); the "
                              "URL did not change - call browser.get_state before retrying")
        error.details["maybe_performed"] = True
        raise error from exc

    def _click_event(self, node: Any) -> Any:
        return load_browser_use(self.settings.state_dir).events.ClickElementEvent(node=node)

    async def click(self, node: Any, before: PageInfo, timeout_s: float) -> dict[str, Any] | None:
        event = self._click_event(node)  # built before the try: a failure here means nothing was sent
        try:
            result = await self.dispatch(event, min(self.settings.action_timeout_s, timeout_s))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - decided truthfully from the page state, never swallowed
            return await self._after_failed_dispatch("click", exc, before)
        if isinstance(result, dict) and result.get("validation_error"):
            # browser-use validates (file input, <select>, ...) before any mouse event is sent.
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{NOTHING_DONE} {result['validation_error']}")
        return result if isinstance(result, dict) else None

    async def type_text(self, node: Any, text: str, clear: bool, timeout_s: float) -> str | None:
        """Type into the element; returns the field value read back afterwards (None if not readable)."""
        bu = load_browser_use(self.settings.state_dir)
        result = await self.dispatch(bu.events.TypeTextEvent(node=node, text=text, clear=clear),
                                     min(self.settings.action_timeout_s, timeout_s))
        # browser-use may fall back to typing into the page; always read the element itself back.
        value = await self.call_on_node(node, NODE_VALUE_FN, self.settings.action_timeout_s)
        if value is None and isinstance(result, dict) and isinstance(result.get("actual_value"), str):
            value = result["actual_value"]
        return value if isinstance(value, str) else None

    async def send_keys(self, keys: str, timeout_s: float) -> None:
        bu = load_browser_use(self.settings.state_dir)
        await self.dispatch(bu.events.SendKeysEvent(keys=keys), min(self.settings.action_timeout_s, timeout_s))

    async def scroll(self, direction: str, amount_px: int, node: Any | None, timeout_s: float) -> None:
        bu = load_browser_use(self.settings.state_dir)
        await self.dispatch(bu.events.ScrollEvent(direction=direction, amount=amount_px, node=node),
                            min(self.settings.action_timeout_s, timeout_s))

    def file_input_for(self, node: Any) -> Any:
        session = self.session
        if session.is_file_input(node):
            return node
        if (node.tag_name or "").lower() in TEXT_ENTRY_TAGS:
            # Never redirect a text field to some file input nearby: that would upload into the wrong control.
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"element is a <{node.tag_name.lower()}> text control, not a "
                                                        "file input or upload button")
        near = session.find_file_input_near_element(node)
        if near is None:
            raise ToolError(ErrorCode.INVALID_ARGUMENT,
                            "the element is not a file input and has no file input nearby; pick the upload control")
        return near

    async def upload(self, node: Any, path: Path, timeout_s: float) -> list[dict[str, Any]]:
        bu = load_browser_use(self.settings.state_dir)
        target = self.file_input_for(node)
        await self.dispatch(bu.events.UploadFileEvent(node=target, file_path=str(path)),
                            min(self.settings.action_timeout_s, timeout_s))
        files = await self.call_on_node(target, NODE_FILES_FN, self.settings.action_timeout_s)
        return files if isinstance(files, list) else []

    async def screenshot(self, full_page: bool, timeout_s: float) -> bytes:
        return await asyncio.wait_for(self.session.take_screenshot(full_page=full_page, format="png"),
                                      timeout=max(MIN_CALL_TIMEOUT_S, min(self.settings.action_timeout_s, timeout_s)))

    def _collect_downloads(self) -> None:
        session = self._session
        if session is None:
            return
        for path in session.downloaded_files:
            self._downloads.setdefault(path, "")

    def downloads(self) -> list[dict[str, Any]]:
        self._collect_downloads()
        files: list[dict[str, Any]] = []
        for raw in self._downloads:
            path = Path(raw)
            size = path.stat().st_size if path.is_file() else None
            files.append({"path": str(path), "file_name": path.name, "size": size, "exists": size is not None})
        return files


def png_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(PNG_SIGNATURE) or len(data) < PNG_HEIGHT_SLICE.stop:
        return None
    return int.from_bytes(data[PNG_WIDTH_SLICE], "big"), int.from_bytes(data[PNG_HEIGHT_SLICE], "big")
