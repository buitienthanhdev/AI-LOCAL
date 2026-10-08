"""Handlers for the browser.* tools (contract: tools/browser.json).

Every handler goes through `BrowserProvider.attached()`, which serialises browser calls, (re)starts or
recovers the dedicated Chrome and maps failures to deterministic error codes. When the browser had to
be restarted, the observation starts with a notice that tab ids and element indices are invalid.

This module must stay cheap to import (the worker imports it at startup): browser_use is only imported
by the provider when the first browser tool actually needs a session.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lga_worker import inventory
from lga_worker.browser.provider import (
    Attached,
    BrowserProvider,
    BrowserSettings,
    PageInfo,
    browser_use_installed,
    browser_use_version,
    png_size,
)
from lga_worker.browser.state import (
    HTTP_SCHEMES,
    MASK,
    PageCriteria,
    hosts_match,
    is_error_page,
    is_sensitive,
    normalized_host,
    resolve_tab_id,
    serialize_element,
    serialize_elements,
    serialize_tab,
    state_observation,
    tab_line,
    truncate,
)
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.tools.system import absolute_path

BACKEND = "browser-use"
SERVICE_NAME = "browser"
# browser.scroll without amount_px scrolls this fraction of the viewport height (keeps some overlap).
SCROLL_VIEWPORT_FRACTION = 0.8
VIEWPORT_HEIGHT_JS = "window.innerHeight"
BLANK_URLS = frozenset({"", "about:blank"})
DOCUMENT_READY_STATES = frozenset({"interactive", "complete"})
OBSERVATION_TITLE_CHARS = 120
MS_PER_S = 1000


async def _provider(ctx: ToolContext) -> BrowserProvider:
    async def create() -> BrowserProvider:
        return BrowserProvider(BrowserSettings.from_config(ctx.config))

    return await ctx.services.get_or_create(SERVICE_NAME, create)


def _with_notice(att: Attached, text: str) -> str:
    return f"{att.notice}\n{text}" if att.notice else text


def _page_line(info: PageInfo) -> str:
    return f"URL now {info.url} (title {truncate(info.title, OBSERVATION_TITLE_CHARS)!r})"


def _navigation_checks(requested: str, info: PageInfo) -> tuple[list[Check], bool]:
    same_host = hosts_match(requested, info.url)
    redirected = not same_host and not is_error_page(info.url) and info.url not in BLANK_URLS
    host_detail = f"requested {normalized_host(requested) or requested}, final {info.url}"
    checks = [
        Check("host_matches", same_host or redirected, host_detail + (" (redirected)" if redirected else "")),
        Check("document_present", bool(info.title) or info.ready_state in DOCUMENT_READY_STATES,
              f"title={info.title!r} readyState={info.ready_state}"),
    ]
    return checks, redirected


async def _tab_dicts(provider: BrowserProvider) -> list[dict[str, Any]]:
    tabs, active = await provider.tabs()
    return [serialize_tab(t, active) for t in tabs]


async def _resolve_tab(provider: BrowserProvider, requested: str) -> str:
    tabs, _ = await provider.tabs()
    tab_id = resolve_tab_id(requested, [t.target_id for t in tabs])
    if tab_id is None:
        raise ToolError(ErrorCode.NOT_FOUND, f"no open tab with id {requested!r}; call browser.tabs",
                        details={"tabs": [t.target_id for t in tabs]})
    return tab_id


async def _match(provider: BrowserProvider, criteria: PageCriteria) -> tuple[bool, list[str], PageInfo]:
    info = await provider.page_info()
    text = await provider.page_text() if criteria.needs_text else None
    ok, details = criteria.evaluate(info.url, info.title, text)
    return ok, details, info


def register(registry: ToolRegistry) -> None:
    @registry.handler("browser.status")
    async def status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        settings = BrowserSettings.from_config(ctx.config)
        chrome = await asyncio.to_thread(inventory.find_chrome, settings.chrome_path)
        version = await asyncio.to_thread(inventory.file_version, chrome) if chrome else None
        installed = browser_use_installed()
        bu_version = browser_use_version() if installed else None
        provider: BrowserProvider | None = ctx.services.peek(SERVICE_NAME)
        connected = provider is not None and provider.connected
        data: dict[str, Any] = {
            "ready": chrome is not None and installed, "chrome_path": str(chrome) if chrome else None,
            "chrome_version": version, "browser_use_version": bu_version, "connected": connected,
            "mode": provider.mode if connected and provider else "",
            "profile_dir": str(settings.profile_dir), "cdp_url": provider.cdp_url if connected and provider else "",
        }
        if connected and provider is not None:
            data["tabs"] = await _tab_dicts(provider)
        problems = [p for p, bad in (("Chrome not found (set browser.chrome_path)", chrome is None),
                                     ("browser-use is not installed (uv sync)", not installed)) if bad]
        state = (f"connected ({data['mode']}, {len(data.get('tabs', []))} tabs)" if connected
                 else "not running (started on the first browser.* call)")
        source = f"attach to {settings.cdp_url}" if settings.cdp_url else f"dedicated profile {settings.profile_dir}"
        observation = ("; ".join(problems) if problems
                       else f"Chrome {version} at {chrome}; browser-use {bu_version}; {source}; {state}")
        return ToolOutcome(data=data, observation=observation, backend=BACKEND)

    @registry.handler("browser.open")
    async def open_browser(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        url = args.get("url")
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.open") as att:
            checks = [Check("cdp_connected", provider.connected, provider.cdp_url)]
            redirected = False
            if url:
                info = await provider.navigate(url, False, ctx.timeout_s(None, settings.navigation_timeout_s))
                nav_checks, redirected = _navigation_checks(url, info)
                checks.extend(nav_checks)
            else:
                info = await provider.page_info()
            tab_id = att.session.agent_focus_target_id
        data: dict[str, Any] = {"url": info.url, "title": info.title, "tab_id": tab_id, "restarted": att.restarted,
                                "launched": att.launched, "mode": provider.mode}
        if url:
            data.update(final_url=info.url, redirected=redirected)
        how = "launched" if att.launched else "already running"
        return ToolOutcome(data=data, checks=checks, backend=BACKEND,
                           observation=_with_notice(att, f"Browser {how} ({provider.mode}). {_page_line(info)}"))

    @registry.handler("browser.navigate")
    async def navigate(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        url = args["url"]
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.navigate") as att:
            info = await provider.navigate(url, args["new_tab"], ctx.timeout_s(None, settings.navigation_timeout_s))
            tab_id = att.session.agent_focus_target_id
        checks, redirected = _navigation_checks(url, info)
        data = {"url": url, "final_url": info.url, "title": info.title, "tab_id": tab_id, "redirected": redirected,
                "restarted": att.restarted, "ready_state": info.ready_state}
        note = f" Redirected to another host: {info.url}." if redirected else ""
        return ToolOutcome(data=data, checks=checks, backend=BACKEND,
                           observation=_with_notice(att, f"Loaded {url}. {_page_line(info)}.{note}"))

    @registry.handler("browser.tabs")
    async def tabs(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        async with provider.attached(ctx.timeout_s(None, provider.settings.launch_timeout_s), "browser.tabs") as att:
            tab_list = await _tab_dicts(provider)
        return ToolOutcome(data={"tabs": tab_list, "restarted": att.restarted}, backend=BACKEND,
                           observation=_with_notice(att, "\n".join(tab_line(t) for t in tab_list) or "No tabs."))

    @registry.handler("browser.switch_tab")
    async def switch_tab(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.switch_tab") as att:
            tab_id = await _resolve_tab(provider, args["tab_id"])
            await provider.switch_tab(tab_id, ctx.timeout_s(None, settings.action_timeout_s))
            focused = att.session.agent_focus_target_id
            info = await provider.page_info()
        return ToolOutcome(data={"url": info.url, "title": info.title, "tab_id": focused, "restarted": att.restarted},
                           checks=[Check("tab_focused", focused == tab_id, f"focused {focused}")], backend=BACKEND,
                           observation=_with_notice(att, f"Switched to tab {tab_id}. {_page_line(info)}"))

    @registry.handler("browser.close_tab")
    async def close_tab(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.close_tab") as att:
            tab_id = await _resolve_tab(provider, args["tab_id"])
            await provider.close_tab(tab_id, ctx.timeout_s(None, settings.action_timeout_s))
            deadline = time.monotonic() + ctx.timeout_s(None, settings.action_timeout_s)
            tab_list = await _tab_dicts(provider)
            while any(t["tab_id"] == tab_id for t in tab_list) and time.monotonic() < deadline:
                await asyncio.sleep(settings.poll_interval_s)
                tab_list = await _tab_dicts(provider)
        gone = all(t["tab_id"] != tab_id for t in tab_list)
        return ToolOutcome(data={"closed": tab_id, "tabs": tab_list},
                           checks=[Check("tab_gone", gone, f"{len(tab_list)} tabs left")], backend=BACKEND,
                           observation=_with_notice(att, f"Closed tab {tab_id}. Open tabs:\n"
                                                    + "\n".join(tab_line(t) for t in tab_list)))

    @registry.handler("browser.get_state")
    async def get_state(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        max_elements = int(args.get("max_elements") or settings.max_state_elements)
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.get_state") as att:
            state = await provider.browser_state(ctx.timeout_s(None, settings.action_timeout_s))
            tab_list = await _tab_dicts(provider)
            text = await provider.page_text()
            info = await provider.page_info()  # JS title/url; CDP target info (state.title) can lag
            selector_map = state.dom_state.selector_map if state.dom_state else {}
            dom_text = state.dom_state.llm_representation() if state.dom_state else ""
        elements, truncated = serialize_elements(selector_map, max_elements)
        url, title = info.url or state.url, info.title or state.title
        for tab in tab_list:
            if tab["active"] and tab["url"] == url:
                tab["title"] = title
        dialogs = list(getattr(state, "closed_popup_messages", None) or [])
        notes = [att.notice] if att.notice else []
        notes += [f"JavaScript dialog was auto-accepted: {m}" for m in dialogs]
        if getattr(state, "state_error", None):
            notes.append(f"State capture problem: {state.state_error}")
        if not selector_map and urlsplit(url).scheme.lower() not in HTTP_SCHEMES:
            notes.append("Interactive elements are only extracted from http(s) pages.")
        if state.pixels_below:
            notes.append(f"{state.pixels_below}px more content below (browser.scroll down to see it).")
        observation = state_observation(url, title, tab_list, dom_text, element_count=len(selector_map),
                                        shown=len(elements), notes=notes, max_chars=settings.observation_max_chars)
        data = {"url": url, "title": title, "tabs": tab_list, "elements": elements,
                "element_count": len(selector_map), "truncated": truncated,
                "text_excerpt": truncate(text, settings.text_excerpt_chars),
                "pixels_above": int(state.pixels_above or 0), "pixels_below": int(state.pixels_below or 0),
                "dialogs": dialogs, "restarted": att.restarted}
        return ToolOutcome(data=data, observation=observation, backend=BACKEND)

    @registry.handler("browser.click")
    async def click(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        index = args["index"]
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.click") as att:
            node = await provider.element(index, ctx.timeout_s(None, settings.action_timeout_s))
            element = serialize_element(index, node)
            before = await provider.page_info()
            tabs_before, _ = await provider.tabs()
            meta = await provider.click(node, ctx.timeout_s(None, settings.navigation_timeout_s)) or {}
            info = await provider.settle(before, ctx.timeout_s(None, settings.navigation_timeout_s))
            tabs_after, _ = await provider.tabs()
        new_tabs = {t.target_id for t in tabs_after} - {t.target_id for t in tabs_before}
        new_tab_id = next(iter(new_tabs), None)
        download = meta.get("download", {}).get("path") if isinstance(meta.get("download"), dict) else None
        changed = info.url != before.url
        parts = [f"Clicked [{index}] <{element['tag']}> {element['text']!r}.", _page_line(info)
                 + (" (changed)" if changed else " (unchanged)") + "."]
        if new_tab_id:
            parts.append(f"A new tab opened: {new_tab_id} (use browser.switch_tab to work in it).")
        if download:
            parts.append(f"Downloaded {download}.")
        return ToolOutcome(data={"url": info.url, "title": info.title, "url_changed": changed, "element": element,
                                 "new_tab_id": new_tab_id, "download": download},
                           backend=BACKEND, observation=_with_notice(att, " ".join(parts)))

    @registry.handler("browser.type")
    async def type_text(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        index, text, clear = args["index"], args["text"], args["clear"]
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.type") as att:
            node = await provider.element(index, ctx.timeout_s(None, settings.action_timeout_s))
            sensitive = is_sensitive(node)
            before = await provider.page_info()
            value = await provider.type_text(node, text, clear, ctx.timeout_s(None, settings.action_timeout_s))
            info = before
            if args["press_enter"]:
                await provider.send_keys("Enter", ctx.timeout_s(None, settings.action_timeout_s))
                info = await provider.settle(before, ctx.timeout_s(None, settings.navigation_timeout_s))
        expected_empty = clear and text == ""
        ok = value is not None and (value == "" if expected_empty else text in value)
        shown = MASK if sensitive and value else value
        checks = [Check("value_contains_text", ok, f"read back {shown!r}" if value is not None
                        else "the element has no readable value")]
        typed = MASK if sensitive else repr(truncate(text, OBSERVATION_TITLE_CHARS))
        enter = f" and pressed Enter. {_page_line(info)}." if args["press_enter"] else "."
        return ToolOutcome(data={"value": shown, "url": info.url, "title": info.title,
                                 "url_changed": info.url != before.url},
                           checks=checks, backend=BACKEND,
                           observation=_with_notice(att, f"Typed {typed} into [{index}]{enter}"))

    @registry.handler("browser.press_keys")
    async def press_keys(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.press_keys") as att:
            before = await provider.page_info()
            await provider.send_keys(args["keys"], ctx.timeout_s(None, settings.action_timeout_s))
            info = await provider.settle(before, ctx.timeout_s(None, settings.navigation_timeout_s))
        return ToolOutcome(data={"url": info.url, "title": info.title, "url_changed": info.url != before.url},
                           backend=BACKEND,
                           observation=_with_notice(att, f"Pressed {args['keys']!r}. {_page_line(info)}."))

    @registry.handler("browser.scroll")
    async def scroll(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.scroll") as att:
            node = None
            if args.get("index") is not None:
                node = await provider.element(args["index"], ctx.timeout_s(None, settings.action_timeout_s))
            amount = args.get("amount_px")
            if amount is None:
                viewport = await provider.evaluate(VIEWPORT_HEIGHT_JS, settings.action_timeout_s)
                amount = max(1, int(float(viewport or 0) * SCROLL_VIEWPORT_FRACTION))
            before = await provider.scroll_position()
            await provider.scroll(args["direction"], int(amount), node, ctx.timeout_s(None, settings.action_timeout_s))
            await asyncio.sleep(settings.poll_interval_s)  # CDP scroll gestures finish asynchronously
            after = await provider.scroll_position()
        target = f"element [{args['index']}]" if node is not None else "page"
        return ToolOutcome(
            data={"scroll_y_before": before.get("y", 0), "scroll_y_after": after.get("y", 0),
                  "page_height": after.get("h", 0)},
            backend=BACKEND,
            observation=_with_notice(att, f"Scrolled {target} {args['direction']} by {amount}px; window scrollY "
                                          f"{before.get('y', 0)} -> {after.get('y', 0)} of {after.get('h', 0)}px."))

    @registry.handler("browser.upload")
    async def upload(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        path = absolute_path(args["path"])
        if not path.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"file to upload does not exist: {path}")
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.upload") as att:
            node = await provider.element(args["index"], ctx.timeout_s(None, settings.action_timeout_s))
            files = await provider.upload(node, path, ctx.timeout_s(None, settings.action_timeout_s))
        names = [str(f.get("name", "")) for f in files if isinstance(f, dict)]
        attached_ok = path.name in names
        return ToolOutcome(data={"path": str(path), "files": [f for f in files if isinstance(f, dict)]},
                           checks=[Check("file_attached", attached_ok, f"input files: {names}")], backend=BACKEND,
                           observation=_with_notice(att, f"Attached {path} to [{args['index']}] (input now has "
                                                         f"{len(names)} file(s): {', '.join(names)})."))

    @registry.handler("browser.wait_for")
    async def wait_for(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        criteria = PageCriteria.from_args(args)
        if criteria.empty:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give at least one of text_contains, url_contains, "
                                                        "title_contains")
        started = time.monotonic()
        deadline = started + ctx.timeout_s(args.get("timeout_ms"), settings.navigation_timeout_s)
        notice = ""
        while True:
            ctx.check_cancelled()
            async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.wait_for") as att:
                ok, details, info = await _match(provider, criteria)
            notice = notice or att.notice
            if ok:
                waited_ms = int((time.monotonic() - started) * MS_PER_S)
                text = f"Page matches after {waited_ms}ms: " + "; ".join(details)
                return ToolOutcome(data={"url": info.url, "title": info.title, "waited_ms": waited_ms},
                                   checks=[Check("page_matches", True, "; ".join(details))], backend=BACKEND,
                                   observation=f"{notice}\n{text}" if notice else text)
            if time.monotonic() >= deadline:
                raise ToolError(ErrorCode.TIMEOUT, "page did not reach the expected state: " + "; ".join(details),
                                details={"url": info.url, "title": info.title, "criteria": details})
            await asyncio.sleep(settings.poll_interval_s)

    @registry.handler("browser.page_matches")
    async def page_matches(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        criteria = PageCriteria.from_args(args)
        if criteria.empty:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give at least one of text_contains, url_contains, "
                                                        "title_contains")
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.page_matches") as att:
            ok, details, info = await _match(provider, criteria)
        return ToolOutcome(data={"matches": ok, "url": info.url, "title": info.title, "details": details},
                           backend=BACKEND,
                           observation=_with_notice(att, f"{'Matches' if ok else 'Does NOT match'}: "
                                                         + "; ".join(details)))

    @registry.handler("browser.screenshot")
    async def screenshot(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.screenshot") as att:
            png = await provider.screenshot(args["full_page"], ctx.timeout_s(None, settings.action_timeout_s))
            info = await provider.page_info()
        path: Path = ctx.artifact_dir("screenshots") / (f"{ctx.step_id or ctx.request_id}-browser-"
                                                        f"{int(time.time() * MS_PER_S)}.png")
        await asyncio.to_thread(path.write_bytes, png)
        size = png_size(png)
        written = path.is_file() and path.stat().st_size == len(png) and size is not None
        width, height = size or (0, 0)
        return ToolOutcome(
            data={"path": str(path), "width": width, "height": height, "url": info.url},
            checks=[Check("png_written", written, f"{path} ({len(png)} bytes)")],
            artifacts=[{"kind": "screenshot", "path": str(path)}], backend=BACKEND,
            observation=_with_notice(att, f"Browser screenshot {width}x{height} of {info.url} saved to {path}."))

    @registry.handler("browser.go_back")
    async def go_back(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        settings = provider.settings
        async with provider.attached(ctx.timeout_s(None, settings.launch_timeout_s), "browser.go_back") as att:
            before = await provider.page_info()
            await provider.go_back(ctx.timeout_s(None, settings.action_timeout_s))
            info = await provider.settle(before, ctx.timeout_s(None, settings.navigation_timeout_s))
        changed = info.url != before.url
        return ToolOutcome(data={"url": info.url, "title": info.title, "url_changed": changed}, backend=BACKEND,
                           observation=_with_notice(att, "Went back. " + _page_line(info)
                                                    + ("." if changed else " (no earlier history entry).")))

    @registry.handler("browser.downloads")
    async def downloads(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        files = provider.downloads()  # a few stat() calls; reads the session's list on the loop thread
        directory = provider.settings.downloads_dir
        lines = [f"{f['path']} ({f['size']} bytes)" if f["exists"] else f"{f['path']} (missing)" for f in files]
        return ToolOutcome(data={"downloads_dir": str(directory), "files": files}, backend=BACKEND,
                           observation="\n".join(lines) or f"No downloads yet (saved to {directory}).")
