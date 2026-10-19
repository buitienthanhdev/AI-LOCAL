"""Handlers for the autocad.* tools (contract: tools/autocad.json).

Every COM call runs on the AutoCAD provider's own STA thread (`AutoCadProvider.executor`). Handlers only shape
arguments, give the provider a deadline that ends before the STA timeout (so the provider fails with its own
deterministic error rather than having its thread abandoned), and turn provider facts into data, Check()s and a
short factual observation. State-changing tools are `verification: "self"`: their checks are computed here from
what AutoCAD reported back after the change.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lga_worker import inventory
from lga_worker.autocad import floorplan, geometry, housemodel, houseviews, pdfcheck, safety, spec_tools
from lga_worker.autocad import layouts as layouts_mod
from lga_worker.autocad import layers as layers_mod
from lga_worker.autocad.provider import (
    ACAD_IMAGE_NAME,
    PLAN_LAYERS,
    DXF_HEAD_BYTES,
    PDF_MAGIC,
    AutoCadProvider,
    CreateResult,
    PrimsResult,
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
               budget_s: float | None = None, **kwargs: Any) -> Any:
    """Run a provider method on the AutoCAD STA thread with a deadline bounded by the request and `budget_s`."""
    ctx.check_cancelled()
    timeout = ctx.remaining_s() if budget_s is None else min(budget_s, ctx.remaining_s())
    timeout = max(MIN_STA_TIMEOUT_S, timeout)
    deadline = time.monotonic() + max(0.0, timeout - STA_TIMEOUT_MARGIN_S)
    return await provider.executor.run(timeout, fn, *args, deadline=deadline, **kwargs)


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


def provider_layers() -> list[tuple[str, int]]:
    return list(PLAN_LAYERS.values())


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


def _journal_path(ctx: ToolContext) -> Path | None:
    try:
        return ctx.artifact_dir("autocad") / "operations.jsonl"
    except OSError:
        return None


async def _guarded_write(ctx: ToolContext, kind: safety.OpKind, path: Path, overwrite: bool,
                         run: Callable[[], Any]) -> tuple[Any, safety.OperationRecord]:
    """Run a file-writing provider call with an operation record and, when it overwrites, a verified backup first.

    A failed write restores the backup if the target changed. Journal problems never fail the tool.
    """
    journal = safety.OperationJournal(_journal_path(ctx))
    inputs = {"path": str(path), "overwrite": overwrite}
    record = safety.new_record(kind, safety.Mode.APPLY, inputs, [str(path)])
    journal.add(record)
    try:
        if overwrite and path.is_file():
            try:
                record.backup = await asyncio.to_thread(safety.backup_file, path)
            except (safety.SafetyError, OSError) as exc:
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                f"refusing to overwrite {path}: its backup could not be made ({exc})") from exc
            record.note(f"backup {record.backup.backup if record.backup else '?'}")
        try:
            result = await run()
        except BaseException as exc:
            record.status = safety.OpStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            if record.backup is not None:
                try:
                    if not path.is_file() or safety.sha256_file(path) != record.backup.sha256:
                        safety.restore_backup(record.backup)
                        record.status = safety.OpStatus.ROLLED_BACK
                        record.note("target restored from backup")
                except (safety.SafetyError, OSError) as restore_error:
                    record.note(f"restore failed: {restore_error}")
            raise
        record.status = safety.OpStatus.APPLIED
        return result, record
    finally:
        record.finished = safety._now()
        journal.flush(record)


def _write_extras(record: safety.OperationRecord) -> tuple[dict[str, Any], list[Check]]:
    data: dict[str, Any] = {"operation_id": record.op_id}
    checks: list[Check] = []
    if record.backup is not None:
        data["backup"] = record.backup.to_dict()
        checks.append(Check("backup_before_overwrite", Path(record.backup.backup).is_file(),
                            f"{record.backup.backup} sha256 {record.backup.sha256[:12]}"))
    return data, checks


def _created(result: CreateResult) -> ToolOutcome:
    entity = result.entity
    detail = (f"{entity['handle']} read back by handle matches {sorted(result.expect)} "
              f"within {result.tolerance:g}")
    return ToolOutcome(data=entity, checks=[Check("read_back_matches", not result.mismatches, detail)],
                       observation=f"{_entity_line(entity)} (verified)", backend=BACKEND)


# ------------------------------------------------------------------ house model and its views (elevation, section, schedules)

def _house(args: Mapping[str, Any]) -> housemodel.HouseModel:
    """The one shared model, built from the same parameters as autocad.draw_floor_plan plus the vertical numbers."""
    try:
        return housemodel.build_house_model(housemodel.HouseParams.from_args(args))
    except ValueError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc


def _layer_map(args: Mapping[str, Any]) -> layers_mod.LayerMap:
    try:
        return layers_mod.LayerMap.from_dict(args.get("layer_map"))
    except layers_mod.LayerMapError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc


def _xy(raw: Mapping[str, Any] | None, default: tuple[float, float]) -> tuple[float, float]:
    if not raw:
        return default
    return float(raw.get("x", default[0])), float(raw.get("y", default[1]))


VOLATILE_ARGS = frozenset({"new_document", "if_exists", "export_path", "overwrite", "idempotency_key"})


def _op_inputs(tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """The inputs that identify a drawing operation (what it draws and where, not how a repeat is handled)."""
    return {"tool": tool, "args": {k: v for k, v in args.items() if k not in VOLATILE_ARGS}}


HOUSE_ARG_KEYS = tuple(housemodel.HouseParams.__dataclass_fields__)


def _view_marker(args: Mapping[str, Any], view_id: str, origin: tuple[float, float] | None = None,
                 extra: Mapping[str, Any] | None = None) -> str:
    """Marker written (as XData) on every entity of one view. It is the hash of WHAT is drawn (the house parameters, the
    view id, where, the layer map), not of which tool drew it: an identical second call, or another tool that draws the
    same view in the same place, finds the entities and adds nothing. An explicit idempotency_key replaces the hash."""
    explicit = str(args.get("idempotency_key") or "").strip()
    if explicit:
        return f"{safety.marker(explicit[:80])}:{view_id}"
    inputs: dict[str, Any] = {"view": view_id, "origin": [round(origin[0], 6), round(origin[1], 6)] if origin else None,
                              "house": {k: args[k] for k in HOUSE_ARG_KEYS if args.get(k) is not None},
                              "layer_map": dict(args.get("layer_map") or {}), **dict(extra or {})}
    return f"{safety.marker(safety.idempotency_key(safety.OpKind.CREATE, inputs))}:{view_id}"


async def _recorded(ctx: ToolContext, kind: safety.OpKind, inputs: Mapping[str, Any], affected: list[str],
                    run: Callable[[], Any]) -> tuple[Any, safety.OperationRecord]:
    """Run a drawing operation under an operation record in the journal (no file is overwritten, so no backup)."""
    journal = safety.OperationJournal(_journal_path(ctx))
    record = safety.new_record(kind, safety.Mode.APPLY, dict(inputs), affected)
    journal.add(record)
    try:
        result = await run()
        record.status = safety.OpStatus.APPLIED
        return result, record
    except BaseException as exc:
        record.status = safety.OpStatus.FAILED
        record.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record.finished = safety._now()
        journal.flush(record)


async def _draw_view(ctx: ToolContext, provider: AutoCadProvider, model: housemodel.HouseModel,
                     view: houseviews.View, origin: tuple[float, float], layer_map: layers_mod.LayerMap,
                     new_document: bool, marker: str | None = None, if_exists: str = "skip"
                     ) -> tuple[PrimsResult, dict[str, Any], list[Check]]:
    names = set(view.layers_used())
    if any(p.kind == "cut" for p in view.prims):
        names.add("A-HATCH")
    try:
        layers = layers_mod.view_layers(sorted(names), layer_map)
    except layers_mod.LayerMapError as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
    result = await _sta(ctx, provider, provider.draw_prims, view.prims, layers, origin, model.text_height, new_document,
                        marker=marker, if_exists=if_exists, expect=view.expected_entities())
    expected = view.expected_entities()
    added = result.entities_after - result.entities_before
    drawn = sum(result.counts.values())
    x0, y0, x1, y1 = view.bbox
    data = {**view.record, "origin": [origin[0], origin[1]], "extent": {"min": [x0 + origin[0], y0 + origin[1]],
                                                                       "max": [x1 + origin[0], y1 + origin[1]]},
            "entities": drawn, "layers": sorted(result.by_layer),
            "idempotency": {"marker": marker, "skipped": result.skipped, "reused": result.reused,
                            "replaced": result.replaced}}
    if result.skipped:
        checks = [Check(f"{view.id}_already_drawn", result.reused == expected,
                        f"{result.reused} entities of the earlier identical call found, {expected} expected; nothing added")]
    else:
        checks = [Check(f"{view.id}_entities_drawn", drawn == expected and added == expected,
                        f"{drawn} created / {added} added to model space, {expected} expected"),
                  Check(f"{view.id}_hatches_ok", not result.hatch_failures,
                        "; ".join(result.hatch_failures) or f"{result.counts.get('hatch', 0)} hatches evaluated")]
    return result, data, checks


def _row_x(origin: tuple[float, float] | None, widths: list[float], index: int, default: tuple[float, float]
           ) -> tuple[float, float]:
    """Origin of the index-th view when the caller gave one origin for a row of views."""
    if origin is None:
        return default
    return origin[0] + sum(w + 14.0 for w in widths[:index]), origin[1]


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
        result, op = await _guarded_write(ctx, safety.OpKind.SAVE, path, bool(args["overwrite"]),
                                          lambda: _sta(ctx, provider, provider.save, path, bool(args["overwrite"])))
        extra, extra_checks = _write_extras(op)
        checks, size, _ = await asyncio.to_thread(_file_checks, path)
        checks += extra_checks
        associated = same_file_path(result.full_name, path) if path.exists() else False
        checks.append(Check("document_path_is_target", associated, f"drawing path is {result.full_name!r}"))
        return ToolOutcome(data={"path": str(path), "size": size, **extra}, checks=checks,
                           artifacts=[{"kind": "file", "path": str(path)}],
                           observation=f"Saved drawing to {path} ({size} bytes, verified).", backend=BACKEND)

    @registry.handler("autocad.export_dxf")
    async def export_dxf(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".dxf")
        result, op = await _guarded_write(ctx, safety.OpKind.EXPORT, path, bool(args["overwrite"]),
                                          lambda: _sta(ctx, provider, provider.export_dxf, path, bool(args["overwrite"])))
        extra, extra_checks = _write_extras(op)
        checks, size, head = await asyncio.to_thread(_file_checks, path)
        checks += extra_checks
        checks.append(Check("dxf_header", looks_like_dxf(head), f"starts with {head[:16]!r}"))
        if result.expected_acadver is not None:
            checks.append(Check("dxf_version", result.acadver == result.expected_acadver,
                                f"$ACADVER {result.acadver}, configured {result.expected_acadver}"))
        still_dwg = Path(result.document_path).suffix.lower() == ".dwg"
        checks.append(Check("document_still_dwg", still_dwg, f"drawing path is {result.document_path!r}"))
        checks.append(Check("temporary_selection_set_removed", result.cleanup_error is None,
                            result.cleanup_error or "removed"))
        return ToolOutcome(data={"path": str(path), "size": size, "document_path": result.document_path,
                                 "dxf_version": result.acadver, **extra},
                           checks=checks, artifacts=[{"kind": "file", "path": str(path)}],
                           observation=f"Exported DXF ({result.acadver}) to {path} ({size} bytes, verified); "
                                       f"drawing still "
                                       f"{result.document_path}.", backend=BACKEND)

    @registry.handler("autocad.export_pdf")
    async def export_pdf(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".pdf")
        result, op = await _guarded_write(ctx, safety.OpKind.EXPORT, path, bool(args["overwrite"]),
                                          lambda: _sta(ctx, provider, provider.export_pdf, path, bool(args["overwrite"])))
        extra, extra_checks = _write_extras(op)
        checks, size, head = await asyncio.to_thread(_file_checks, path)
        checks += extra_checks
        checks.append(Check("pdf_header", head.startswith(PDF_MAGIC), f"starts with {head[:8]!r}"))
        checks.append(Check("layout_settings_restored", not result.restore_errors,
                            "; ".join(result.restore_errors) or f"layout {result.layout} restored"))
        return ToolOutcome(
            data={"path": str(path), "size": size, "device": result.device, "media": result.media,
                  "layout": result.layout, **extra},
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

    @registry.handler("autocad.draw_floor_plan")
    async def draw_floor_plan(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        floors = int(args.get("floors") or 1)
        origin = args.get("origin") or {}
        plan_origin = (float(origin.get("x", 0.0)), float(origin.get("y", 0.0)))
        if_exists = str(args.get("if_exists", "skip"))
        provider = await _provider(ctx)
        if floors == 1 and not args.get("stair") and not args.get("floor_heights"):
            return await _draw_single_floor_plan(ctx, provider, args, plan_origin, if_exists)
        model = _house(args)
        if model.floors < 2:
            return await _draw_single_floor_plan(ctx, provider, args, plan_origin, if_exists)
        layer_map = _layer_map(args)
        new_document = bool(args.get("new_document", True))

        async def run() -> tuple[list[dict[str, Any]], list[Check], list[dict[str, Any]]]:
            floor_data: list[dict[str, Any]] = []
            checks: list[Check] = []
            records: list[dict[str, Any]] = []
            for n in range(1, model.floors + 1):
                plan_n = housemodel.floor_plan(model, n)
                org = houseviews.floor_plan_origin(model, n, plan_origin)
                result = await _sta(ctx, provider, provider.draw_plan, plan_n, new_document and n == 1, org,
                                    marker=_view_marker(args, f"PLAN-F{n}", org), if_exists=if_exists,
                                    expect=floorplan.expected_entity_count(plan_n))
                counts = result.counts
                expected = floorplan.expected_entity_count(plan_n)
                if result.skipped:
                    checks.append(Check(f"floor_{n}_plan_already_drawn", result.reused == expected,
                                        f"{result.reused} entities of the earlier identical call found, {expected} expected"))
                else:
                    checks.append(Check(f"floor_{n}_walls_drawn", counts.get("wall", 0) == 2 * len(plan_n.walls),
                                        f"{counts.get('wall', 0)} wall entities for {len(plan_n.walls)} wall pieces"))
                    checks.append(Check(f"floor_{n}_labels_drawn", counts.get("text", 0) == len(plan_n.labels) + 2,
                                        f"{counts.get('text', 0)} texts for {len(plan_n.labels)} rooms + title + table"))
                    checks.append(Check(f"floor_{n}_entities_total", sum(counts.values()) == expected,
                                        f"{sum(counts.values())} drawn, {expected} expected"))
                entry: dict[str, Any] = {"floor": n, "level_id": housemodel.floor_level_id(n), "origin": list(org),
                                         "document": result.document, "entities": counts,
                                         "idempotency": {"marker": _view_marker(args, f"PLAN-F{n}", org), "skipped": result.skipped,
                                                         "reused": result.reused, "replaced": result.replaced}}
                view = houseviews.stair_plan_view(model, n)
                record: dict[str, Any] = {"id": f"PLAN-F{n}", "kind": "plan", "code": housemodel.plan_sheet_code(n),
                                          "level_id": housemodel.floor_level_id(n), "floor": n, "stairs": [],
                                          "slab_openings": [], "origin": list(org), "drawn": True}
                if view is not None:
                    _, vdata, vchecks = await _draw_view(ctx, provider, model, view, org, layer_map, False,
                                                         _view_marker(args, view.id, org), if_exists)
                    checks += vchecks
                    record.update({"stairs": vdata["stairs"], "slab_openings": vdata["slab_openings"]})
                    entry["stair_view"] = {"entities": vdata["entities"], "idempotency": vdata["idempotency"]}
                records.append(record)
                floor_data.append(entry)
            if args.get("roof_plan", True):
                roof = houseviews.roof_plan_view(model)
                org = houseviews.floor_plan_origin(model, model.floors + 1, plan_origin)
                _, rdata, rchecks = await _draw_view(ctx, provider, model, roof, org, layer_map, False,
                                                     _view_marker(args, roof.id, org), if_exists)
                checks += rchecks
                records.append({**rdata, "drawn": True})
            return floor_data, checks, records

        (floor_data, checks, records), op = await _recorded(
            ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_floor_plan", args),
            [f"floor-plan:{n}" for n in range(1, model.floors + 1)], run)
        first = model.plan
        rooms = [{"name": r.name, "area_m2": round(r.area, 2)} for r in model.floor_rooms(1) if r.listed]
        stairs = [{"id": s.id, "from_level": s.from_level, "to_level": s.to_level, "total_height": s.total_height,
                   "riser_count": s.riser_count, "riser_height": round(s.total_height / s.riser_count, 4), "tread": s.tread,
                   "width": s.width, "shape": s.shape, "landing": s.landing, "footprint": list(s.footprint),
                   "slab_opening": [list(r) for r in s.opening]} for s in model.stairs]
        skipped = [f for f in floor_data if f["idempotency"]["skipped"]]
        return ToolOutcome(
            data={"document": floor_data[0]["document"], "width": first.width, "depth": first.depth,
                  "area_m2": round(first.area, 2), "floors": floor_data, "views": records, "stairs": stairs,
                  "rooms": rooms, "entities": floor_data[0]["entities"], "operation_id": op.op_id,
                  "layers": [name for name, _ in provider_layers()], "assumptions": model.assumptions},
            checks=checks, backend=BACKEND,
            observation=(f"Drew {model.floors} floor plans of a {first.width:g} x {first.depth:g} m house"
                         + (f" ({len(skipped)} already present from an identical earlier call, nothing duplicated)" if skipped else "")
                         + f" with {len(stairs)} stair(s): "
                         + "; ".join(f"{s['id']} {s['riser_count']} risers of {s['riser_height'] * 1000:.0f} mm, tread "
                                     f"{s['tread'] * 1000:.0f} mm, {s['shape']}" for s in stairs)
                         + ", slab openings on the upper floors and a roof plan. Floors 2.. repeat the ground-floor walls (ASSUMED); "
                           "stair sizes without a verified profile are working defaults, not code limits. Pass data.views to "
                           "autocad.house_model (views)."))

    async def _draw_single_floor_plan(ctx: ToolContext, provider: AutoCadProvider, args: dict[str, Any],
                                      origin: tuple[float, float], if_exists: str) -> ToolOutcome:
        try:
            plan = floorplan.plan_house(width=args.get("width"), depth=args.get("depth"), area=args.get("area"),
                                        bedrooms=args.get("bedrooms"), altar=args.get("altar"), study=args.get("study"),
                                        porch=args.get("porch"))
        except ValueError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        marker = _view_marker(args, "PLAN-F1", origin)

        async def run() -> Any:
            return await _sta(ctx, provider, provider.draw_plan, plan, bool(args.get("new_document", True)), origin,
                              marker=marker, if_exists=if_exists, expect=floorplan.expected_entity_count(plan))

        result, op = await _recorded(ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_floor_plan", args), ["floor-plan:1"], run)
        rooms = [{"name": r.name, "area_m2": round(r.total_area, 2), "width": round(r.width, 2), "depth": round(r.depth, 2)}
                 for r in plan.rooms if r.kind not in ("corridor", "annex")]
        counts = result.counts
        expected = floorplan.expected_entity_count(plan)
        if result.skipped:
            checks = [Check("plan_already_drawn", result.reused == expected,
                            f"{result.reused} entities of the earlier identical call found, {expected} expected")]
        else:
            walls_ok = counts.get("wall", 0) == 2 * len(plan.walls)
            labels_ok = counts.get("text", 0) == len(plan.labels) + 2
            checks = [Check("walls_drawn", walls_ok, f"{counts.get('wall', 0)} wall entities for {len(plan.walls)} wall pieces"),
                      Check("labels_drawn", labels_ok, f"{counts.get('text', 0)} texts for {len(plan.labels)} rooms + title + table")]
        listing = "; ".join(f"{r['name']} {r['area_m2']:g} m²" for r in rooms)
        note = (f"Already present from an identical earlier call ({result.reused} entities found); nothing was added. "
                if result.skipped else "")
        return ToolOutcome(
            data={"document": result.document, "width": plan.width, "depth": plan.depth, "area_m2": round(plan.area, 2),
                  "rooms": rooms, "entities": counts, "layers": [name for name, _ in provider_layers()],
                  "operation_id": op.op_id,
                  "idempotency": {"marker": marker, "skipped": result.skipped, "reused": result.reused,
                                  "replaced": result.replaced}},
            checks=checks,
            observation=(f"{note}Drew a complete {plan.width:g} x {plan.depth:g} m house plan ({plan.area:g} m²) in {result.document}: "
                         f"{listing}. Walls filled, doors with swings, windows, furniture, room labels with areas, "
                         f"dimensions, title and area table; one undo step (layers KT-*)."),
            backend=BACKEND,
        )

    # ------------------------------------------------------------------ house model, elevations, sections, schedules

    async def _export_json(ctx: ToolContext, args: Mapping[str, Any], payload: Mapping[str, Any]
                           ) -> tuple[dict[str, Any], list[Check], list[dict[str, str]]]:
        """Write `payload` to args['export_path'] through the operation record and the backup-before-overwrite guard."""
        if not args.get("export_path"):
            return {}, [], []
        path = _output_path(args["export_path"], ".json")
        overwrite = bool(args.get("overwrite", False))
        if path.is_dir():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is a directory")
        if path.exists() and not overwrite:
            raise ToolError(ErrorCode.CONFLICT, f"{path} exists; pass overwrite=true (a backup is made first)")
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        _, op = await _guarded_write(ctx, safety.OpKind.EXPORT, path, overwrite,
                                     lambda: asyncio.to_thread(safety.atomic_write_text, path, text))
        extra, checks = _write_extras(op)
        size = path.stat().st_size if path.is_file() else 0
        checks.append(Check("export_written", size > 0, f"{path} ({size} bytes)"))
        try:
            json.loads(path.read_text(encoding="utf-8"))
            parsed = True
        except (OSError, ValueError):
            parsed = False
        checks.append(Check("export_is_json", parsed, "read back and parsed"))
        return {"export_path": str(path), "export_size": size, **extra}, checks, [{"kind": "file", "path": str(path)}]

    @registry.handler("autocad.house_model")
    async def house_model(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        model = _house(args)
        extra_meta = {k: args[k] for k in ("layouts", "sheet_index", "pdf_render_check", "reopen_check") if args.get(k) is not None}
        data_model = model.validation_model(list(args.get("views") or []), extra_meta)
        summary = model.summary()
        payload = {"summary": summary, "model": data_model}
        extra, checks, artifacts = await _export_json(ctx, args, payload)
        n_ent = len(data_model["entities"])
        stair_text = ""
        if model.stairs:
            st = model.stairs[0]
            stair_text = (f" {len(model.stairs)} stair(s), e.g. {st.id}: {st.riser_count} risers of "
                          f"{st.total_height / st.riser_count * 1000:.1f} mm (total rise {st.total_height:g} m recomputed from the "
                          f"levels), tread {st.tread * 1000:.0f} mm, {st.shape}.")
        return ToolOutcome(
            data={"summary": summary, "model": data_model, "assumptions": model.assumptions, **extra},
            checks=checks, artifacts=artifacts, backend="offline",
            observation=(f"House model {model.width:g} x {model.depth:g} m, {model.floors} floor(s): {len(model.walls)} wall runs, "
                         f"{len(model.doors)} doors, {len(model.windows)} windows, {len(model.listed_rooms)} listed rooms, "
                         f"{len(model.levels)} levels; {n_ent} entities, {len(data_model['meta']['views'])} view record(s)."
                         + stair_text +
                         f" {len(model.assumptions)} working defaults are flagged ASSUMED (not code requirements). "
                         "Give data.model to autocad.validate_model / autocad.report as 'model'."
                         + (f" Written: {extra['export_path']}." if extra else "")))

    @registry.handler("autocad.draw_elevation")
    async def draw_elevation(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        model = _house(args)
        sides = list(dict.fromkeys(args.get("sides") or ["front"]))
        bad = [s for s in sides if s not in housemodel.SIDES]
        if bad:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"unknown side(s) {bad}; use {list(housemodel.SIDES)}")
        layer_map = _layer_map(args)
        plan_origin = _xy(args.get("plan_origin"), (0.0, 0.0))
        given = _xy(args["origin"], (0.0, 0.0)) if args.get("origin") else None
        views = [houseviews.elevation_view(model, s) for s in sides]
        widths = [v.bbox[2] - v.bbox[0] for v in views]
        provider = await _provider(ctx)
        new_document = bool(args.get("new_document", False))
        if_exists = str(args.get("if_exists", "skip"))

        async def run() -> list[tuple[dict[str, Any], list[Check]]]:
            out = []
            for i, view in enumerate(views):
                origin = _row_x(given, widths, i, houseviews.default_origin(model, sides[i], plan_origin))
                _, data, checks = await _draw_view(ctx, provider, model, view, origin, layer_map,
                                                   new_document and i == 0, _view_marker(args, view.id, origin), if_exists)
                out.append((data, checks))
            return out

        drawn, op = await _recorded(ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_elevation", args),
                                    [f"elevation:{s}" for s in sides], run)
        records = [d for d, _ in drawn]
        checks = [c for _, cs in drawn for c in cs]
        listing = "; ".join(f"{r['id']} ({r['sheet']}) {len(r['openings'])} opening(s), "
                            f"{len(r['levels'])} level marks, {r['entities']} entities"
                            + (" (already present, not redrawn)" if r["idempotency"]["skipped"] else "") for r in records)
        return ToolOutcome(
            data={"views": records, "levels": [dict(id=l.id, name=l.name, elevation=l.elevation, status=l.status.value)
                                              for l in model.levels],
                  "assumptions": model.assumptions, "operation_id": op.op_id},
            checks=checks, backend=BACKEND,
            observation=(f"Drew {len(records)} elevation(s) from the shared house model ({model.floors} floor(s)), model space 1:1, "
                         f"ByLayer on A-* layers: {listing}. Datum +0.000 = floor 1 (ASSUMED convention); heights and sizes not "
                         "given by the user are working defaults flagged ASSUMED. Pass data.views to autocad.house_model "
                         "(views) before validating."))

    @registry.handler("autocad.draw_section")
    async def draw_section(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        model = _house(args)
        raw = args.get("cut") or {}
        default_axis = houseviews.default_cut(model).axis
        cut = houseviews.Cut(axis=str(raw.get("axis", default_axis)), position=raw.get("position"),
                             direction=int(raw.get("direction", 1)), letter=str(raw.get("letter", "A")),
                             sheet=str(raw.get("sheet", houseviews.SECTION_SHEET)))
        try:
            cut = houseviews.check_cut(model, cut)
            view = houseviews.section_view(model, cut)
            marker_view = houseviews.section_marker_view(model, cut) if args.get("draw_marker", True) else None
        except ValueError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        layer_map = _layer_map(args)
        plan_origin = _xy(args.get("plan_origin"), (0.0, 0.0))
        origin = _xy(args.get("origin"), houseviews.default_origin(model, "section", plan_origin))
        provider = await _provider(ctx)
        new_document = bool(args.get("new_document", False))
        if_exists = str(args.get("if_exists", "skip"))

        async def run() -> tuple[dict[str, Any], list[Check], dict[str, Any] | None]:
            _, data, checks = await _draw_view(ctx, provider, model, view, origin, layer_map, new_document,
                                               _view_marker(args, view.id, origin), if_exists)
            marker_data = None
            if marker_view is not None:
                _, marker_data, more = await _draw_view(ctx, provider, model, marker_view, plan_origin, layer_map, False,
                                                        _view_marker(args, marker_view.id, plan_origin), if_exists)
                checks += more
            return data, checks, marker_data

        (data, checks, marker_data), op = await _recorded(
            ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_section", args), [f"section:{cut.code}"], run)
        return ToolOutcome(
            data={"view": data, "marker": marker_data, "operation_id": op.op_id,
                  "cut": data["cut"], "assumptions": model.assumptions},
            checks=checks, backend=BACKEND,
            observation=(f"Drew section {cut.code} cut at {cut.axis}={cut.position:g} looking "
                         f"{'+' if cut.direction > 0 else '-'}{cut.axis if cut.axis == 'y' else 'x'} "
                         f"({data['entities']} entities; {model.floors} floor(s); cut walls {len(data['cut_walls'])}, slabs "
                         f"{data['slabs']}, {len(data['clear_heights'])} clear height(s), {len(data['stairs'])} stair(s)) "
                         + (f"and its marker on the plan at origin {list(plan_origin)} ({marker_data['entities']} entities)"
                            if marker_data else "without a marker")
                         + (" [already present from an identical earlier call, not redrawn]" if data["idempotency"]["skipped"] else "")
                         + ". Model space 1:1, ByLayer on A-* layers. Pass data.view to autocad.house_model (views)."))

    @registry.handler("autocad.draw_schedules")
    async def draw_schedules(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        model = _house(args)
        layer_map = _layer_map(args)
        plan_origin = _xy(args.get("plan_origin"), (0.0, 0.0))
        origin = _xy(args.get("origin"), houseviews.default_origin(model, "schedule", plan_origin))
        views = houseviews.schedule_views(model)
        provider = await _provider(ctx)
        new_document = bool(args.get("new_document", False))
        if_exists = str(args.get("if_exists", "skip"))

        async def run() -> list[tuple[dict[str, Any], list[Check]]]:
            out = []
            for i, view in enumerate(views):
                _, data, checks = await _draw_view(ctx, provider, model, view, origin, layer_map,
                                                   new_document and i == 0, _view_marker(args, view.id, origin), if_exists)
                out.append((data, checks))
            return out

        drawn, op = await _recorded(ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_schedules", args),
                                    [v.id for v in views], run)
        records = [d for d, _ in drawn]
        checks = [c for _, cs in drawn for c in cs]
        rows = {"door": model.door_rows(), "window": model.window_rows(), "room": model.room_rows()}
        total = round(sum(r["area"] for r in rows["room"]), 4)
        extra, more, artifacts = await _export_json(ctx, args, {"schedules": rows, "room_total_area_m2": total,
                                                               "views": records, "assumptions": model.assumptions})
        checks += more
        per_floor = f" per floor ({model.floors} floors, {len(views)} tables)" if model.floors > 1 else ""
        return ToolOutcome(
            data={"views": records, "schedules": rows, "room_total_area_m2": total, "operation_id": op.op_id,
                  "assumptions": model.assumptions, **extra},
            checks=checks, artifacts=artifacts, backend=BACKEND,
            observation=(f"Drew the door ({len(rows['door'])} rows), window ({len(rows['window'])} rows) and room/area "
                         f"({len(rows['room'])} rows, total {total:.2f} m², centre-line rule) tables{per_floor} from the same model as the "
                         "plan symbols; rows are returned in data.schedules and re-read from the drawn cell text in "
                         "data.views[].rows."
                         + (f" Written: {extra['export_path']}." if extra else "")))

    @registry.handler("autocad.draw_stair")
    async def draw_stair(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if int(args.get("floors") or 1) < 2:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "a stair needs floors >= 2")
        model = _house(args)
        floor = int(args["floor"])
        if not 1 <= floor <= model.floors:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"floor must be 1..{model.floors}")
        view = houseviews.stair_plan_view(model, floor)
        assert view is not None
        layer_map = _layer_map(args)
        origin_arg = args.get("origin")
        plan_origin = _xy(args.get("plan_origin"), (0.0, 0.0))
        origin = _xy(origin_arg, houseviews.floor_plan_origin(model, floor, plan_origin))
        provider = await _provider(ctx)
        if_exists = str(args.get("if_exists", "skip"))

        async def run() -> tuple[dict[str, Any], list[Check]]:
            _, data, checks = await _draw_view(ctx, provider, model, view, origin, layer_map,
                                               bool(args.get("new_document", False)), _view_marker(args, view.id, origin), if_exists)
            return data, checks

        (data, checks), op = await _recorded(ctx, safety.OpKind.CREATE, _op_inputs("autocad.draw_stair", args),
                                             [f"stair-plan:{floor}"], run)
        stairs = [s for s in model.summary()["stair_detail"]]
        return ToolOutcome(
            data={"view": data, "stairs": stairs, "floor": floor, "operation_id": op.op_id,
                  "assumptions": model.assumptions},
            checks=checks, backend=BACKEND,
            observation=(f"Drew the stair symbol(s) and slab opening of floor {floor}: "
                         + "; ".join(f"{s['id']} {s['risers']} risers of {s['riser_height'] * 1000:.1f} mm, tread "
                                     f"{s['tread'] * 1000:.0f} mm, width {s['width'] * 1000:.0f} mm, {s['shape']}" for s in stairs)
                         + ". Numbers are recomputed from the floor-to-floor height; sizes not given are working defaults "
                           "(ASSUMED) and no code limit is applied without a verified profile."))

    # ------------------------------------------------------------------ layouts, plotting, re-open check

    @registry.handler("autocad.create_layout")
    async def create_layout(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        model = _house(args)
        paper = str(args.get("paper", "A3")).upper()
        orientation = str(args.get("orientation", "landscape"))
        extents: dict[str, list[float]] = {}
        for rec in args.get("views") or []:
            ext = rec.get("extent") if isinstance(rec, Mapping) else None
            if ext and rec.get("id"):
                extents[str(rec["id"])] = [ext["min"][0], ext["min"][1], ext["max"][0], ext["max"][1]]
        try:
            frame = layouts_mod.frame_for(paper, orientation)
            specs = layouts_mod.fit_all(layouts_mod.sheet_specs(
                model, plan_origin=_xy(args.get("plan_origin"), (0.0, 0.0)), scales=args.get("scales"),
                sheet_codes=args.get("sheet_codes"), extents=extents, include=args.get("sheets")), frame)
            prim_sets = [layouts_mod.frame_prims(s, frame, args.get("title_block")) for s in specs]
        except layouts_mod.LayoutError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        layer_map = _layer_map(args)
        try:
            layers = layers_mod.view_layers([layouts_mod.TTLB, layouts_mod.VIEWPORT_LAYER], layer_map)
        except layers_mod.LayerMapError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        provider = await _provider(ctx)
        if_exists = str(args.get("if_exists", "skip"))
        w, h = frame.width, frame.height
        a, b = (int(w), int(h)) if orientation == "landscape" else (int(h), int(w))
        # full bleed first: the frame lies 5 mm from the paper edge and must not be clipped by the printable-area margin
        candidates = [f"ISO_full_bleed_A{paper[1:]}_({a}.00_x_{b}.00_MM)", f"ISO_A{paper[1:]}_({a}.00_x_{b}.00_MM)",
                      f"ISO_expand_A{paper[1:]}_({a}.00_x_{b}.00_MM)"]

        async def run() -> list[dict[str, Any]]:
            out = []
            for spec, (prims, measured) in zip(specs, prim_sets):
                vp_rect = layouts_mod.viewport_rect_for(spec, frame)
                win = layouts_mod.viewport_window_for(spec, vp_rect)
                call_spec = {"name": spec.code, "media_candidates": candidates, "rotation": 0, "frame_prims": prims,
                             "layers": layers, "viewport_layer": layouts_mod.VIEWPORT_LAYER,
                             "viewport_mm": list(vp_rect), "view_window": list(win),
                             "scale_denominator": spec.scale, "unit_mm": 1000.0, "marker": _view_marker(args, f"LAYOUT-{spec.code}", None,
                                                                 {"paper": frame.paper, "orientation": orientation,
                                                                  "scale": spec.scale, "window": [round(v, 3) for v in win],
                                                                  "title": measured["sheet_title"]["value"],
                                                                  "values": {k: v["value"] for k, v in measured.items()}}),
                             "if_exists": if_exists, "expected_marked": len(prims) + 1, "style_sheet": args.get("style_sheet", "monochrome.ctb"), "lock": True}
                info = await _sta(ctx, provider, provider.create_layout, call_spec, budget_s=_call_budget(ctx) * 4)
                info["declared"] = layouts_mod.view_frame_record(spec, frame, vp_rect)
                info["expected_title_block"] = measured
                info["expected_frame_entities"] = len(prims)
                out.append(info)
            return out

        infos, op = await _recorded(ctx, safety.OpKind.CREATE, _op_inputs("autocad.create_layout", args),
                                    [f"layout:{s.code}" for s in specs], run)
        checks: list[Check] = []
        for info, spec in zip(infos, specs):
            code = spec.code
            vps = layouts_mod_viewports(info)
            checks.append(Check(f"{code}_paper", _paper_matches(info, frame),
                                f"{info.get('paper_mm')} mm, {info.get('orientation')}, media {info.get('media')} "
                                f"(wanted {frame.width:g} x {frame.height:g})"))
            checks.append(Check(f"{code}_one_viewport", len(vps) == 1, f"{len(vps)} viewport(s) created by this tool"))
            if vps:
                measured_den = vps[0].get("scale_denominator")
                checks.append(Check(f"{code}_viewport_scale",
                                    measured_den is not None and abs(measured_den - spec.scale) <= 0.001 * spec.scale,
                                    f"viewport scale 1:{measured_den} (declared 1:{spec.scale})"))
                checks.append(Check(f"{code}_viewport_locked", vps[0].get("locked") is True,
                                    f"DisplayLocked={vps[0].get('locked')}"))
            if info.get("reused"):
                checks.append(Check(f"{code}_layout_already_present", info.get("marked_entities", 0) >= info["expected_frame_entities"],
                                    f"{info.get('marked_entities')} marked entities found, {info['expected_frame_entities']} frame entities expected"))
            else:
                checks.append(Check(f"{code}_frame_entities", info.get("frame_entities_drawn") == info["expected_frame_entities"],
                                    f"{info.get('frame_entities_drawn')} frame/title entities drawn, "
                                    f"{info['expected_frame_entities']} expected"))
            block = info.get("title_block") or {}
            missing = [k for k in layouts_mod.title_keys() if k not in block or not block[k].get("label")]
            checks.append(Check(f"{code}_title_block_fields", not missing, f"missing fields: {missing or 'none'}"))
            people = [k for k in layouts_mod.NEVER_FIELDS if str((block.get(k) or {}).get("value", "")).strip()]
            checks.append(Check(f"{code}_people_fields_blank", not people,
                                f"prepared/checked/approved cells left empty (filled: {people or 'none'})"))
        layouts_meta = layouts_mod.layouts_meta(infos)
        sheet_index = layouts_mod.expected_sheet_index(specs, frame)
        listing = ", ".join(f"{s.code} {s.scale_label}{'*' if s.scale_adjusted else ''}" for s in specs)
        adjusted = [f"{s.code}: wanted 1:{s.requested_scale}, 1:{s.scale} fits" for s in specs if s.scale_adjusted]
        return ToolOutcome(
            data={"layouts": layouts_meta, "sheet_index": sheet_index, "paper": frame.paper, "orientation": orientation,
                  "scale_adjusted": adjusted, "operation_id": op.op_id,
                  "title_block_policy": "project/client/designer/date/revision/status only from the arguments; prepared, "
                                        "checked, approved and signatures are never written"},
            checks=checks, backend=BACKEND,
            observation=(f"Created/checked {len(specs)} layout(s) on {frame.paper} {orientation}: {listing}. Each has a frame, title "
                         "block (people fields left empty), and one locked viewport at an exact scale read back from AutoCAD."
                         + (f" Scales adjusted to fit: {'; '.join(adjusted)}." if adjusted else "")
                         + " Give data.layouts and data.sheet_index to autocad.house_model for the sheet rules."))

    def layouts_mod_viewports(info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [v for v in info.get("viewports", []) if v.get("role") == "viewport" or v.get("marked")]

    def _paper_matches(info: Mapping[str, Any], frame: layouts_mod.Frame) -> bool:
        got = info.get("paper_mm") or [0, 0]
        return abs(got[0] - frame.width) <= 1.0 and abs(got[1] - frame.height) <= 1.0

    @registry.handler("autocad.plot_sheets")
    async def plot_sheets(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".pdf")
        overwrite = bool(args["overwrite"])
        if path.is_dir():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is a directory")
        if path.exists() and not overwrite:
            raise ToolError(ErrorCode.CONFLICT, f"{path} already exists; pass overwrite=true to replace it (a backup is made first)")
        measured = await _sta(ctx, provider, provider.read_layouts, args.get("sheets"), budget_s=_call_budget(ctx) * 2)
        measured = [m for m in measured if m.get("marked_entities")]
        if args.get("sheets"):
            order = {str(c).casefold(): i for i, c in enumerate(args["sheets"])}
            measured = sorted(measured, key=lambda m: order.get(str(m["name"]).casefold(), 999))
            missing = [c for c in args["sheets"] if str(c).casefold() not in {str(m["name"]).casefold() for m in measured}]
            if missing:
                raise ToolError(ErrorCode.NOT_FOUND, f"no layout made by autocad.create_layout for sheet(s) {missing}")
        else:
            measured = sorted(measured, key=lambda m: _natural_key(str(m["name"])))
        if not measured:
            raise ToolError(ErrorCode.NOT_FOUND, "the drawing has no layouts made by autocad.create_layout; create them first")
        names = [str(m["name"]) for m in measured]
        sheets = [{"code": n, "paper_mm": m.get("paper_mm")} for n, m in zip(names, measured)]
        render = bool(args.get("render_check", True))
        png_dir = None if not args.get("png_dir") else absolute_path(args["png_dir"])
        try:
            configured = ctx.config.get("autocad.pdf_check_python")  # optional key: interpreter that has PyMuPDF
        except Exception:  # not configured: the system Pythons are searched
            configured = None
        python = await asyncio.to_thread(pdfcheck.find_system_python, str(configured) if configured else None)

        async def plot() -> dict[str, Any]:
            path.parent.mkdir(parents=True, exist_ok=True)
            # One PDF per sheet (PlotToFile of the active layout), merged into one multi-page PDF with PyMuPDF.
            parts: list[Path] = []
            restore_errors: list[str] = []
            device = None
            try:
                for i, name in enumerate(names):
                    part = path.with_name(f"{path.stem}.part{i + 1:02d}.pdf")
                    one = await _sta(ctx, provider, provider.plot_layouts, [name], part, budget_s=_call_budget(ctx) * 4)
                    parts.append(part)
                    restore_errors += one.get("restore_errors", [])
                    device = one.get("device")
                    if not part.is_file() or part.stat().st_size == 0:
                        raise ToolError(ErrorCode.EXECUTION_FAILED, f"AutoCAD wrote no PDF for sheet {name}")
                if python is None and len(parts) > 1:
                    raise ToolError(ErrorCode.EXECUTION_FAILED,
                                    "no Python with PyMuPDF was found, so the per-sheet PDFs cannot be merged into one "
                                    f"multi-page PDF; the single-sheet files are kept next to {path}")
                if len(parts) == 1:
                    parts[0].replace(path)
                    merged = {"ok": True, "pages": 1, "note": "one sheet: no merge needed"}
                    parts = []
                else:
                    merged = await asyncio.to_thread(pdfcheck.merge_pdfs, parts, path, python)
                    if not merged["ok"]:
                        raise ToolError(ErrorCode.EXECUTION_FAILED, merged["note"])
            except BaseException:
                raise
            else:
                for part in parts:
                    part.unlink(missing_ok=True)
            info: dict[str, Any] = {"method": "one PDF per sheet (PlotToFile of the active layout), merged with PyMuPDF",
                                    "merge": merged, "device": device, "restore_errors": restore_errors, "pages": merged["pages"]}
            if render and python is not None:
                info["pdf_check"] = await asyncio.to_thread(pdfcheck.render_check, path, sheets, out_dir=png_dir, python=python)
                info["pages"] = info["pdf_check"].get("pages", merged["pages"])
            return info

        info, op = await _guarded_write(ctx, safety.OpKind.EXPORT, path, overwrite, plot)
        extra, extra_checks = _write_extras(op)
        checks, size, head = await asyncio.to_thread(_file_checks, path)
        checks += extra_checks
        checks.append(Check("pdf_header", head.startswith(PDF_MAGIC), f"starts with {head[:8]!r}"))
        checks.append(Check("settings_restored", not info.get("restore_errors"),
                            "; ".join(info.get("restore_errors") or []) or "BACKGROUNDPLOT and QuietErrorMode restored"))
        pdf_check = info.get("pdf_check")
        if pdf_check is not None and pdf_check.get("rendered"):
            checks.append(Check("pdf_pages_equal_sheets", pdf_check["pages"] == len(names),
                                f"{pdf_check['pages']} pages for {len(names)} sheets"))
            checks.append(Check("pdf_pages_not_blank", all(p["nonwhite_ratio"] >= 0.0005 for p in pdf_check["page_checks"]),
                                ", ".join(f"p{p['page']} {p['nonwhite_ratio']:.4f}" for p in pdf_check["page_checks"])))
            checks.append(Check("pdf_pages_have_text_and_codes",
                                all(p["text_chars"] and p["sheet_code"] in p["codes_found"] for p in pdf_check["page_checks"]),
                                "; ".join(f"p{p['page']} {p['text_chars']} chars" for p in pdf_check["page_checks"])))
            checks.append(Check("pdf_page_sizes_match_layouts", not [x for x in pdf_check.get("problems", []) if "mm" in x],
                                "; ".join(x for x in pdf_check.get("problems", []) if "mm" in x) or "all pages match their layout paper"))
        meta_check = None
        if pdf_check is not None:
            meta_check = {"ok": bool(pdf_check.get("ok")), "rendered": bool(pdf_check.get("rendered")),
                          "note": pdf_check.get("note", ""), "pages": pdf_check.get("pages"),
                          "expected_pages": len(names), "page_checks": pdf_check.get("page_checks", []),
                          "renderer": pdf_check.get("renderer")}
        status = ("rendered with PyMuPDF and inspected: " + pdf_check["note"]) if pdf_check and pdf_check.get("rendered") else (
            "NOT rendered or inspected: " + (pdf_check["note"] if pdf_check else
                                             ("render_check was off" if not render else "no system Python with PyMuPDF was found")))
        return ToolOutcome(
            data={"path": str(path), "size": size, "sheets": names, "pages": info.get("pages"), "method": info.get("method"),
                  "pdf_render_check": meta_check, "page_images": [p.get("png") for p in (pdf_check or {}).get("page_checks", [])],
                  "device": info.get("device"), **extra},
            checks=checks, artifacts=[{"kind": "file", "path": str(path)}], backend=BACKEND,
            observation=(f"Plotted {len(names)} layout(s) {names} to {path} ({size} bytes, {info.get('method')}); PDF {status}."))

    def _natural_key(name: str) -> list[Any]:
        import re  # noqa: PLC0415
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

    @registry.handler("autocad.reopen_check")
    async def reopen_check(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        path = _output_path(args["path"], ".dwg")
        keep_open = bool(args.get("keep_open", False))
        raw = await _sta(ctx, provider, provider.reopen_check, path, keep_open, budget_s=_call_budget(ctx) * 4)
        expected = args.get("expected_counts") or raw.get("before")
        differences = layouts_mod.compare_counts(expected, raw["after"]) if expected else ["no counts to compare with"]
        opened = True
        compared = "the same drawing before closing it" if raw.get("before") else "the expected_counts argument"
        record = {"opened": opened, "differences": differences, "compared": compared, "path": str(path),
                  "before": expected, "after": raw["after"], "read_only": raw["opened_read_only"]}
        checks = [Check("dwg_reopened", opened, f"opened {raw['opened_name']} from disk (read-only={raw['opened_read_only']})"),
                  Check("counts_equal_after_reopen", not differences,
                        "; ".join(differences) or f"model space {raw['after']['model_space']} entities, "
                                                  f"{len(raw['after']['layouts'])} layouts, all counts equal")]
        return ToolOutcome(
            data={"reopen_check": record, "after": raw["after"], "before": expected, "was_open_before": raw["was_open_before"],
                  "evidence": {"dwg_opened_in_autocad": not differences, "dwg_reopen_check": record}},
            checks=checks, backend=BACKEND,
            observation=(f"Re-opened {path} from disk in AutoCAD ({'kept open' if keep_open else 'closed again'}): model space "
                         f"{raw['after']['model_space']} entities, {len(raw['after']['layouts'])} layout(s); "
                         + ("counts equal those of " + compared if not differences else "DIFFERENCES: " + "; ".join(differences))
                         + ". Pass data.evidence to autocad.report."))

    @registry.handler("autocad.draw_image")
    async def draw_image(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        from PIL import Image

        from lga_worker.paint import sketch as sketching
        from lga_worker.paint import vectorize
        from lga_worker.paint.model import Canvas

        source = Path(args["image_path"])
        if not source.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"image not found: {source}")
        try:
            with Image.open(source) as opened:
                picture = opened.convert("RGB")
        except OSError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is not an image I can read: {exc}") from exc
        if min(picture.size) < 16:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{source.name} is too small ({picture.width}x{picture.height})")
        detail = str(args.get("detail") or vectorize.DEFAULT_DETAIL)
        width = float(args.get("width") or 1000.0)
        sheet = Canvas(1152, 648)  # only its proportions matter: the picture is fitted into a 1000 x 562 unit sheet
        traced = await asyncio.to_thread(sketching.sketch, picture, sheet, detail=detail,
                                         colors=int(args.get("colors") or sketching.DEFAULT_COLORS),
                                         shading=bool(args.get("shading", True)))
        scale = width / 1000.0
        height = sheet.unit_height * scale
        cad = [(tuple((x * scale, height - y * scale) for x, y in s.points), sketching.hex_to_rgb(s.color), s.closed)
               for s in traced.strokes]
        provider = await _provider(ctx)
        paper = str(args.get("paper") or "white")
        result = await _sta(ctx, provider, provider.draw_strokes, cad, bool(args.get("new_document", True)), paper,
                            sketching.aci_nearest, budget_s=max(60.0, ctx.remaining_s() - 5.0))
        rendered = sketching.render(traced.strokes, sheet)
        likeness = await asyncio.to_thread(vectorize.fidelity, picture, rendered, sheet)
        added = result.entities_after - result.entities_before
        complete = result.drawn == len(traced.strokes) and added == result.drawn
        note = ""
        if result.background == "white":
            was = result.previous_background
            note = (f" The model-space background was set to white (it was {was:#08x}) so pencil lines of any colour show;"
                    " change it back under Options > Display > Colors if you prefer black.")
        return ToolOutcome(
            data={"document": result.document, "strokes": result.drawn, "contours": traced.stats["contours"],
                  "hatch_lines": traced.stats["hatch_lines"], "layers": result.layers, "colors": traced.stats["colors"],
                  "true_color": result.true_color, "background": result.background, "sheet": [width, round(height, 1)],
                  "fidelity": likeness, "source": str(source), "detail": detail},
            checks=[Check("strokes_drawn", complete, f"{added} entities added for {len(traced.strokes)} strokes")],
            observation=(f"Sketched {source.name} in {result.document} as {traced.stats['contours']} coloured contours and "
                         f"{traced.stats['hatch_lines']} shading lines on {result.layers} colour layers "
                         f"({'true colour' if result.true_color else 'nearest index colours'}), one undo step; sheet "
                         f"{width:g} x {height:.0f} units; likeness to the picture: structural similarity "
                         f"{likeness['ssim']}.{note}"),
            backend=BACKEND,
        )

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

    # ------------------------------------------------------------------ spec tools: offline, no AutoCAD needed

    async def _offline(fn: Callable[[Mapping[str, Any]], dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, args)
        except spec_tools.SpecInputError as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc

    @registry.handler("autocad.profile_check")
    async def profile_check(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await _offline(spec_tools.profile_check, args)
        missing = {tier: ids for tier, ids in data["missing"].items() if ids}
        return ToolOutcome(
            data=data, backend="offline",
            observation=(f"Profile: ready_to_draw={data['ready_to_draw']}, ready_for_plot={data['ready_for_plot']}, "
                         f"ready_for_code_review={data['ready_for_code_review']}; {len(data['questions'])} question(s). "
                         f"Missing: {missing or 'nothing'}. Ask the user the questions (options, Khac, Giu UNKNOWN); "
                         "do not fill missing values or state a legal requirement."))

    @registry.handler("autocad.validate_model")
    async def validate_model(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await _offline(spec_tools.validate_model, args)
        if args.get("list_rules"):
            return ToolOutcome(data=data, backend="offline", observation=f"{len(data['rules'])} validation rules.")
        failing = [f"{r['rule_id']} ({r['severity']})" for r in data["results"] if r["result"] == "FAIL"]
        return ToolOutcome(
            data=data, backend="offline",
            observation=(f"Validation {data['overall']} over {data['entity_count']} entities: counts {data['counts']}, "
                         f"by scope {data['by_scope']}. Failing: {failing or 'none'}. NOT_RUN: {data['not_run'] or 'none'}. "
                         "NOT_RUN means not checked; it is not a pass."))

    @registry.handler("autocad.compare_revisions")
    async def compare_revisions(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await _offline(spec_tools.compare_revisions, args)
        c = data["counts"]
        note = " Mapping is uncertain: confirm before accepting any deletion." if data["mass_delete_requires_confirmation"] else ""
        return ToolOutcome(data=data, backend="offline",
                           observation=f"Revisions: {c['added']} added, {c['modified']} modified, {c['deleted']} deleted, "
                                       f"{c['conflicted']} conflicted, {c['unchanged']} unchanged.{note}")

    @registry.handler("autocad.plan_operation")
    async def plan_operation(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await _offline(spec_tools.plan_operation, args)
        verdict = ("needs explicit confirmation: " + "; ".join(data["confirmation_reasons"])) if data["needs_confirmation"] \
            else "no confirmation needed"
        return ToolOutcome(data=data, backend="offline",
                           observation=f"{data['kind']} dry run: {verdict}. Idempotency key {data['idempotency_key']} "
                                       f"(marker {data['marker']}).")

    @registry.handler("autocad.report")
    async def report(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        built = await _offline(spec_tools.build_report, args)
        files: list[str] = []
        checks: list[Check] = []
        if args.get("report_path"):
            path = absolute_path(args["report_path"])
            try:
                written = await asyncio.to_thread(spec_tools.write_report_files, built["markdown"], built["report"], path)
            except spec_tools.SpecInputError as exc:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
            files = [str(p) for p in written]
            checks.append(Check("report_written", path.is_file() and path.stat().st_size > 0, f"{path}"))
        final = built["report"]["FINAL_STATUS"]
        validation_overall = (built["validation"] or {}).get("overall")
        return ToolOutcome(
            data={"report": built["report"], "markdown": built["markdown"], "final_status": final["status"],
                  "verified_level": final["verified_level"], "files": files, "validation_overall": validation_overall},
            checks=checks, artifacts=[{"kind": "file", "path": f} for f in files], backend="offline",
            observation=(f"Report built. FINAL_STATUS {final['status']} (verified level {final['verified_level']}); "
                         f"validation {validation_overall or 'not run'}; "
                         f"{len(built['report']['LIMITATIONS'])} limitation(s). Report the status exactly as computed."
                         + (f" Written: {', '.join(files)}." if files else "")))
