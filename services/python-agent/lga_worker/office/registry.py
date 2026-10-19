"""OfficeCapabilityRegistry (spec section 7): what the agent can REALLY do per application, with evidence.

Rules this module enforces:
* an operation is `supported` only when (a) a tool implementing it is declared in the tool contract AND (b) the
  dependency it needs was found on this machine by a probe (COM ProgID registered and its server file present,
  python module importable, ...). Otherwise it is listed under `unsupported_operations` with the reason.
* the probes look at registration / importability only. They never start an Office application and never touch
  the network, so `verified_live` is always False: a registered Word that is licensed-out or broken is not detected.
* `installed` of an application that has no adapter does NOT make it available (spec 4.8: "opening the app" is not support).
* the Microsoft Graph adapter is a stub: available=False, reason "not configured/no credentials".
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from lga_worker.office.graph import GraphAdapter

METHOD_PRIORITY = ("graph_api", "object_model", "file_library", "ui_automation", "vision_mouse")
SPEC_FIELDS = ("application_id", "application_version", "execution_environment", "installed", "authenticated", "available",
               "supported_operations", "unsupported_operations", "required_permissions", "required_dependencies",
               "connectivity_requirement", "verification_methods", "known_limitations", "last_health_check")
OPERATION_FIELDS = ("operation_id", "input_schema", "output_schema", "preconditions", "side_effects", "timeout_policy", "retry_policy",
                    "idempotency", "verification_strategy", "rollback_strategy")


class NotSupported(Exception):
    """Raised by `require`: the operation has no evidence of working here."""


# ----------------------------------------------------------------------------------------- probes
class Probe(Protocol):
    def com_progid(self, progid: str) -> dict[str, Any] | None: ...
    def module(self, name: str) -> dict[str, Any] | None: ...
    def tool_contract(self, tool: str) -> dict[str, Any] | None: ...


def _repo_tools_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "tools"


class SystemProbe:
    """Looks at this machine. No application is started, nothing is imported except to read its version, no network."""

    def __init__(self, tools_dir: Path | None = None) -> None:
        self.tools_dir = tools_dir or _repo_tools_dir()
        self._contracts: dict[str, dict[str, Any]] | None = None

    def com_progid(self, progid: str) -> dict[str, Any] | None:
        if sys.platform != "win32":
            return None
        try:
            import winreg
        except ImportError:
            return None

        def read(root: Any, sub: str) -> str | None:
            try:
                with winreg.OpenKey(root, sub) as key:
                    return str(winreg.QueryValue(key, None))
            except OSError:
                return None

        clsid = read(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID")
        if not clsid:
            return None
        server = read(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32")
        path = None
        if server:
            server = os.path.expandvars(server.strip())
            path = server[1:server.index('"', 1)] if server.startswith('"') else server.split(" /")[0].strip()
        exists = bool(path) and Path(path).is_file()
        cur = read(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CurVer")
        version = cur.rsplit(".", 1)[-1] if cur and cur.rsplit(".", 1)[-1].isdigit() else None
        return {"progid": progid, "clsid": clsid, "server": path, "server_exists": exists, "version": f"{version}.0" if version else None}

    def module(self, name: str) -> dict[str, Any] | None:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            return None
        if spec is None:
            return None
        dist = {"docx": "python-docx", "pptx": "python-pptx", "win32com": "pywin32", "PIL": "Pillow", "fitz": "PyMuPDF",
                "extract_msg": "extract-msg", "olefile": "olefile"}.get(name, name)
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {"module": name, "version": version}

    def tool_contract(self, tool: str) -> dict[str, Any] | None:
        if self._contracts is None:
            self._contracts = {}
            for f in sorted(self.tools_dir.glob("*.json")):
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for raw in doc.get("tools", []):
                    self._contracts[raw["name"]] = {**raw, "_file": f.name}
        return self._contracts.get(tool)


# ----------------------------------------------------------------------------------------- catalogue
@dataclass(frozen=True)
class MethodImpl:
    method: str  # one of METHOD_PRIORITY
    tool: str | None = None  # tool of this repository that implements it; None = nothing exists
    progid: str | None = None  # COM ProgID that must be registered
    any_modules: tuple[str, ...] = ()  # at least one python module must import
    note: str = ""


@dataclass(frozen=True)
class OpDef:
    operation_id: str
    description: str
    methods: tuple[MethodImpl, ...] = ()
    preconditions: tuple[str, ...] = ("file path exists and is readable",)
    verification_strategy: str = "re-read the saved result with office.verify"
    rollback_strategy: str = "none (read only)"
    guard_level: int = 1


@dataclass(frozen=True)
class AppDef:
    application_id: str
    name: str
    progid: str | None = None
    modules: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()
    cloud_only: bool = False
    connectivity: str = "offline"
    permissions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    verification_methods: tuple[str, ...] = ()
    operations: tuple[OpDef, ...] = ()


def _com(tool: str, progid: str, note: str = "") -> MethodImpl:
    return MethodImpl("object_model", tool, progid=progid, note=note)


def _lib(tool: str, *modules: str, note: str = "") -> MethodImpl:
    return MethodImpl("file_library", tool, any_modules=modules, note=note)


def _missing(op_id: str, desc: str, level: int = 2) -> OpDef:
    return OpDef(op_id, desc, (), preconditions=("an adapter must be implemented first",), verification_strategy="n/a (not implemented)",
                 rollback_strategy="n/a", guard_level=level)


WORD = AppDef(
    "word", "Microsoft Word", "Word.Application", ("docx",), ("docx", "docm", "dotx", "doc", "rtf", "odt"),
    verification_methods=("office.verify (zip+XML re-read of the saved .docx)", "Word COM read-back (office.word_write contains_appended)"),
    limitations=("Word tools: text replace/append, TOC, Track Changes/comments, headers/footers/page numbers/margins/orientation; styles, compare versions have no tool",
                 "layout/page numbers/TOC freshness cannot be verified without Word (verify reports them NOT_RUN)"),
    operations=(
        OpDef("word.read_text", "Read the text of a Word document", (_com("office.word_read", "Word.Application"), _lib("document.read")),
              verification_strategy="n/a (read only)"),
        OpDef("word.write_text", "Replace text / append heading and paragraphs and save",
              (_com("office.word_write", "Word.Application"),), ("path writable", "work on a copy for existing documents"),
              "office.verify on the saved file + COM read-back", "copy-before-edit (guard) then restore the copy", 2),
        OpDef("word.export_pdf", "Export a document to PDF", (_com("office.export_pdf", "Word.Application"),),
              verification_strategy="office.verify on the PDF (header, %%EOF, page count)", rollback_strategy="delete the new PDF", guard_level=2),
        OpDef("word.verify_saved", "Re-read a saved .docx and check paragraphs, headings, tables, text", (_lib("office.verify"),),
              guard_level=1, verification_strategy="n/a (it is the verifier)"),
        OpDef("word.insert_toc", "Insert / refresh a table of contents and update fields", (_com("office.word_toc", "Word.Application"),),
              ("path writable", "work on a copy for existing documents"), "Word read-back (TOC entries == headings) + office.verify on the saved file",
              "copy-before-edit then restore the copy", 2),
        OpDef("word.track_changes", "Track Changes on/off, list revisions and comments, add a comment (accept/reject only when asked)",
              (_com("office.word_review", "Word.Application"),), ("path writable", "work on a copy for existing documents"),
              "Word read-back of TrackRevisions/revisions/comments + saved-file XML (w:ins/w:del, comments.xml)", "copy-before-edit then restore the copy", 2),
        _missing("word.compare_versions", "Compare two document versions"),
        OpDef("word.headers_footers", "Headers, footers, page numbers, margins, orientation", (_com("office.word_layout", "Word.Application"),),
              ("path writable", "work on a copy for existing documents"), "Word read-back + saved-file XML (sectPr, header/footer parts, PAGE field)",
              "copy-before-edit then restore the copy", 2),
    ),
)
EXCEL = AppDef(
    "excel", "Microsoft Excel", "Excel.Application", (), ("xlsx", "xlsm", "xlsb", "xls", "csv", "tsv"),
    verification_methods=("office.verify (zip+XML re-read: error values, formulas vs values, counts)", "Excel COM read-back of written cells"),
    limitations=("no calculation engine outside Excel: files written by libraries have no cached formula results (verify says so)",
                 "VBA, Power Query and PivotCharts have no tool; pivot/chart/format tools are PivotTable, embedded chart, number format, conditional format and validation only"),
    operations=(
        OpDef("excel.read_range", "Read cells (values and formulas)", (_com("office.excel_read", "Excel.Application"),
                                                                         MethodImpl("file_library", "document.read", note="cached values only")),
              verification_strategy="n/a (read only)"),
        OpDef("excel.write_range", "Write values/formulas to a range and save", (_com("office.excel_write", "Excel.Application"),),
              ("workbook not locked", "work on a copy for existing workbooks"), "COM read-back + office.verify with before=copy",
              "copy-before-edit then restore", 2),
        OpDef("excel.aggregate", "Exact sums/means/groups from CSV/XLSX/JSON", (MethodImpl("file_library", "data.aggregate"),),
              verification_strategy="n/a (read only; reports skipped cells)"),
        OpDef("excel.export_pdf", "Export a workbook to PDF", (_com("office.export_pdf", "Excel.Application"),),
              verification_strategy="office.verify on the PDF", rollback_strategy="delete the new PDF", guard_level=2),
        OpDef("excel.verify_saved", "Re-read a saved .xlsx and check errors/formulas/counts", (_lib("office.verify"),),
              verification_strategy="n/a (it is the verifier)"),
        OpDef("excel.pivot_table", "Create a PivotTable from a range and read its result", (_com("office.excel_pivot", "Excel.Application"),),
              ("workbook not locked", "work on a copy for existing workbooks"), "grand total and groups recomputed in Python from the source + pivot part in the saved package",
              "copy-before-edit then restore the copy", 2),
        OpDef("excel.chart", "Create an embedded chart from a range", (_com("office.excel_chart", "Excel.Application"),),
              ("workbook not locked", "work on a copy for existing workbooks"), "COM read-back (type, series, points) + chart part in the saved package",
              "copy-before-edit then restore the copy", 2),
        OpDef("excel.format_cells", "Number formats, conditional formatting, data validation", (_com("office.excel_format", "Excel.Application"),),
              ("workbook not locked", "work on a copy for existing workbooks"), "COM read-back + cfRule/dataValidation in the saved sheet XML",
              "copy-before-edit then restore the copy", 2),
        _missing("excel.vba", "Read/write VBA macros (would be level 4)", 4),
        _missing("excel.power_query", "Power Query / Data Model"),
    ),
)
POWERPOINT = AppDef(
    "powerpoint", "Microsoft PowerPoint", "PowerPoint.Application", ("pptx",), ("pptx", "pptm", "ppsx", "ppt"),
    verification_methods=("office.verify (every slide: bounds, overflow estimate, overlap, images)",),
    limitations=("slides are created/edited through the PowerPoint object model (text, bullets, notes only; no images/charts/themes/hyperlinks); text-overflow is an estimate from box size and font size",
                 "python-pptx is optional; verification uses zip+XML"),
    operations=(
        OpDef("powerpoint.read_text", "Read slide text", (MethodImpl("file_library", "document.read"), _com("office.ppt_read", "PowerPoint.Application")),
              verification_strategy="n/a (read only)"),
        OpDef("powerpoint.export_pdf", "Export a presentation to PDF", (_com("office.export_pdf", "PowerPoint.Application"),),
              verification_strategy="office.verify on the PDF", rollback_strategy="delete the new PDF", guard_level=2),
        OpDef("powerpoint.verify_saved", "Check every slide of a saved .pptx", (_lib("office.verify"),),
              verification_strategy="n/a (it is the verifier)"),
        OpDef("powerpoint.create_slides", "Create a new deck: title slide + content slides with bullets", (_com("office.ppt_create", "PowerPoint.Application"),),
              ("output path free (or overwrite)",), "PowerPoint read-back + saved-file XML + office.verify on every slide", "delete the new file", 2),
        OpDef("powerpoint.edit_slides", "Add a slide, replace text on slides", (_com("office.ppt_edit", "PowerPoint.Application"),),
              ("work on a copy for existing decks",), "PowerPoint read-back + saved-file XML + office.verify on every slide", "copy-before-edit then restore the copy", 2),
        OpDef("powerpoint.speaker_notes", "Set speaker notes", (_com("office.ppt_edit", "PowerPoint.Application"),),
              ("work on a copy for existing decks",), "notes re-read from PowerPoint and from the saved file", "copy-before-edit then restore the copy", 2),
        _missing("powerpoint.hyperlinks", "Hyperlinks, charts, images on slides"),
    ),
)
OUTLOOK = AppDef(
    "outlook", "Microsoft Outlook", "Outlook.Application", (), ("msg", "eml", "ics", "pst"), connectivity="offline_or_online",
    permissions=("Mailbox access of the signed-in Windows/Outlook profile",),
    verification_methods=("COM: draft EntryID returned and item found in Drafts", "Sent: only Outlook's acceptance; delivery is NOT verified"),
    limitations=("sending is level 3: always confirmed by the user", "real delivery needs a network and a reachable mail server",
                 "calendar, contacts, rules and folders have no tool", "needs an Outlook profile (classic Outlook); new Outlook has no COM"),
    operations=(
        OpDef("outlook.list_mail", "List mail in inbox/sent/drafts", (_com("office.outlook_list", "Outlook.Application"),),
              verification_strategy="n/a (read only)"),
        OpDef("outlook.read_mail", "Read one mail by EntryID", (_com("office.outlook_read", "Outlook.Application"),),
              verification_strategy="n/a (read only)"),
        OpDef("outlook.create_draft", "Create a draft and show it", (_com("office.outlook_draft", "Outlook.Application"),),
              ("recipients named by the user", "attachments exist and are not executable"), "draft_entry_id present", "delete the draft", 2),
        OpDef("outlook.send_mail", "Send a mail now", (_com("office.outlook_send", "Outlook.Application"),),
              ("explicit user request", "recipients, text and attachments reviewed", "online or Outlook queue"),
              "Outlook accepted; check Sent Items for evidence, never retry blindly", "none (a sent mail cannot be recalled)", 3),
        _missing("outlook.calendar", "Create / update calendar events", 3),
        _missing("outlook.contacts", "Contacts"),
        _missing("outlook.rules", "Mailbox rules and folders", 4),
    ),
)
PDF = AppDef(
    "pdf", "PDF", None, ("pypdf",), ("pdf",),
    verification_methods=("office.verify (header, %%EOF, page count, text extraction)",),
    limitations=("scanned PDFs have no text layer: OCR is not part of the verifier (reported NOT_RUN)", "no PDF editing tool"),
    operations=(
        OpDef("pdf.read_text", "Read PDF text", (MethodImpl("file_library", "document.read", any_modules=()),), verification_strategy="n/a (read only)"),
        OpDef("pdf.verify_saved", "Check a PDF: exists, valid, page count, text", (_lib("office.verify"),),
              verification_strategy="n/a (it is the verifier)"),
        _missing("pdf.edit", "Merge/split/annotate/fill forms"),
    ),
)


def _stub_app(app_id: str, name: str, progid: str | None, ops: tuple[tuple[str, str, int], ...], *, cloud: bool = False,
              perms: tuple[str, ...] = (), ext: tuple[str, ...] = (), note: str = "") -> AppDef:
    return AppDef(app_id, name, progid, (), ext, cloud_only=cloud, connectivity="online" if cloud else "offline", permissions=perms,
                  limitations=(note or "no adapter implemented in this repository; the application is not claimed as supported",),
                  verification_methods=(), operations=tuple(_missing(f"{app_id}.{o}", d, lv) for o, d, lv in ops))


ONENOTE = _stub_app("onenote", "Microsoft OneNote", "OneNote.Application", (("read_pages", "Read notebooks/sections/pages", 1), ("create_page", "Create a page", 2)),
                    ext=("one",), perms=("Notes.Read", "Notes.ReadWrite (Graph)"))
ACCESS = _stub_app("access", "Microsoft Access", "Access.Application", (("read_schema", "Read tables/fields/relations", 1), ("query_sql", "SQL query", 1),
                                                                            ("update_records", "Create/update records", 2)),
                   ext=("accdb", "mdb"), note="needs the Access Database Engine + driver (pyodbc); none verified here")
PUBLISHER = _stub_app("publisher", "Microsoft Publisher", "Publisher.Application", (("open_and_export", "Open and export to PDF", 2),), ext=("pub",),
                      note="Publisher is not part of every Microsoft 365 plan; no adapter")
VISIO = _stub_app("visio", "Microsoft Visio", "Visio.Application", (("read_drawing", "Read shapes/connectors", 1), ("create_diagram", "Create a diagram", 2)),
                  ext=("vsdx", "vsd"))
PROJECT = _stub_app("project", "Microsoft Project", "MSProject.Application", (("read_plan", "Read tasks/dependencies", 1), ("update_plan", "Update the plan", 2)),
                    ext=("mpp",), note="Project desktop, Project Online and Planner use different data models; no adapter")
ONEDRIVE_SP = _stub_app("onedrive_sharepoint", "OneDrive / SharePoint", None, (("list_files", "Browse/search files", 1), ("download", "Download", 1),
                                                                                  ("upload", "Upload / change cloud data", 3), ("share", "Share a file", 3)),
                        cloud=True, perms=("Files.Read", "Files.ReadWrite", "Sites.Read.All"))
TEAMS = _stub_app("teams", "Microsoft Teams", None, (("read_messages", "Search messages/meetings", 1), ("send_message", "Send a message", 3)),
                  cloud=True, perms=("Chat.Read", "ChannelMessage.Send"))
FORMS = _stub_app("forms", "Microsoft Forms", None, (("read_responses", "Read responses", 1), ("create_form", "Create a form", 2)), cloud=True,
                  perms=("Forms.Read",))
LISTS_PLANNER = _stub_app("lists_planner", "Microsoft Lists / Planner", None, (("read_tasks", "Read tasks", 1), ("update_tasks", "Create/update tasks", 3)),
                          cloud=True, perms=("Tasks.ReadWrite", "Sites.ReadWrite.All"))
POWER_AUTOMATE = _stub_app("power_automate", "Power Automate", None, (("analyse_flow", "Analyse a flow", 1), ("create_flow", "Create a flow", 3)), cloud=True,
                           perms=("Flows.Read.All", "Flows.Manage.All"))
POWER_BI = _stub_app("power_bi", "Power BI", None, (("read_report", "Read reports/datasets", 1), ("publish", "Publish / refresh", 3)), cloud=True,
                     perms=("Report.Read.All", "Dataset.ReadWrite.All"))
POWER_APPS = _stub_app("power_apps", "Power Apps", None, (("read_app", "Read app structure", 1), ("publish", "Publish an app", 3)), cloud=True,
                       perms=("PowerApps service rights",))

APPS: tuple[AppDef, ...] = (WORD, EXCEL, POWERPOINT, OUTLOOK, PDF, ONENOTE, ACCESS, PUBLISHER, VISIO, PROJECT, ONEDRIVE_SP, TEAMS, FORMS,
                            LISTS_PLANNER, POWER_AUTOMATE, POWER_BI, POWER_APPS)
APP_INDEX = {a.application_id: a for a in APPS}


# ----------------------------------------------------------------------------------------- registry
@dataclass
class MethodEvaluation:
    method: str
    tool: str | None
    ok: bool
    reasons: list[str]
    evidence: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class OfficeCapabilityRegistry:
    def __init__(self, probe: Probe | None = None, graph: GraphAdapter | None = None, apps: tuple[AppDef, ...] | None = None) -> None:
        self.probe: Probe = probe or SystemProbe()
        self.graph = graph or GraphAdapter()
        self.apps = apps or APPS
        self._cache: dict[str, dict[str, Any]] = {}
        self.last_health_check: str | None = None

    # --- evaluation
    def _evaluate(self, impl: MethodImpl, app: AppDef) -> MethodEvaluation:
        reasons: list[str] = []
        evidence: list[str] = []
        if impl.method == "graph_api":
            status = self.graph.status()
            return MethodEvaluation(impl.method, impl.tool, bool(status["available"]), [] if status["available"] else [status["reason"]])
        if impl.tool is None:
            return MethodEvaluation(impl.method, None, False, ["not implemented: no tool in this repository"])
        contract = self.probe.tool_contract(impl.tool)
        if contract is None:
            reasons.append(f"tool {impl.tool} is not declared in the tool contract")
        else:
            evidence.append(f"tool {impl.tool} declared in tools/{contract['_file']}")
            if impl.tool == "document.read":
                evidence.append("runs in the Company Brain interpreter (core-native tool); its libraries are not probed here")
        if impl.progid:
            com = self.probe.com_progid(impl.progid)
            if com is None:
                reasons.append(f"COM ProgID {impl.progid} is not registered (application not installed, or not Windows)")
            elif not com["server_exists"]:
                reasons.append(f"COM ProgID {impl.progid} is registered but its server file {com.get('server')} is missing")
            else:
                evidence.append(f"COM ProgID {impl.progid} registered, server {com['server']} exists" + (f", version {com['version']}" if com.get("version") else ""))
        if impl.any_modules:
            found = [m for m in impl.any_modules if self.probe.module(m)]
            if found:
                evidence.append("python module(s) importable: " + ", ".join(found))
            else:
                reasons.append("none of the python modules is installed: " + ", ".join(impl.any_modules))
        return MethodEvaluation(impl.method, impl.tool, not reasons, reasons, evidence)

    def _operation_record(self, op: OpDef, app: AppDef, full: bool) -> tuple[bool, dict[str, Any]]:
        evals = sorted((self._evaluate(m, app) for m in op.methods), key=lambda e: METHOD_PRIORITY.index(e.method))
        chosen = next((e for e in evals if e.ok), None)
        base: dict[str, Any] = {"operation_id": op.operation_id, "description": op.description}
        if chosen is None:
            reasons = [r for e in evals for r in e.reasons] or ["not implemented: no method exists in this repository"]
            base.update(supported=False, reasons=reasons, guard_level=op.guard_level)
            if full:
                base.update(self._schema_fields(op, None))
            return False, base
        base.update(supported=True, method=chosen.method, tool=chosen.tool, evidence=chosen.evidence, verified_live=False,
                    alternatives=[{"method": e.method, "tool": e.tool, "ok": e.ok, "reasons": e.reasons} for e in evals if e is not chosen],
                    guard_level=op.guard_level)
        if full:
            base.update(self._schema_fields(op, chosen.tool))
        return True, base

    def _schema_fields(self, op: OpDef, tool: str | None) -> dict[str, Any]:
        contract = self.probe.tool_contract(tool) if tool else None
        idempotent = bool(contract["idempotent"]) if contract else None
        retry = ({"policy": "transient_errors_only", "max_attempts": 3, "backoff_ms": 500} if idempotent
                 else {"policy": "never_blind_retry", "on_timeout": "check the real state first, then decide"})
        return {
            "input_schema": contract["input_schema"] if contract else None,
            "output_schema": contract["output_schema"] if contract else None,
            "preconditions": list(op.preconditions),
            "side_effects": contract["side_effects"] if contract else ("none" if op.guard_level == 1 else "unknown"),
            "timeout_policy": {"timeout_ms": contract["timeout_ms"]} if contract else None,
            "retry_policy": retry if contract else None,
            "idempotency": idempotent,
            "verification_strategy": op.verification_strategy,
            "rollback_strategy": op.rollback_strategy,
        }

    def _health(self, app: AppDef, full: bool) -> dict[str, Any]:
        supported: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        for op in app.operations:
            ok, rec = self._operation_record(op, app, full)
            (supported if ok else unsupported).append(rec)
        com = self.probe.com_progid(app.progid) if app.progid else None
        modules = {m: self.probe.module(m) for m in app.modules}
        envs: list[str] = []
        if com and com["server_exists"]:
            envs.append("desktop_com")
        if any(modules.values()) or any(op["method"] == "file_library" for op in supported):
            envs.append("file_library")
        if app.cloud_only:
            envs.append("cloud_graph (not configured)")
        installed = bool(com and com["server_exists"]) if app.progid else (bool(any(modules.values())) if app.modules else (False if app.cloud_only else None))
        if app.cloud_only:
            installed = False  # a cloud service is not installed; "reachable and authenticated" is the question
        version = (com or {}).get("version")
        if not version:
            ver = next((m["version"] for m in modules.values() if m and m.get("version")), None)
            version = f"library {ver}" if ver else None
        record = {
            "application_id": app.application_id, "name": app.name, "application_version": version,
            "execution_environment": envs[0] if envs else "none", "environments": envs,
            "installed": installed, "authenticated": False if app.cloud_only else None,
            "available": bool(supported), "supported_operations": supported, "unsupported_operations": unsupported,
            "required_permissions": list(app.permissions), "required_dependencies": self._dependencies(app),
            "connectivity_requirement": app.connectivity, "verification_methods": list(app.verification_methods),
            "known_limitations": list(app.limitations) + (["installed but no adapter exists: NOT claimed as supported"]
                                                          if installed and not supported else []),
            "last_health_check": _now(),
            "evidence": {"com": com, "modules": {k: v for k, v in modules.items()}, "probe": "registration/importability only; nothing was started"},
        }
        return record

    def _dependencies(self, app: AppDef) -> list[str]:
        deps = []
        if app.progid:
            deps.append(f"COM ProgID {app.progid} (desktop application, Windows)")
        deps.extend(f"python module {m}" for m in app.modules)
        if app.cloud_only:
            deps.append("Microsoft Graph credentials (OAuth) - not configured")
        return deps

    # --- API
    def refresh(self, full: bool = False) -> dict[str, dict[str, Any]]:
        self._cache = {a.application_id: self._health(a, full) for a in self.apps}
        self.last_health_check = _now()
        return self._cache

    def snapshot(self, app: str | None = None, detail: str = "summary", refresh: bool = False) -> dict[str, Any]:
        full = detail == "full"
        defs = [a for a in self.apps if app is None or a.application_id == app]
        if not defs:
            raise KeyError(f"unknown application {app!r}; known: {', '.join(a.application_id for a in self.apps)}")
        if full or app or refresh or not self._cache:
            data = {a.application_id: self._health(a, full) for a in defs}
            if app is None and not full:
                self._cache = data
                self.last_health_check = _now()
        else:
            data = self._cache
        if not full:
            data = {k: self._summarise(v) for k, v in data.items()}
        return {"generated": _now(), "graph": self.graph.status(), "applications": list(data.values()),
                "summary": {"available": [k for k, v in data.items() if v["available"]],
                            "installed_without_adapter": [k for k, v in data.items() if v["installed"] and not v["available"]],
                            "not_available": [k for k, v in data.items() if not v["available"]]}}

    @staticmethod
    def _summarise(rec: dict[str, Any]) -> dict[str, Any]:
        out = dict(rec)
        out["supported_operations"] = [{"operation_id": o["operation_id"], "method": o["method"], "tool": o["tool"]} for o in rec["supported_operations"]]
        out["unsupported_operations"] = [{"operation_id": o["operation_id"], "reason": o["reasons"][0]} for o in rec["unsupported_operations"]]
        return out

    def get(self, app_id: str) -> dict[str, Any]:
        defs = {a.application_id: a for a in self.apps}
        if app_id not in defs:
            raise KeyError(f"unknown application {app_id!r}")
        return self._health(defs[app_id], True)

    def can(self, app_id: str, operation_id: str) -> tuple[bool, str]:
        """(supported, reason/evidence). Query this BEFORE executing: no evidence means no claim of support."""
        defs = {a.application_id: a for a in self.apps}
        app = defs.get(app_id)
        if app is None:
            return False, f"unknown application {app_id!r}"
        for op in app.operations:
            if op.operation_id == operation_id:
                ok, rec = self._operation_record(op, app, False)
                return (True, "; ".join(rec["evidence"])) if ok else (False, "; ".join(rec["reasons"]))
        return False, f"operation {operation_id!r} is not catalogued for {app_id}"

    def require(self, app_id: str, operation_id: str) -> str:
        ok, why = self.can(app_id, operation_id)
        if not ok:
            raise NotSupported(f"{operation_id} is not supported here: {why}")
        return why


def validate_record_shape(record: dict[str, Any]) -> list[str]:
    """Names of spec fields missing from an application record (used by tests and by the tool)."""
    return [f for f in SPEC_FIELDS if f not in record]


def operation_shape_problems(op: dict[str, Any]) -> list[str]:
    return [f for f in OPERATION_FIELDS if f not in op]


__all__ = ["OfficeCapabilityRegistry", "SystemProbe", "NotSupported", "METHOD_PRIORITY", "APPS", "APP_INDEX"]
