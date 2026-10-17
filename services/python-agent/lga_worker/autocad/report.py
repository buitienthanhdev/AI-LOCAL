"""Run report (spec section 19) with a FINAL_STATUS computed only from verified evidence. Pure Python.

Sections, always present: INPUT_REVIEW, ASSUMPTIONS, QUESTIONS, DRAWING_PLAN, EXECUTION_LOG, VALIDATION,
OPEN_ISSUES, OUTPUTS, FINAL_STATUS, LIMITATIONS.

FINAL_STATUS is the highest level whose requirements are all met, checked in order:

  DRAFT            default; nothing verified
  CAD_VALID        DWG exists on disk with a DWG header, was opened in AutoCAD (caller evidence), units rule PASS,
                   FILE and GEOMETRY validation ran and has no open BLOCKER/ERROR
  DRAWING_CHECKED  CAD_VALID + annotation rules ran without open ERROR + PDF exists, has a PDF header, was rendered
                   and inspected (caller evidence) + layout rules do not fail
  COORDINATED      DRAWING_CHECKED + coordination rules ran without open ERROR
  CODE_REVIEWED    COORDINATED + a named human reviewer record (reviewer, role, date, scope) + a profile whose code
                   fields are all verified by the lead designer

A construction-issue status can only be reached when the caller supplies an explicit label AND a complete approval
record (approved_by, role, date, document_ref) AND the drawing is CODE_REVIEWED. Nothing in this module can produce it
otherwise. Outputs are listed only when the file exists on disk; a claimed-but-missing file is reported as missing.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lga_worker.autocad import safety
from lga_worker.autocad.provenance import SourceStatus
from lga_worker.autocad.standards import check_profile
from lga_worker.autocad.validation import DrawingModel

SECTIONS = ("INPUT_REVIEW", "ASSUMPTIONS", "QUESTIONS", "DRAWING_PLAN", "EXECUTION_LOG", "VALIDATION", "OPEN_ISSUES",
            "OUTPUTS", "FINAL_STATUS", "LIMITATIONS")
STATUS_LEVELS = ("DRAFT", "CAD_VALID", "DRAWING_CHECKED", "COORDINATED", "CODE_REVIEWED")
APPROVAL_FIELDS = ("approved_by", "role", "date", "document_ref")
REVIEW_FIELDS = ("reviewer", "role", "date", "scope")
OVERCLAIM = re.compile(r"industry[\s-]*standard|đạt\s+chuẩn|dat\s+chuan|issued\s+for\s+construction|"
                       r"code[\s-]*compliant|legally\s+compliant|tuân\s+thủ\s+(?:pháp|quy\s+chuẩn)", re.IGNORECASE)
DWG_MAGIC = b"AC10"
PDF_MAGIC = b"%PDF-"
OUTPUT_KINDS = {".dwg": "DWG", ".dxf": "DXF", ".pdf": "PDF", ".json": "JSON", ".md": "REPORT", ".png": "PREVIEW",
                ".jpg": "PREVIEW", ".csv": "SCHEDULE", ".xlsx": "SCHEDULE"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_facts(path: str | Path) -> dict[str, Any]:
    """What is verifiably true about a file right now: existence, size, header bytes, hash."""
    p = Path(path)
    kind = OUTPUT_KINDS.get(p.suffix.lower(), "FILE")
    if not p.is_file():
        return {"path": str(p), "kind": kind, "exists": False, "size": 0, "header_ok": False, "sha256": None}
    size = p.stat().st_size
    with p.open("rb") as fh:
        head = fh.read(16)
    header_ok = {"DWG": head.startswith(DWG_MAGIC), "PDF": head.startswith(PDF_MAGIC)}.get(kind, size > 0)
    return {"path": str(p), "kind": kind, "exists": True, "size": size, "header_ok": bool(header_ok and size > 0),
            "sha256": safety.sha256_file(p)}


def _scope(validation: Mapping[str, Any] | None, scope: str) -> str:
    return (validation or {}).get("by_scope", {}).get(scope, "NOT_RUN")


def _rule(validation: Mapping[str, Any] | None, rule_id: str) -> str:
    for r in (validation or {}).get("results", []):
        if r["rule_id"] == rule_id:
            return r["result"]
    return "NOT_RUN"


def compute_final_status(*, validation: Mapping[str, Any] | None, dwg: Mapping[str, Any] | None,
                         pdf: Mapping[str, Any] | None, evidence: Mapping[str, Any] | None,
                         profile_report: Mapping[str, Any] | None, code_review: Mapping[str, Any] | None,
                         issue_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The status proven by the inputs, and for each higher level the first missing requirement."""
    evidence = evidence or {}
    blocked: dict[str, list[str]] = {}
    reached = "DRAFT"

    # CAD_VALID
    why: list[str] = []
    if not dwg or not dwg["exists"]:
        why.append("no DWG output exists on disk")
    elif not dwg["header_ok"]:
        why.append("the DWG file has no DWG header or is empty")
    if not evidence.get("dwg_opened_in_autocad"):
        why.append("the DWG was not confirmed to open in AutoCAD")
    if validation is None:
        why.append("validation was not run")
    else:
        if _rule(validation, "FILE.UNITS_DECLARED") != "PASS":
            why.append(f"units rule is {_rule(validation, 'FILE.UNITS_DECLARED')}, not PASS")
        for scope in ("FILE", "GEOMETRY"):
            state = _scope(validation, scope)
            if state == "FAIL":
                why.append(f"{scope} validation has open BLOCKER/ERROR")
            elif state == "NOT_RUN":
                why.append(f"{scope} validation did not run")
        if validation.get("open_blockers"):
            why.append("open BLOCKER(s): " + ", ".join(validation["open_blockers"]))
    if why:
        blocked["CAD_VALID"] = why
        return _finish(reached, blocked, issue_config, code_review, profile_report)
    reached = "CAD_VALID"

    # DRAWING_CHECKED
    why = []
    if _scope(validation, "ANNOTATION") in ("FAIL", "NOT_RUN"):
        why.append(f"annotation validation is {_scope(validation, 'ANNOTATION')}")
    if not pdf or not pdf["exists"] or not pdf["header_ok"]:
        why.append("no valid PDF output on disk")
    if not evidence.get("pdf_rendered_checked"):
        why.append("the PDF was not rendered to an image and inspected")
    if _scope(validation, "PLOT") == "FAIL":
        why.append("plot/layout validation has open ERROR")
    if why:
        blocked["DRAWING_CHECKED"] = why
        return _finish(reached, blocked, issue_config, code_review, profile_report)
    reached = "DRAWING_CHECKED"

    # COORDINATED
    why = []
    state = _scope(validation, "COORDINATION")
    if state in ("FAIL", "NOT_RUN"):
        why.append(f"coordination validation is {state}")
    if why:
        blocked["COORDINATED"] = why
        return _finish(reached, blocked, issue_config, code_review, profile_report)
    reached = "COORDINATED"

    # CODE_REVIEWED
    why = []
    review = code_review or {}
    missing = [f for f in REVIEW_FIELDS if not str(review.get(f) or "").strip()]
    if missing:
        why.append("no complete human code-review record (missing " + ", ".join(missing) + ")")
    if not profile_report or not profile_report.get("ready_for_code_review"):
        why.append("profile code fields are not all verified by the lead designer")
    if why:
        blocked["CODE_REVIEWED"] = why
        return _finish(reached, blocked, issue_config, code_review, profile_report)
    reached = "CODE_REVIEWED"
    return _finish(reached, blocked, issue_config, code_review, profile_report)


