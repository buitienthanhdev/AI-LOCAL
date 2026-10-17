"""Argument -> result functions behind autocad.profile_check / validate_model / compare_revisions / report /
plan_operation. Everything is synchronous and offline (JSON files and Python objects only); the async tool handlers in
lga_worker/tools/autocad.py call these through `asyncio.to_thread`.

Errors in the caller's data are raised as `SpecInputError` (the handlers turn it into INVALID_ARGUMENT).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lga_worker.autocad import layers, report as report_mod, revision, safety, standards, validation
from lga_worker.autocad.provenance import Entity, ProvenanceError, load_entities

MAX_JSON_BYTES = 32 * 1024 * 1024


class SpecInputError(ValueError):
    pass


def read_json_file(path: str | Path, *, what: str) -> Any:
    p = Path(path)
    if not p.is_file():
        raise SpecInputError(f"{what} file not found: {p}")
    if p.stat().st_size > MAX_JSON_BYTES:
        raise SpecInputError(f"{what} file is larger than {MAX_JSON_BYTES} bytes: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SpecInputError(f"{what} file {p} is not readable JSON: {exc}") from exc


def profile_check(args: Mapping[str, Any]) -> dict[str, Any]:
    return standards.check_profile(args.get("profile") or {}).to_dict()


def _model_from_args(args: Mapping[str, Any]) -> validation.DrawingModel:
    sources = [k for k in ("model", "model_path", "autocad_entities") if args.get(k) is not None]
    if len(sources) != 1:
        raise SpecInputError("give exactly one of model, model_path or autocad_entities "
                             f"(got {sources or 'none'})")
    try:
        if "autocad_entities" in sources:
            layer_map = layers.LayerMap.from_dict(args.get("layer_map"))
            meta = dict(args.get("meta") or {})
            entities = layers.import_autocad_entities(args["autocad_entities"], layer_map,
                                                      units=meta.get("units"), source_file=meta.get("source_file"))
            return validation.DrawingModel(meta=meta, entities=entities, profile=dict(args.get("profile") or {}))
        raw = args["model"] if "model" in sources else read_json_file(args["model_path"], what="model")
        model = validation.DrawingModel.from_dict(raw)
        if args.get("profile") and not model.profile:
            model.profile = dict(args["profile"])
        return model
    except (ProvenanceError, layers.LayerMapError, KeyError, TypeError) as exc:
        raise SpecInputError(f"model is invalid: {exc}") from exc


def validate_model(args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("list_rules"):
        return {"rules": validation.rule_catalog(), "overall": "NOT_RUN", "counts": {}, "by_scope": {},
                "not_run": [], "results": [], "waived": [], "rejected_waivers": [], "tolerances": {},
                "open_blockers": [], "open_errors": []}
    model = _model_from_args(args)
    try:
        result = validation.run_validation(model, tolerances=args.get("tolerances"), params=args.get("rule_params"),
                                           waivers=args.get("waivers"), scopes=args.get("scopes"),
                                           rule_ids=args.get("rule_ids"))
    except (validation.ToleranceError, ValueError) as exc:
        raise SpecInputError(str(exc)) from exc
    data = result.to_dict()
    data["entity_count"] = len(model.entities)
    return data


def _entities_arg(args: Mapping[str, Any], key: str) -> list[Entity]:
    raw = args.get(key)
    if raw is None and args.get(f"{key}_path"):
        loaded = read_json_file(args[f"{key}_path"], what=key)
        raw = loaded.get("entities") if isinstance(loaded, Mapping) else loaded
    if raw is None:
        raise SpecInputError(f"give '{key}' (entity list) or '{key}_path'")
    try:
        return load_entities(list(raw))
    except (ProvenanceError, TypeError) as exc:
        raise SpecInputError(f"{key}: {exc}") from exc


def compare_revisions(args: Mapping[str, Any]) -> dict[str, Any]:
    old, new = _entities_arg(args, "old"), _entities_arg(args, "new")
    base = _entities_arg(args, "base") if (args.get("base") is not None or args.get("base_path")) else None
    try:
        diff = revision.compare_revisions(old, new, float(args.get("tolerance", 0.01)), base=base)
    except ValueError as exc:
        raise SpecInputError(str(exc)) from exc
    data = diff.to_dict()
    log = args.get("log")
    if log:
        try:
            data["log"] = revision.make_log_entries(diff, reason=log.get("reason", ""),
                                                    source_request=log.get("source_request", ""),
                                                    actor=log.get("actor", ""),
                                                    sheets_affected=log.get("sheets_affected") or [])
        except ValueError as exc:
            raise SpecInputError(str(exc)) from exc
    return data


def plan_operation(args: Mapping[str, Any]) -> dict[str, Any]:
    try:
        kind = safety.OpKind(str(args["kind"]).upper())
    except ValueError as exc:
        raise SpecInputError(f"unknown operation kind {args['kind']!r}") from exc
    target = Path(args["target_path"]) if args.get("target_path") else None
    reasons = safety.confirmation_reasons(kind, count=int(args.get("count", 1)),
                                          target_exists=bool(target and target.is_file()),
                                          bulk_threshold=int(args.get("bulk_threshold",
                                                                      safety.DEFAULT_BULK_DELETE_THRESHOLD)))
    key = safety.idempotency_key(kind, args.get("inputs") or {})
    return {"kind": kind.value, "mode": "DRY_RUN", "idempotency_key": key, "marker": safety.marker(key),
            "needs_confirmation": bool(reasons), "confirmation_reasons": reasons,
            "backup_planned": bool(target and target.is_file() and kind in (safety.OpKind.OVERWRITE, safety.OpKind.SAVE,
                                                                             safety.OpKind.EXPORT)),
            "note": "dry run only: nothing was changed"}


def build_report(args: Mapping[str, Any]) -> dict[str, Any]:
    """Returns {"report": ..., "markdown": ..., "validation": ... | None}. Writing the files is the handler's job."""
    model = None
    if any(args.get(k) is not None for k in ("model", "model_path", "autocad_entities")):
        model = _model_from_args(args)
    validation_dict = None
    if model is not None and args.get("validate", True):
        try:
            validation_dict = validation.run_validation(model, tolerances=args.get("tolerances"),
                                                        params=args.get("rule_params"), waivers=args.get("waivers"),
                                                        scopes=args.get("scopes")).to_dict()
        except (validation.ToleranceError, ValueError) as exc:
            raise SpecInputError(str(exc)) from exc
    code_review = None
    if args.get("code_review_file"):
        code_review = read_json_file(args["code_review_file"], what="code review")
    issue_config = None
    if args.get("issue_label") or args.get("issue_approval_file"):
        approval = read_json_file(args["issue_approval_file"], what="issue approval") \
            if args.get("issue_approval_file") else {}
        issue_config = {"label": args.get("issue_label") or "", "approval": approval}
    evidence = dict(args.get("evidence") or {})
    profile = args.get("profile") if args.get("profile") is not None else (model.profile if model and model.profile else None)
    report = report_mod.build_report(
        project=str(args["project"]), profile=profile, model=model, validation=validation_dict,
        outputs=list(args.get("outputs") or []), inputs=list(args.get("inputs") or []), evidence=evidence,
        drawing_plan=list(args.get("drawing_plan") or []), execution_log=list(args.get("execution_log") or []),
        assumptions=list(args.get("assumptions") or []), open_questions=list(args.get("open_questions") or []),
        limitations=list(args.get("limitations") or []), code_review=code_review, issue_config=issue_config)
    return {"report": report, "markdown": report_mod.render_markdown(report), "validation": validation_dict}


def write_report_files(markdown: str, report: Mapping[str, Any], path: Path) -> list[Path]:
    """Write `<path>` (.md) and a JSON twin next to it, atomically. Returns the files that now exist."""
    if path.suffix.lower() != ".md":
        raise SpecInputError(f"report_path must end in .md: {path}")
    json_path = path.with_suffix(".json")
    safety.atomic_write_text(path, markdown)
    safety.atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return [p for p in (path, json_path) if p.is_file()]
