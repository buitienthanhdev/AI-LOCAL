"""Handlers for the vision.* tools (contract: tools/vision.json)."""

from __future__ import annotations

from typing import Any

from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.vision.provider import VisionProvider
from lga_worker.windows.provider import Rect


async def _provider(ctx: ToolContext) -> VisionProvider:
    desktop = await ctx.services.desktop()

    async def create() -> VisionProvider:
        return VisionProvider(ctx.config, desktop)

    return await ctx.services.get_or_create("vision", create)


def register(registry: ToolRegistry) -> None:
    @registry.handler("vision.status")
    async def status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        ready, info, summary = await provider.status()
        return ToolOutcome(data={"ready": ready, **info}, observation=summary, backend=provider.backend_name)

    @registry.handler("vision.ocr")
    async def read_text(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        region = Rect(**args["region"]) if args.get("region") else None
        lines, language, shot = await provider.read_text(ctx.artifact_dir("screenshots"), ctx.step_id or ctx.request_id,
                                                         args.get("hwnd"), region,
                                                         prefer_vietnamese=bool(args.get("vietnamese")))
        needle = (args.get("find_text") or "").lower()
        rows = []
        for line in lines:
            if needle and needle not in line.text.lower():
                continue
            left, top, right, bottom = line.box
            sx, sy = shot.origin_x, shot.origin_y
            rows.append({"text": line.text, "left": int(sx + left), "top": int(sy + top), "right": int(sx + right),
                         "bottom": int(sy + bottom), "center_x": int(sx + (left + right) / 2),
                         "center_y": int(sy + (top + bottom) / 2)})
            if len(rows) >= args["max_lines"]:
                break
        listing = "\n".join(f"'{r['text']}' @({r['center_x']},{r['center_y']})" for r in rows)
        summary = (f"OCR ({language}) found {len(rows)} line(s)" + (f" containing {args['find_text']!r}" if needle else "")
                   + (":\n" + listing if rows else "."))
        return ToolOutcome(data={"language": language, "lines": rows, "screenshot": str(shot.path)},
                           artifacts=[{"kind": "screenshot", "path": str(shot.path)}], observation=summary,
                           backend="windows-ocr")

    @registry.handler("vision.locate")
    async def locate(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        located = await provider.locate(ctx.artifact_dir("screenshots"), ctx.step_id or ctx.request_id,
                                        args["description"], args.get("hwnd"))
        screen = located.image.screen
        box = None
        if located.grounding.box is not None:
            x1, y1, x2, y2 = located.grounding.box
            box = {"left": screen.left + round(x1 * screen.width), "top": screen.top + round(y1 * screen.height),
                   "right": screen.left + round(x2 * screen.width), "bottom": screen.top + round(y2 * screen.height)}
        inside = screen.contains(located.x, located.y)
        element = located.element_under_point
        under = (f"{element['control_type'].removesuffix('Control')} '{element['name']}'" if element else "nothing")
        return ToolOutcome(
            data={"x": located.x, "y": located.y, "label": located.grounding.label,
                  "confidence": located.grounding.confidence, "backend": located.grounding.backend,
                  "screenshot": str(located.image.path), "box": box, "element_under_point": element},
            checks=[Check("point_inside_captured_area", inside, f"({located.x},{located.y}) in {screen.to_dict()}")],
            artifacts=[{"kind": "screenshot", "path": str(located.image.path)}],
            observation=f"Located {args['description']!r} at ({located.x},{located.y}) via {located.grounding.backend}"
                        f" (label {located.grounding.label!r}); UI Automation element under the point: {under}.",
            backend=located.grounding.backend,
        )

    @registry.handler("vision.describe")
    async def describe(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        answer, backend, screenshot = await provider.describe(ctx.artifact_dir("screenshots"),
                                                              ctx.step_id or ctx.request_id, args["question"],
                                                              args.get("hwnd"))
        return ToolOutcome(data={"answer": answer, "backend": backend, "screenshot": str(screenshot)},
                           artifacts=[{"kind": "screenshot", "path": str(screenshot)}],
                           observation=f"Vision answer: {answer}", backend=backend)