def _finish(reached: str, blocked: dict[str, list[str]], issue_config: Mapping[str, Any] | None,
            code_review: Mapping[str, Any] | None, profile_report: Mapping[str, Any] | None) -> dict[str, Any]:
    status, issued = reached, False
    label = str((issue_config or {}).get("label") or "").strip()
    if label:
        approval = (issue_config or {}).get("approval") or {}
        gaps = [f for f in APPROVAL_FIELDS if not str(approval.get(f) or "").strip()]
        problems: list[str] = []
        if label in STATUS_LEVELS:
            problems.append(f"issue label {label!r} is not an issue status")
        if gaps:
            problems.append("no complete approval record (missing " + ", ".join(gaps) + ")")
        if reached != "CODE_REVIEWED":
            problems.append(f"the drawing only reached {reached}")
        if problems:
            blocked[label] = problems
        else:
            status, issued = label, True
    return {"status": status, "verified_level": reached, "issued": issued, "blocked_at": blocked,
            "basis": "computed from verified evidence only"}


def _overclaims(texts: Sequence[str]) -> list[str]:
    return [t for t in texts if OVERCLAIM.search(t)]


def _entity_summary(model: DrawingModel | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {"entity_count": len(model.entities),
            "by_type": dict(Counter(e.entity_type.value for e in model.entities)),
            "by_status": dict(Counter(e.status.value for e in model.entities)),
            "units": model.units, "meta_keys": sorted(model.meta)}


def build_report(*, project: str, profile: Mapping[str, Any] | None = None, model: DrawingModel | None = None,
                 validation: Mapping[str, Any] | None = None, outputs: Sequence[str] = (),
                 evidence: Mapping[str, Any] | None = None, drawing_plan: Sequence[str] = (),
                 execution_log: Sequence[Any] = (), assumptions: Sequence[str] = (),
                 open_questions: Sequence[str] = (), limitations: Sequence[str] = (),
                 code_review: Mapping[str, Any] | None = None, issue_config: Mapping[str, Any] | None = None,
                 inputs: Sequence[str] = ()) -> dict[str, Any]:
    evidence = dict(evidence or {})
    profile_report = check_profile(profile).to_dict() if profile is not None else None
    facts = [file_facts(p) for p in outputs]
    existing = [f for f in facts if f["exists"]]
    missing = [f["path"] for f in facts if not f["exists"]]
    dwg = next((f for f in existing if f["kind"] == "DWG"), None)
    pdf = next((f for f in existing if f["kind"] == "PDF"), None)
    final = compute_final_status(validation=validation, dwg=dwg, pdf=pdf, evidence=evidence,
                                 profile_report=profile_report, code_review=code_review, issue_config=issue_config)

    # --- ASSUMPTIONS / QUESTIONS
    assumed = list(assumptions)
    conflicts: list[str] = []
    if model is not None:
        for e in model.entities:
            if e.status is SourceStatus.ASSUMED:
                assumed.append(f"entity {e.entity_id} ({e.entity_type.value}) is ASSUMED: {e.source.note or 'no reason recorded'}")
            if e.status is SourceStatus.CONFLICTED:
                conflicts.append(f"entity {e.entity_id}: sources disagree; which source is authoritative?")
    if profile_report:
        assumed += [f"profile field {f} is ASSUMED" for f in profile_report["assumed"]]
    questions = [q["prompt"] + f" ({q['field_id']}: {q['reason']})" for q in (profile_report or {}).get("questions", [])]
    questions += conflicts + list(open_questions)

    # --- OPEN ISSUES
    open_issues: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    for r in (validation or {}).get("results", []):
        for issue in r["issues"]:
            row = {"rule_id": r["rule_id"], "severity": issue["severity"], "kind": issue["kind"],
                   "message": issue["message"], "entity_ids": issue["entity_ids"]}
            (waived if issue["waived"] else open_issues).append(row)
    rank = {"BLOCKER": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}
    open_issues.sort(key=lambda i: rank[i["severity"]])

    # --- LIMITATIONS (what was NOT checked)
    lim: list[str] = []
    claimed = sorted(k for k, v in evidence.items() if v)
    if claimed:
        lim.append("Evidence flags supplied by the caller and not independently verified by this report: "
                   + ", ".join(claimed) + ". File existence and headers were checked by the report itself.")
    if not evidence.get("dwg_opened_in_autocad"):
        lim.append("The DWG was not confirmed to open in AutoCAD in this run.")
    if not evidence.get("autocad_session"):
        lim.append("No live AutoCAD session was used as evidence (no AUDIT, no regenerate, no plot test).")
    if not (pdf and evidence.get("pdf_rendered_checked")):
        lim.append("The PDF was not rendered to an image and inspected (or no PDF exists).")
    if validation is None:
        lim.append("No validation was run.")
    else:
        not_run = validation.get("not_run", [])
        if not_run:
            lim.append(f"{len(not_run)} validation rule(s) could not run (NOT_RUN): {', '.join(not_run)}.")
        if validation.get("rejected_waivers"):
            lim.append(f"{len(validation['rejected_waivers'])} waiver(s) were rejected as incomplete.")
    if profile_report is None:
        lim.append("No Project Standards Profile was supplied.")
    else:
        if not profile_report["ready_to_draw"]:
            lim.append("The Project Standards Profile is missing required drawing fields: "
                       + ", ".join(profile_report["missing"]["DRAW"]) + ".")
        if not profile_report["ready_for_code_review"]:
            lim.append("Code/legal fields are missing or not verified by the lead designer; "
                       "no legal or code compliance statement is made.")
    lim.append("This tool checks geometry, data consistency and files only. It does not check legal, structural, "
               "fire-safety or accessibility compliance; only a qualified, authorised person can.")
    lim += list(limitations)
    texts = [*assumed, *drawing_plan, *open_questions, *limitations, project] + [str(x) for x in execution_log]
    for claim in _overclaims(texts):
        lim.append(f"Supplied text contains a compliance or issue claim the report cannot support: {claim!r}.")
    if missing:
        lim.append("Listed output file(s) do not exist and are not reported as outputs: " + ", ".join(missing) + ".")

    input_facts = [file_facts(p) for p in inputs]
    report = {
        "project": project,
        "generated": _now(),
        "INPUT_REVIEW": {"input_files": input_facts, "profile": profile_report, "model": _entity_summary(model)},
        "ASSUMPTIONS": assumed,
        "QUESTIONS": questions,
        "DRAWING_PLAN": list(drawing_plan),
        "EXECUTION_LOG": list(execution_log),
        "VALIDATION": ({k: validation[k] for k in ("overall", "counts", "by_scope", "not_run", "tolerances")}
                       | {"rules": [{k: r[k] for k in ("rule_id", "scope", "severity", "result", "evidence")}
                                    for r in validation["results"]]}) if validation else None,
        "OPEN_ISSUES": {"open": open_issues, "waived": waived},
        "OUTPUTS": {"files": existing, "missing": missing},
        "FINAL_STATUS": final,
        "LIMITATIONS": lim,
    }
    return report


def _bullets(items: Sequence[Any], empty: str = "none") -> list[str]:
    return [f"- {i}" for i in items] if items else [f"- {empty}"]


def render_markdown(report: Mapping[str, Any]) -> str:
    out = [f"# Drawing report: {report['project']}", "", f"Generated {report['generated']}", ""]
    ir = report["INPUT_REVIEW"]
    out += ["## INPUT_REVIEW"]
    out += _bullets([f"{f['path']} ({'exists, ' + str(f['size']) + ' bytes' if f['exists'] else 'NOT FOUND'})"
                     for f in ir["input_files"]], "no input files listed")
    if ir["model"]:
        out.append(f"- model: {ir['model']['entity_count']} entities {ir['model']['by_type']}, units {ir['model']['units']}")
    if ir["profile"]:
        p = ir["profile"]
        out.append(f"- profile: ready_to_draw={p['ready_to_draw']}, ready_for_plot={p['ready_for_plot']}, "
                   f"ready_for_code_review={p['ready_for_code_review']}; missing for drawing: "
                   f"{', '.join(p['missing']['DRAW']) or 'none'}")
    out += ["", "## ASSUMPTIONS"] + _bullets(report["ASSUMPTIONS"])
    out += ["", "## QUESTIONS"] + _bullets(report["QUESTIONS"])
    out += ["", "## DRAWING_PLAN"] + _bullets(report["DRAWING_PLAN"])
    out += ["", "## EXECUTION_LOG"] + _bullets(report["EXECUTION_LOG"])
    out += ["", "## VALIDATION"]
    v = report["VALIDATION"]
    if v:
        out.append(f"- overall: {v['overall']}; counts {v['counts']}; by scope {v['by_scope']}")
        out += [f"- {r['result']:8} {r['severity']:8} {r['rule_id']}: {r['evidence']}" for r in v["rules"]]
    else:
        out.append("- NOT RUN")
    oi = report["OPEN_ISSUES"]
    out += ["", "## OPEN_ISSUES"]
    out += _bullets([f"[{i['severity']}] {i['rule_id']}: {i['message']}" for i in oi["open"]])
    if oi["waived"]:
        out += ["Waived (approved exceptions):"] + _bullets([f"{w['rule_id']}: {w['message']}" for w in oi["waived"]])
    out += ["", "## OUTPUTS"]
    out += _bullets([f"{f['kind']} {f['path']} ({f['size']} bytes, sha256 {f['sha256'][:12]}...)" for f in report["OUTPUTS"]["files"]],
                    "no output file exists")
    if report["OUTPUTS"]["missing"]:
        out += ["Not found on disk:"] + _bullets(report["OUTPUTS"]["missing"])
    fs = report["FINAL_STATUS"]
    out += ["", "## FINAL_STATUS", f"**{fs['status']}** ({fs['basis']}; verified level {fs['verified_level']})"]
    for level, reasons in fs["blocked_at"].items():
        out.append(f"- not {level}: " + "; ".join(reasons))
    out += ["", "## LIMITATIONS"] + _bullets(report["LIMITATIONS"])
    return "\n".join(out) + "\n"
