"""Handlers for the desktop.* tools (contract: tools/desktop.json)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.windows import win32
from lga_worker.windows.feedback import pointer_line
from lga_worker.windows.provider import ElementQuery, Rect, UIElement, WindowInfo
from lga_worker.windows.windows_use_provider import WindowsUseProvider

BACKEND = "windows-use"
WAIT_POLL_INTERVAL_S = 0.25
# Elements rendered into the observation text the planner reads; the full list is in `data`.
OBSERVATION_ELEMENT_LIMIT = 150
OBSERVATION_VALUE_CHARS = 80


async def _sta(ctx: ToolContext, provider: WindowsUseProvider, fn, *args) -> Any:
    return await provider.executor.run(max(0.5, ctx.remaining_s()), fn, *args)


def _window_line(w: WindowInfo) -> str:
    return f"hwnd={w.hwnd} '{w.title}' [{w.process_name}] {w.state}{' (foreground)' if w.is_foreground else ''}"


def _element_line(e: UIElement) -> str:
    cx, cy = e.rect.center
    value = f" value='{e.value[:OBSERVATION_VALUE_CHARS]}'" if e.value else ""
    flags = "" if e.is_enabled else " disabled"
    state = f" [{e.state}]" if e.state else ""
    aid = f" #{e.automation_id}" if e.automation_id and len(e.automation_id) <= 24 and not e.automation_id.isdigit() else ""
    return f"{e.element_id} {e.control_type.removesuffix('Control')} '{e.name}'{value}{aid} @({cx},{cy}){state}{flags}"


def _texts_block(texts: list[str], limit: int = 30) -> str:
    if not texts:
        return ""
    shown = texts[:limit]
    more = f" (+{len(texts) - limit} more)" if len(texts) > limit else ""
    return "Text shown: " + " | ".join(shown) + more


def _focused_line(focused: dict[str, Any] | None) -> str:
    if not focused:
        return "Focused control: none"
    content = focused.get("text") if focused.get("text") is not None else focused.get("value")
    shown = f" text={content[:OBSERVATION_VALUE_CHARS * 2]!r}" if content else ""
    return f"Focused control: {focused.get('control_type')} '{focused.get('name')}'{shown}"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def _foreground_note(ctx: ToolContext, provider: WindowsUseProvider) -> str:
    window = await _sta(ctx, provider, provider.foreground_window)
    return f"Foreground window: {_window_line(window)}" if window else "No foreground window."


def register(registry: ToolRegistry) -> None:
    @registry.handler("desktop.list_windows")
    async def list_windows(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        windows = await _sta(ctx, provider, provider.list_windows)
        return ToolOutcome(data={"windows": [w.to_dict() for w in windows]}, backend=BACKEND,
                           observation="\n".join(_window_line(w) for w in windows) or "No windows.")

    @registry.handler("desktop.launch_app")
    async def launch_app(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        timeout = ctx.timeout_s(args.get("timeout_ms"), ctx.config.seconds("desktop.launch_timeout_ms"))
        result = await _sta(ctx, provider, provider.launch_app, args["target"], args["arguments"], timeout,
                            args.get("expect_title"))
        window = result.window
        note = f" Note: {result.note}." if result.note else ""
        return ToolOutcome(
            data={"pid": result.pid, "window": window.to_dict(), "match_kind": result.match_kind,
                  "resolved_target": result.resolved_target, "note": result.note},
            checks=[Check("window_appeared", True, _window_line(window))],
            observation=f"Launched {args['target']} -> {_window_line(window)} ({result.match_kind}).{note}",
            backend=BACKEND,
        )

    @registry.handler("desktop.close_app")
    async def close_app(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        timeout = min(ctx.config.seconds("desktop.close_timeout_ms"), ctx.remaining_s())
        result = await _sta(ctx, provider, provider.close_dialog_aware, args["hwnd"], args["force"], bool(args.get("discard")), timeout)
        if not result.closed:
            dialog = result.blocking_dialog
            question = f" It asks: {result.dialog_text}" if result.dialog_text else ""
            buttons = f" Buttons: {', '.join(result.dialog_buttons)}." if result.dialog_buttons else ""
            advice = (" Save the work first (desktop.save_as) or call desktop.close_app with discard=true to throw it away."
                      if result.dialog_buttons else "")
            raise ToolError(
                ErrorCode.EXECUTION_FAILED,
                f"window {args['hwnd']} did not close"
                + (f"; dialog '{dialog.title}' (hwnd={dialog.hwnd}) is waiting for an answer." if dialog else ".")
                + question + buttons + advice,
                retryable=False,
                details={"blocking_dialog": dialog.to_dict() if dialog else None, "buttons": result.dialog_buttons,
                         "text": result.dialog_text},
            )
        windows = await _sta(ctx, provider, provider.list_windows)
        gone = args["hwnd"] not in {w.hwnd for w in windows}
        return ToolOutcome(data={"closed": True}, checks=[Check("window_gone", gone, f"hwnd={args['hwnd']}")],
                           observation=f"Closed window {args['hwnd']}.", backend=BACKEND)

    @registry.handler("desktop.focus_window")
    async def focus_window(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        timeout = min(ctx.config.seconds("desktop.focus_timeout_ms"), ctx.remaining_s())
        window = await _sta(ctx, provider, provider.focus_window, args["hwnd"], timeout)
        return ToolOutcome(data={"window": window.to_dict()},
                           checks=[Check("is_foreground", True, _window_line(window))],
                           observation=f"Focused {_window_line(window)}", backend=BACKEND)

    @registry.handler("desktop.wait_for_window")
    async def wait_for_window(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        title = (args.get("title_contains") or "").lower()
        process = (args.get("process_name") or "").lower()
        deadline = time.monotonic() + ctx.timeout_s(args.get("timeout_ms"), ctx.remaining_s())
        while True:
            ctx.check_cancelled()
            for window in await _sta(ctx, provider, provider.list_windows):
                if title and title not in window.title.lower():
                    continue
                if process and window.process_name.lower() != process:
                    continue
                return ToolOutcome(data={"window": window.to_dict()},
                                   checks=[Check("window_found", True, _window_line(window))],
                                   observation=f"Found {_window_line(window)}", backend=BACKEND)
            if time.monotonic() >= deadline:
                raise ToolError(ErrorCode.TIMEOUT, "no matching window appeared", details=dict(args))
            await asyncio.sleep(WAIT_POLL_INTERVAL_S)

    @registry.handler("desktop.inspect_ui")
    async def inspect_ui(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        max_elements = int(args.get("max_elements") or ctx.config.get("desktop.ui_tree_max_elements"))
        snapshot = await _sta(ctx, provider, provider.get_ui_tree, args.get("hwnd"), max_elements)
        header = _window_line(snapshot.window) if snapshot.window else "unknown window"
        lines = [_element_line(e) for e in snapshot.elements[:OBSERVATION_ELEMENT_LIMIT]]
        more = len(snapshot.elements) - len(lines)
        if more > 0 or snapshot.truncated:
            lines.append(f"... more elements not shown ({more} in data; truncated={snapshot.truncated})")
        texts = _texts_block(snapshot.texts)
        return ToolOutcome(data=snapshot.to_dict(), backend=BACKEND,
                           observation=f"UI of {header}:\n" + (texts + "\n" if texts else "")
                           + ("\n".join(lines) or "(no interactive elements)"))

    @registry.handler("desktop.find_element")
    async def find_element(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        query = ElementQuery(
            window_hwnd=args.get("hwnd"), name=args.get("name"), name_contains=args.get("name_contains"),
            control_type=args.get("control_type"), automation_id=args.get("automation_id"),
            class_name=args.get("class_name"), include_offscreen=args["include_offscreen"],
            max_results=args["max_results"],
        )
        elements = await _sta(ctx, provider, provider.find_element, query)
        return ToolOutcome(data={"elements": [e.to_dict() for e in elements]}, backend=BACKEND,
                           observation="\n".join(_element_line(e) for e in elements) or "No matching element.")

    @registry.handler("desktop.read_element")
    async def read_element(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        content = await _sta(ctx, provider, provider.read_element, args["element_id"])
        text = content.text if content.text is not None else content.value
        return ToolOutcome(
            data={"element": content.element.to_dict(), "value": content.value, "text": content.text,
                  "toggle_state": content.toggle_state, "is_read_only": content.is_read_only},
            observation=f"{_element_line(content.element)} text={text!r} toggle={content.toggle_state}",
            backend=BACKEND,
        )

    @registry.handler("desktop.set_value")
    async def set_value(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        content = await _sta(ctx, provider, provider.write_element, args["element_id"], args["value"])
        matches = (content.value or "") == args["value"]
        return ToolOutcome(data={"value": content.value},
                           checks=[Check("value_read_back", matches, f"read back {content.value!r}")],
                           observation=f"Set {args['element_id']} to {args['value']!r}", backend=BACKEND)

    @registry.handler("desktop.click")
    async def click(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        element = None
        if "element_id" in args:
            element = (await _sta(ctx, provider, provider.describe_element, args["element_id"])).to_dict()
            if args["button"] == "left" and args["count"] == 1 and await _sta(ctx, provider, provider.try_invoke, args["element_id"]):
                # Pressed through UI Automation: the pointer never moved.
                return ToolOutcome(data={"element": element, "method": "uia_invoke"}, backend=BACKEND,
                                   observation=f"Pressed {args['element_id']} through UI Automation (no mouse). "
                                   + await _foreground_note(ctx, provider))
            await _sta(ctx, provider, provider.bring_to_front, await _sta(ctx, provider, provider.element_window, args["element_id"]))
            x, y = await _sta(ctx, provider, provider.element_center, args["element_id"])
        else:
            x, y = args["x"], args["y"]
            await _sta(ctx, provider, provider.bring_point_window_to_front, x, y)
            # What is under the point decides what this click does: tell the planner, so a vision hit that was a few
            # pixels off shows up as "no named control" instead of passing as success.
            under = await _sta(ctx, provider, provider.element_at, x, y)
            under_line = pointer_line(x, y, under)
        if args["count"] == 2:
            await _sta(ctx, provider, provider.double_click, x, y)
        else:
            await _sta(ctx, provider, provider.click, x, y, args["button"])
        data: dict[str, Any] = {"x": x, "y": y, "method": "mouse"}
        if element is not None:
            data["element"] = element
        else:
            data["under_pointer"] = under
        what = f"{args['element_id']} at ({x},{y})" if element else f"({x},{y})"
        return ToolOutcome(data=data, backend=BACKEND,
                           observation=f"{args['button']} click x{args['count']} on {what}. "
                           + (under_line + " " if element is None else "")
                           + await _foreground_note(ctx, provider))

    @registry.handler("desktop.move_mouse")
    async def move_mouse(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        x, y = await _sta(ctx, provider, provider.move_mouse, args["x"], args["y"])
        ok = (x, y) == (args["x"], args["y"])
        return ToolOutcome(data={"x": x, "y": y}, checks=[Check("cursor_at_target", ok, f"cursor at ({x},{y})")],
                           observation=f"Mouse at ({x},{y}).", backend=BACKEND)

    @registry.handler("desktop.drag")
    async def drag(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        await _sta(ctx, provider, provider.drag, (args["start_x"], args["start_y"]), (args["end_x"], args["end_y"]),
                   args["duration_ms"] / 1000.0)
        return ToolOutcome(data={}, backend=BACKEND,
                           observation=f"Dragged ({args['start_x']},{args['start_y']}) -> ({args['end_x']},{args['end_y']}).")

    @registry.handler("desktop.scroll")
    async def scroll(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        await _sta(ctx, provider, provider.scroll, args.get("x"), args.get("y"), args["direction"], args["amount"])
        return ToolOutcome(data={}, backend=BACKEND, observation=f"Scrolled {args['direction']} x{args['amount']}.")

    @registry.handler("desktop.type")
    async def type_text(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        element_id = args.get("element_id")
        if args["clear"] and not element_id:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "clear=true requires element_id")
        if element_id:
            await _sta(ctx, provider, provider.focus_element, element_id)
            if args["clear"]:
                await _sta(ctx, provider, provider.hotkey, ["ctrl", "a"])
                await _sta(ctx, provider, provider.press_key, "delete", 1)
        await _sta(ctx, provider, provider.type_text, args["text"])
        if args["press_enter"]:
            await _sta(ctx, provider, provider.press_key, "enter", 1)
        checks: list[Check] = []
        value_after: str | None = None
        focused: dict[str, Any] | None = None
        if element_id:
            content = await _sta(ctx, provider, provider.read_element, element_id)
            value_after = content.text if content.text is not None else content.value
        else:
            # No element named: verify against whatever control has the focus, when it exposes its text.
            focused = await _sta(ctx, provider, provider.focused_control_info)
            if focused:
                value_after = focused.get("text") if focused.get("text") is not None else focused.get("value")
        if value_after is not None:
            expected = args["text"].replace("\n", "\r").strip() if not args["press_enter"] else args["text"].strip()
            normalized = value_after.replace("\r\n", "\r").replace("\n", "\r")
            present = expected.replace("\n", "\r") in normalized
            where = element_id or f"focused {focused.get('control_type')} '{focused.get('name')}'"
            checks.append(Check("text_present", present, f"{where} now holds {len(value_after)} chars"
                                + ("" if present else f"; it reads {value_after[:160]!r}")))
        return ToolOutcome(data={"typed_chars": len(args["text"]), "value_after": value_after, "focused": focused},
                           checks=checks, backend=BACKEND,
                           observation=f"Typed {len(args['text'])} characters into "
                           + (f"{element_id}. " if element_id else f"{_focused_line(focused)}. ")
                           + await _foreground_note(ctx, provider))

    @registry.handler("desktop.press_key")
    async def press_key(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        await _sta(ctx, provider, provider.press_key, args["key"], args["repeat"])
        return ToolOutcome(data={}, backend=BACKEND, observation=f"Pressed {args['key']} x{args['repeat']}. "
                           + await _foreground_note(ctx, provider))

    @registry.handler("desktop.hotkey")
    async def hotkey(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        await _sta(ctx, provider, provider.hotkey, args["keys"])
        return ToolOutcome(data={}, backend=BACKEND, observation=f"Pressed {'+'.join(args['keys'])}. "
                           + await _foreground_note(ctx, provider))

    @registry.handler("desktop.read_window_text")
    async def read_window_text(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        hwnd = args.get("hwnd")
        if hwnd is None:
            window = await _sta(ctx, provider, provider.foreground_window)
            hwnd = window.hwnd if window else None
        if hwnd is None:
            raise ToolError(ErrorCode.WINDOW_NOT_FOUND, "no window to read")
        info = await _sta(ctx, provider, win32.window_info, hwnd)
        max_items = int(args.get("max_items") or 80)
        texts = await _sta(ctx, provider, provider.window_texts, hwnd, max_items, 6000, max(2.0, min(10.0, ctx.remaining_s() - 1)))
        focused = await _sta(ctx, provider, provider.focused_control_info)
        body = "\n".join(f"- {t}" for t in texts) or "(no text exposed by UI Automation; try vision.ocr on this window)"
        return ToolOutcome(data={"window": info.to_dict(), "texts": texts, "focused": focused}, backend=BACKEND,
                           observation=f"Text in {_window_line(info)}:\n{body}\n{_focused_line(focused)}")

    @registry.handler("desktop.save_as")
    async def save_as(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        path = Path(args["path"])
        timeout = max(5.0, min(30.0, ctx.remaining_s() - 2))
        result = await _sta(ctx, provider, provider.save_as, args["hwnd"], path, bool(args.get("overwrite")), timeout)
        return ToolOutcome(data=result, backend=BACKEND,
                           checks=[Check("file_written", True, f"{result['path']} ({result['size']} bytes)")],
                           observation=f"Saved through the '{result['dialog_title']}' dialog ({result['hotkey']}) to "
                                       f"{result['path']} ({result['size']} bytes); window title now '{result['window_title']}'.")

    @registry.handler("desktop.observe")
    async def observe(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        max_elements = int(args.get("max_elements") or 80)
        path = ctx.artifact_dir("screenshots") / f"{ctx.step_id or ctx.request_id}-observe-{int(time.time() * 1000)}.png"
        data = await _sta(ctx, provider, provider.observe, path, max_elements)
        shot = data.pop("screenshot", None)
        lines = []
        if data["working"]:
            w = data["working"]
            lines.append(f"Working window: hwnd={w['hwnd']} '{w['title']}' [{w['process_name']}] {w['state']}"
                         + (" (foreground)" if w["is_foreground"] else ""))
        if data["foreground"] and (not data["working"] or data["foreground"]["hwnd"] != data["working"]["hwnd"]):
            f = data["foreground"]
            lines.append(f"In front: hwnd={f['hwnd']} '{f['title']}' [{f['process_name']}]")
        if data["surprise"]:
            lines.append(f"ATTENTION: {data['surprise']}")
        if data.get("no_change"):
            lines.append(data["no_change"])
        lines.append(_focused_line(data["focused"]))
        texts = _texts_block(data["texts"], 25)
        if texts:
            lines.append(texts)
        elements = data["elements"]
        shown = []
        for e in elements[:60]:
            value = f" value='{str(e.get('value'))[:OBSERVATION_VALUE_CHARS]}'" if e.get("value") else ""
            state = f" [{e['state']}]" if e.get("state") else ""
            shown.append(f"{e['element_id']} {e['control_type'].removesuffix('Control')} '{e['name']}'{value} @({e['center']['x']},{e['center']['y']}){state}")
        if shown:
            lines.append(f"Controls ({len(elements)}):\n" + "\n".join(shown) + ("\n..." if len(elements) > 60 else ""))
        artifacts = [{"kind": "screenshot", "path": str(shot.path)}] if shot else []
        data["screenshot"] = str(shot.path) if shot else None
        return ToolOutcome(data=data, backend=BACKEND, artifacts=artifacts, observation="\n".join(lines))

    @registry.handler("desktop.screenshot")
    async def screenshot(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await ctx.services.desktop()
        region = Rect(**args["region"]) if args.get("region") else None
        path = ctx.artifact_dir("screenshots") / f"{ctx.step_id or ctx.request_id}-{int(time.time() * 1000)}.png"
        shot = await _sta(ctx, provider, provider.take_screenshot, path, region, args.get("hwnd"))
        exists = shot.path.is_file() and shot.path.stat().st_size > 0
        return ToolOutcome(
            data={"path": str(shot.path), "width": shot.width, "height": shot.height,
                  "origin_x": shot.origin_x, "origin_y": shot.origin_y},
            checks=[Check("file_written", exists, str(shot.path))],
            artifacts=[{"kind": "screenshot", "path": str(shot.path)}],
            observation=f"Screenshot {shot.width}x{shot.height} saved to {shot.path} "
                        f"(image origin = screen ({shot.origin_x},{shot.origin_y})).",
            backend=BACKEND,
        )
