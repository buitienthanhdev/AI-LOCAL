"""Handlers for the autocad.* tools (contract: tools/autocad.json).

Every COM call runs on the AutoCAD provider's own STA thread (`AutoCadProvider.executor`). Handlers only shape
arguments, give the provider a deadline that ends before the STA timeout (so the provider fails with its own
deterministic error rather than having its thread abandoned), and turn provider facts into data, Check()s and a
short factual observation. State-changing tools are `verification: "self"`: their checks are computed here from
what AutoCAD reported back after the change.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lga_worker import inventory
from lga_worker.autocad import geometry
from lga_worker.autocad.provider import (
    ACAD_IMAGE_NAME,
    DXF_HEAD_BYTES,
    PDF_MAGIC,
    AutoCadProvider,
    CreateResult,
    looks_like_dxf,
    same_file_path,
)
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry
from lga_worker.paths import absolute_path

BACKEND = "autocad-com"
SERVICE_NAME = "autocad"
# The provider's own deadline ends this long before the STA executor's timeout.
STA_TIMEOUT_MARGIN_S = 1.0
MIN_STA_TIMEOUT_S = 0.5
OBSERVATION_ENTITY_LIMIT = 50


async def _provider(ctx: ToolContext) -> AutoCadProvider:
    async def create() -> AutoCadProvider:
        return AutoCadProvider(ctx.config)

    return await ctx.services.get_or_create(SERVICE_NAME, create)


async def _sta(ctx: ToolContext, provider: AutoCadProvider, fn: Callable[..., Any], *args: Any,
               budget_s: float | None = None) -> Any:
    """Run a provider method on the AutoCAD STA thread with a deadline bounded by the request and `budget_s`."""
    ctx.check_cancelled()
    timeout = ctx.remaining_s() if budget_s is None else min(budget_s, ctx.remaining_s())
    timeout = max(MIN_STA_TIMEOUT_S, timeout)
    deadline = time.monotonic() + max(0.0, timeout - STA_TIMEOUT_MARGIN_S)
    return await provider.executor.run(timeout, fn, *args, deadline=deadline)


def _call_budget(ctx: ToolContext) -> float:
    return ctx.config.seconds("autocad.com_call_timeout_ms")


def _num(value: float) -> str:
    return f"{float(value):.6g}"


def _pt(point: Mapping[str, float]) -> str:
    return f"({_num(point['x'])},{_num(point['y'])},{_num(point['z'])})"


def _entity_line(entity: Mapping[str, Any]) -> str:
    name = geometry.short_object_name(entity["object_name"]).capitalize()
    parts = [f"{name} handle={entity['handle']}"]
    if "start" in entity and "end" in entity:
        parts.append(f"start={_pt(entity['start'])} end={_pt(entity['end'])}")
    if "center" in entity:
        parts.append(f"center={_pt(entity['center'])} r={_num(entity['radius'])}")
    if "points" in entity:
        parts.append(f"{entity['vertex_count']} vertices{' closed' if entity['closed'] else ''}")
    if "text" in entity:
        parts.append(f"text={entity['text']!r} at {_pt(entity['insertion'])} h={_num(entity['height'])}")
    parts.append(f"on layer {entity['layer']}")
    return " ".join(parts)


def _output_path(raw: str, suffix: str) -> Path:
    path = absolute_path(raw)
    if path.suffix.lower() != suffix:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"path must end in {suffix}: {raw!r}")
    return path.parent.resolve() / path.name if path.parent.exists() else path


def _file_checks(path: Path) -> tuple[list[Check], int, bytes]:
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    head = b""
    if exists and size:
        with path.open("rb") as fh:
            head = fh.read(DXF_HEAD_BYTES)
    return [Check("file_exists", exists, str(path)), Check("file_not_empty", size > 0, f"{size} bytes")], size, head


def _created(result: CreateResult) -> ToolOutcome:
    entity = result.entity
    detail = (f"{entity['handle']} read back by handle matches {sorted(result.expect)} "
              f"within {result.tolerance:g}")
    return ToolOutcome(data=entity, checks=[Check("read_back_matches", not result.mismatches, detail)],
                       observation=f"{_entity_line(entity)} (verified)", backend=BACKEND)


def register(registry: ToolRegistry) -> None:
    @registry.handler("autocad.status")
    async def status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        prog_id = str(ctx.config.get("autocad.prog_id"))

        def local_facts() -> tuple[list[dict[str, Any]], bool, bool]:
            return (inventory.autocad_installations(), inventory.com_registered(prog_id),
                    inventory.process_running(ACAD_IMAGE_NAME))

        installations, registered, running = await asyncio.to_thread(local_facts)
        data: dict[str, Any] = {
            "installed": bool(installations), "com_registered": registered, "running": running,
            "com_responsive": False, "executable": installations[0]["executable"] if installations else None,
            "version": None, "pid": None, "documents": None, "active_document": None, "reason": None,
        }
        if running:
            provider = await _provider(ctx)
            try:
                probe = await _sta(ctx, provider, provider.probe, budget_s=_call_budget(ctx))
            except ToolError as err:
                if err.code not in (ErrorCode.BUSY, ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.TIMEOUT):
                    raise
                data["reason"] = f"AutoCAD is running but not answering COM ({err.code}): {err.message}"
            else:
                data.update(probe, com_responsive=True)
        elif not installations:
            data["reason"] = "AutoCAD is not installed"
        elif not registered:
            data["reason"] = f"{prog_id} is not registered for COM"
        else:
            data["reason"] = "AutoCAD is not running; autocad.launch starts it"
        data["ready"] = bool(installations) and registered and (not running or data["com_responsive"])
        if data["com_responsive"]:
            summary = (f"AutoCAD {data['version']} ready (pid {data['pid']}): {data['documents']} drawing(s) open, "
                       f"active: {data['active_document'] or 'none'}")
        else:
            summary = f"AutoCAD {'ready' if data['ready'] else 'not ready'}: {data['reason']}"
        return ToolOutcome(data=data, observation=summary, backend=BACKEND)

    @registry.handler("autocad.launch")
    async def launch(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        info = await _sta(ctx, provider, provider.launch)
        data = {"version": info.version, "pid": info.pid, "hwnd": info.hwnd, "launched": info.launched,
                "documents": info.documents, "active_document": info.active_document,
                "outlives_worker": info.outlives_worker}
        how = "Started" if info.launched else "Attached to running"
        note = "" if info.outlives_worker else " (it will close when the agent's worker exits)"
        hint = "" if info.documents else " No drawing open: call autocad.new_document."
        return ToolOutcome(
            data=data,
            checks=[Check("com_responsive", True, f"AutoCAD {info.version} pid={info.pid} answered COM and is idle")],
            observation=f"{how} AutoCAD {info.version} pid={info.pid} hwnd={info.hwnd}; {info.documents} drawing(s) "
                        f"open, active: {info.active_document or 'none'}.{hint}{note}",
            backend=BACKEND,
        )

    @registry.handler("autocad.new_document")
    async def new_document(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        info = await _sta(ctx, provider, provider.new_document, args.get("template"))
        return ToolOutcome(
            data={"name": info.name, "documents": info.documents_after, "template": info.template},
            checks=[Check("document_count_increased", info.documents_after == info.documents_before + 1,
                          f"{info.documents_before} -> {info.documents_after}"),
                    Check("new_document_active", info.active_document == info.name,
                          f"active document is {info.active_document!r}")],
            observation=f"New drawing {info.name} is active ({info.documents_after} open).",
            backend=BACKEND,
        )

    @registry.handler("autocad.list_documents")
    async def list_documents(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        documents = await _sta(ctx, provider, provider.list_documents, budget_s=_call_budget(ctx))
        lines = [f"{'* ' if d['active'] else '  '}{d['name']} {d['full_name'] or '(never saved)'}"
                 f"{'' if d['saved'] else ' [unsaved changes]'}" for d in documents]
        return ToolOutcome(data={"documents": documents}, backend=BACKEND,
                           observation="\n".join(lines) or "No drawings open.")

    @registry.handler("autocad.get_entities")
    async def get_entities(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        entities, total, truncated = await _sta(ctx, provider, provider.get_entities, args.get("object_name"),
                                                args.get("layer"), int(args["limit"]))
        lines = [_entity_line(e) for e in entities[:OBSERVATION_ENTITY_LIMIT]]
        if len(entities) > len(lines) or truncated:
            lines.append(f"... more not shown (returned {len(entities)}, limit reached={truncated})")
        header = f"{len(entities)} matching of {total} entities in model space"
        return ToolOutcome(data={"entities": entities, "total_in_model_space": total, "truncated": truncated},
                           observation=header + (":\n" + "\n".join(lines) if lines else "."), backend=BACKEND)

    @registry.handler("autocad.create_line")
    async def create_line(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        return _created(await _sta(ctx, provider, provider.create_line, args["start"], args["end"], args.get("layer"),
                                   budget_s=_call_budget(ctx)))

    @registry.handler("autocad.create_circle")
    async def create_circle(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        return _created(await _sta(ctx, provider, provider.create_circle, args["center"], float(args["radius"]),
                                   args.get("layer"), budget_s=_call_budget(ctx)))

    @registry.handler("autocad.create_polyline")
    async def create_polyline(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        return _created(await _sta(ctx, provider, provider.create_polyline, args["points"], bool(args["closed"]),
                                   args.get("layer"), budget_s=_call_budget(ctx)))

    @registry.handler("autocad.create_text")
    async def create_text(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        return _created(await _sta(ctx, provider, provider.create_text, args["insertion"], args["text"],
                                   float(args["height"]), float(args["rotation_deg"]), args.get("layer"),
                                   budget_s=_call_budget(ctx)))

    @registry.handler("autocad.create_layer")
    async def create_layer(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        info = await _sta(ctx, provider, provider.create_layer, args["name"], args.get("color"),
                          budget_s=_call_budget(ctx))
        checks = [Check("layer_exists", info.exists, f"layer {info.name!r}")]
        if args.get("color") is not None:
            checks.append(Check("color_matches", info.color == args["color"], f"ACI {info.color}"))
        return ToolOutcome(
            data={"name": info.name or args["name"], "color": info.color if info.color is not None else 0,
                  "created": info.created},
            checks=checks,
            observation=f"Layer {info.name} {'created' if info.created else 'already existed'}, color ACI {info.color} "
                        "(verified).",
            backend=BACKEND,
        )

    @registry.handler("autocad.verify_geometry")
    async def verify_geometry(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        handle = args["handle"].upper()
        result = await _sta(ctx, provider, provider.verify_geometry, handle, args["expect"],
                            budget_s=_call_budget(ctx))
        if result.actual is None:
            summary = f"No live entity with handle {handle}: matches=false."
        elif result.matches:
            summary = f"{_entity_line(result.actual)} matches {sorted(args['expect'])}: matches=true."
        else:
            summary = (f"{_entity_line(result.actual)}: matches=false; "
                       f"{geometry.describe_mismatches(result.mismatches)}")
        return ToolOutcome(data={"matches": result.matches, "handle": handle, "actual": result.actual,
                                 "mismatches": result.mismatches, "tolerance": result.tolerance},
                           observation=summary, backend=BACKEND)

    @registry.handler("autocad.save")
    async def save(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".dwg")
        result = await _sta(ctx, provider, provider.save, path, bool(args["overwrite"]))
        checks, size, _ = await asyncio.to_thread(_file_checks, path)
        associated = same_file_path(result.full_name, path) if path.exists() else False
        checks.append(Check("document_path_is_target", associated, f"drawing path is {result.full_name!r}"))
        return ToolOutcome(data={"path": str(path), "size": size}, checks=checks,
                           artifacts=[{"kind": "file", "path": str(path)}],
                           observation=f"Saved drawing to {path} ({size} bytes, verified).", backend=BACKEND)

    @registry.handler("autocad.export_dxf")
    async def export_dxf(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".dxf")
        result = await _sta(ctx, provider, provider.export_dxf, path, bool(args["overwrite"]))
        checks, size, head = await asyncio.to_thread(_file_checks, path)
        checks.append(Check("dxf_header", looks_like_dxf(head), f"starts with {head[:16]!r}"))
        if result.expected_acadver is not None:
            checks.append(Check("dxf_version", result.acadver == result.expected_acadver,
                                f"$ACADVER {result.acadver}, configured {result.expected_acadver}"))
        still_dwg = Path(result.document_path).suffix.lower() == ".dwg"
        checks.append(Check("document_still_dwg", still_dwg, f"drawing path is {result.document_path!r}"))
        checks.append(Check("temporary_selection_set_removed", result.cleanup_error is None,
                            result.cleanup_error or "removed"))
        return ToolOutcome(data={"path": str(path), "size": size, "document_path": result.document_path,
                                 "dxf_version": result.acadver},
                           checks=checks, artifacts=[{"kind": "file", "path": str(path)}],
                           observation=f"Exported DXF ({result.acadver}) to {path} ({size} bytes, verified); "
                                       f"drawing still "
                                       f"{result.document_path}.", backend=BACKEND)

    @registry.handler("autocad.export_pdf")
    async def export_pdf(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".pdf")
        result = await _sta(ctx, provider, provider.export_pdf, path, bool(args["overwrite"]))
        checks, size, head = await asyncio.to_thread(_file_checks, path)
        checks.append(Check("pdf_header", head.startswith(PDF_MAGIC), f"starts with {head[:8]!r}"))
        checks.append(Check("layout_settings_restored", not result.restore_errors,
                            "; ".join(result.restore_errors) or f"layout {result.layout} restored"))
        return ToolOutcome(
            data={"path": str(path), "size": size, "device": result.device, "media": result.media,
                  "layout": result.layout},
            checks=checks, artifacts=[{"kind": "file", "path": str(path)}],
            observation=f"Plotted layout {result.layout} (extents, fit, centred, {result.media}) to {path} "
                        f"({size} bytes, verified).",
            backend=BACKEND,
        )

    @registry.handler("autocad.zoom_extents")
    async def zoom_extents(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        document = await _sta(ctx, provider, provider.zoom_extents, budget_s=_call_budget(ctx))
        return ToolOutcome(data={"document": document},
                           checks=[Check("zoom_returned", True, f"ZoomExtents completed on {document}")],
                           observation=f"Zoomed {document} to extents.", backend=BACKEND)

    @registry.handler("autocad.erase")
    async def erase(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        handle = args["handle"].upper()
        result = await _sta(ctx, provider, provider.erase, handle, budget_s=_call_budget(ctx))
        return ToolOutcome(
            data={"handle": handle, "object_name": result.object_name, "erased": result.gone},
            checks=[Check("entity_gone", result.gone,
                          f"handle {handle} {'no longer resolves' if result.gone else 'still resolves'}")],
            observation=f"Erased {result.object_name} handle={handle} (verified).", backend=BACKEND,
        )
