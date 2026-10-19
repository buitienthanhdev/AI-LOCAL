"""OfficeIntentRouter (spec sections 2.1, 6, 10): request text + capability registry -> application + method + why.

Deterministic and keyword based (Vietnamese with or without accents, and English): no model call, so the same request
and registry always give the same route and the choice can be explained. The router never executes anything.

Method priority (spec 2.1): official API/Graph > object model (COM) > file library > UI Automation > vision + mouse.
A method is chosen only when the registry has evidence for it; otherwise the reason is reported.

Ambiguity produces a clarification question (short question, options, multi-select when it fits, a default with the
reason, an "other" option) instead of a guess. Recipients, files to overwrite and cloud-vs-local scope are never guessed.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from lga_worker.office.registry import METHOD_PRIORITY, OfficeCapabilityRegistry

VI_CHARS = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\s\"'<>|?*]+|\"[^\"]+\.[A-Za-z0-9]{2,5}\"|'[^']+\.[A-Za-z0-9]{2,5}'|\b[\w-]+(?:\.[\w-]+)*\.(?:docx?|xlsx?|xlsm|csv|tsv|pptx?|pdf|msg|eml|accdb|mdb|vsdx?|mpp|pub|one)\b)", re.I)
EXT_APP = {"doc": "word", "docx": "word", "docm": "word", "xls": "excel", "xlsx": "excel", "xlsm": "excel", "csv": "excel", "tsv": "excel",
           "ppt": "powerpoint", "pptx": "powerpoint", "pdf": "pdf", "msg": "outlook", "eml": "outlook", "ics": "outlook", "accdb": "access",
           "mdb": "access", "vsd": "visio", "vsdx": "visio", "mpp": "project", "pub": "publisher", "one": "onenote"}

# (application, weight, regex on accent-free lower-case text)
SIGNALS: tuple[tuple[str, int, str], ...] = (
    ("word", 3, r"word|docx|microsoft word"), ("word", 2, r"van ban|soan thao|hop dong|bien ban|cong van|muc luc|heading|tieu de|contract|memo|letter"),
    ("word", 1, r"bao cao|report|tai lieu|document|de xuat|proposal|huong dan|bien soan"),
    ("excel", 3, r"excel|xlsx|xlsm|csv|tsv|spreadsheet|bang tinh|workbook"),
    ("excel", 2, r"cong thuc|formula|pivot|worksheet|\bsheet\b|vlookup|xlookup|sumifs|countifs|du toan|so lieu|doanh thu"),
    ("excel", 1, r"bang\b|table|thong ke|tong hop|bao cao|\bcot\b|\bdong\b|\bcell\b"),
    ("powerpoint", 3, r"powerpoint|pptx|\bppt\b|slides?\b|trinh chieu|thuyet trinh"), ("powerpoint", 2, r"presentation|\bdeck\b"), ("powerpoint", 1, r"bao cao"),
    ("outlook", 3, r"outlook|e-?mail|\bmail\b|thu dien tu|gui thu|hop thu|inbox"),
    ("outlook", 2, r"lich hop|calendar|cuoc hop|\bmeeting\b|danh ba|\bcontacts?\b|dinh kem|attachments?|chuyen tiep|\bforward\b|\bthu\b"), ("outlook", 1, r"\bgui\b|\bsend\b"),
    ("onenote", 3, r"onenote|so tay|notebook"), ("onenote", 2, r"ghi chu|\bnotes?\b"),
    ("access", 3, r"\baccess\b|accdb|\bmdb\b"), ("access", 2, r"co so du lieu|database|\bcsdl\b|\bsql\b"),
    ("publisher", 3, r"publisher|\.pub\b"), ("publisher", 2, r"brochure|to roi|flyer|catalogue|thiep"),
    ("visio", 3, r"visio|vsdx"), ("visio", 2, r"flowchart|so do|bpmn|org chart|network diagram"),
    ("project", 3, r"ms project|microsoft project|\bmpp\b|gantt"), ("project", 2, r"tien do|critical path|milestone|ke hoach du an|project plan|baseline"),
    ("pdf", 3, r"\bpdf\b"),
    ("onedrive_sharepoint", 3, r"onedrive|sharepoint"), ("onedrive_sharepoint", 2, r"cloud|luu tru dam may|thu vien tai lieu|1drv"),
    ("teams", 3, r"\bteams\b|microsoft teams"), ("teams", 2, r"\bchannel\b|\bkenh\b|tin nhan nhom"),
    ("forms", 3, r"microsoft forms|khao sat|\bsurvey\b"), ("lists_planner", 3, r"planner|microsoft lists"),
    ("power_bi", 3, r"power ?bi|\bdax\b"), ("power_automate", 3, r"power automate|tu dong hoa quy trinh"), ("power_apps", 3, r"power apps"),
)
OPERATIONS: tuple[tuple[str, str], ...] = (
    ("delete", r"\bxoa\b|\bdelete\b|\bremove\b|go bo|\berase\b"),
    ("share", r"chia se|\bshare\b|\binvite\b|\bmoi\b|\bpublish\b|xuat ban"),
    ("send", r"\bgui\b|\bsend\b|\bforward\b|chuyen tiep|tra loi|\breply\b"),
    ("convert", r"\bxuat\b|chuyen sang|chuyen thanh|\bconvert\b|\bexport\b|save as"),
    ("verify", r"kiem tra|xac minh|\bverify\b|\bcheck\b|\bvalidate\b|doi chieu"),
    ("create", r"\btao\b|soan|\bviet\b|\blap\b|xay dung|\bcreate\b|\bmake\b|\bgenerate\b|\bdraft\b|\bbuild\b|\bnew\b"),
    ("edit", r"\bsua\b|chinh|cap nhat|thay the|\bthem\b|dinh dang|\bedit\b|\bupdate\b|\breplace\b|\bmodify\b|\bchange\b|\bformat\b|\bfix\b|\bappend\b|\binsert\b"),
    ("read", r"(?<![.\w])doc(?![\w.])|\bxem\b|\bmo\b|phan tich|tom tat|trich xuat|liet ke|\btim\b|tra cuu|\bread\b|\bshow\b|\bview\b|analy[sz]e|summar|\bextract\b|\blist\b|\bfind\b|\bsearch\b|\bopen\b"),
)
OP_PRIORITY = ("delete", "share", "send")  # the riskiest named intent wins when several appear
LINKERS = re.compile(r"\brồi\b|\broi\b|sau do|\bthen\b|\band then\b|\btu\b.+\b(sang|thanh|vao|into|to)\b|\bfrom\b.+\b(into|to)\b|->|\bva\b|\band\b|\bkem\b|\bwith\b")
# intent -> registry operation id by application
OP_MAP: dict[tuple[str, str], str] = {
    ("word", "read"): "word.read_text", ("word", "edit"): "word.write_text", ("word", "create"): "word.write_text", ("word", "convert"): "word.export_pdf",
    ("word", "verify"): "word.verify_saved",
    ("excel", "read"): "excel.read_range", ("excel", "edit"): "excel.write_range", ("excel", "create"): "excel.write_range", ("excel", "convert"): "excel.export_pdf",
    ("excel", "verify"): "excel.verify_saved",
    ("powerpoint", "read"): "powerpoint.read_text", ("powerpoint", "create"): "powerpoint.create_slides", ("powerpoint", "edit"): "powerpoint.edit_slides",
    ("powerpoint", "convert"): "powerpoint.export_pdf", ("powerpoint", "verify"): "powerpoint.verify_saved",
    ("outlook", "read"): "outlook.list_mail", ("outlook", "create"): "outlook.create_draft", ("outlook", "send"): "outlook.send_mail",
    ("pdf", "read"): "pdf.read_text", ("pdf", "verify"): "pdf.verify_saved", ("pdf", "edit"): "pdf.edit",
}
CALENDAR = re.compile(r"lich|calendar|cuoc hop|\bmeeting\b|appointment")
TEMPLATE_HINTS = (("weekly_report", r"bao cao tuan|weekly report|bao cao dinh ky"), ("project_plan", r"ke hoach du an|project plan|lap ke hoach|planner"),
                  ("email_doc_analysis", r"(email|mail|thu).*(dinh kem|attachment|tai lieu|document)|(dinh kem|attachment).*(email|mail|thu)"))


def fold(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower().replace("đ", "d").replace("Đ", "d"))
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn")


def detect_language(text: str, hint: str | None = None) -> str:
    if hint in ("vi", "en"):
        return hint
    if VI_CHARS.search(text):
        return "vi"
    return "vi" if re.search(r"\b(gui|tao|bao cao|tep|sua|cap nhat|xoa|giup|toi|khong)\b", fold(text)) else "en"


def _app_scores(folded: str, raw: str, files: list[str]) -> dict[str, dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}

    def bump(app: str, w: int, why: str) -> None:
        s = scores.setdefault(app, {"score": 0, "matched": []})
        s["score"] += w
        if why not in s["matched"]:
            s["matched"].append(why)

    for app, w, pattern in SIGNALS:
        m = re.search(pattern, folded)
        if m:
            bump(app, w, m.group(0).strip())
    for ext in re.findall(r"\.([a-z0-9]{2,5})\b", folded):
        if ext in EXT_APP:
            bump(EXT_APP[ext], 3, "." + ext)
    for f in files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if ext in EXT_APP:
            bump(EXT_APP[ext], 3, "file " + f.replace("\\", "/").rsplit("/", 1)[-1])
    return scores


def _question(vi: bool, kind: str, text: str, options: list[tuple[str, str, str]], *, multi: bool = False, default: str | None = None,
              default_why: str = "", free_text: bool = False) -> dict[str, Any]:
    opts = [{"id": i, "label": label, "description": desc} for i, label, desc in options]
    opts.append({"id": "other", "label": "Khác" if vi else "Other", "description": "Nhập phương án khác" if vi else "Type another answer", "free_text": True})
    return {"kind": kind, "question": text, "options": opts, "multi_select": multi, "default": default, "default_reason": default_why,
            "allow_free_text": True, "can_cancel": True, "free_text_only": free_text}


def route(request: str, registry: OfficeCapabilityRegistry | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = dict(context or {})
    reg = registry or OfficeCapabilityRegistry()
    files = [str(f) for f in (ctx.get("files") or ctx.get("candidate_files") or [])]
    raw = request or ""
    folded = fold(raw)
    vi = detect_language(raw, ctx.get("language")) == "vi"
    scores = _app_scores(folded, raw, files)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    apps = [{"application_id": a, "score": s["score"], "matched": s["matched"][:6]} for a, s in ranked]
    ops_found = [name for name, pat in OPERATIONS if re.search(pat, folded)]
    primary_op = next((o for o in OP_PRIORITY if o in ops_found), None)
    if primary_op is None and ops_found:
        first = min(ops_found, key=lambda o: re.search(dict(OPERATIONS)[o], folded).start())  # type: ignore[union-attr]
        primary_op = first
    strong = [a for a, s in ranked if s["score"] >= 3]
    explicit_paths = PATH_RE.findall(raw) + files
    emails = EMAIL_RE.findall(raw) + [str(r) for r in (ctx.get("recipients") or [])]
    cross = len(strong) >= 2 and bool(LINKERS.search(folded) or len(strong) >= 3)
    template = next((t for t, pat in TEMPLATE_HINTS if re.search(pat, folded)), None) if cross or len(strong) >= 2 else None
    result: dict[str, Any] = {"request": raw[:500], "language": "vi" if vi else "en", "applications": apps, "operations": ops_found, "operation": primary_op,
                              "cross_application": cross, "workflow_template": template, "primary_app": None, "method": None, "operation_id": None,
                              "needs_clarification": False, "clarification": None, "clarifications": [], "why": [], "suggested_tools": [], "guard_level_hint": None,
                              "files_mentioned": explicit_paths[:10], "recipients_mentioned": emails[:10]}
    qs: list[dict[str, Any]] = []
    # ---- application
    if not ranked:
        qs.append(_question(vi, "application", "Bạn muốn làm việc với ứng dụng hoặc loại tệp nào?" if vi else "Which application or file type do you mean?",
                            [("word", "Word (văn bản)" if vi else "Word (document)", ""), ("excel", "Excel (bảng tính)" if vi else "Excel (spreadsheet)", ""),
                             ("powerpoint", "PowerPoint (trình chiếu)" if vi else "PowerPoint (slides)", ""), ("outlook", "Outlook (email/lịch)" if vi else "Outlook (mail/calendar)", ""),
                             ("pdf", "PDF", "")], multi=True, default=None, default_why=""))
    elif not cross:
        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        if top[1]["score"] < 2 or (second and second[1]["score"] >= 1 and top[1]["score"] - second[1]["score"] <= 1 and top[1]["score"] < 3):
            cand = [a for a, s in ranked if s["score"] >= max(1, top[1]["score"] - 1)][:5]
            qs.append(_question(vi, "application", "Yêu cầu này nên làm bằng ứng dụng nào?" if vi else "Which application should this use?",
                                [(a, a, "gợi ý từ: " + ", ".join(scores[a]["matched"][:3]) if vi else "hint: " + ", ".join(scores[a]["matched"][:3])) for a in cand],
                                multi=primary_op in ("create", None), default=top[0],
                                default_why=("khớp nhiều nhất với yêu cầu" if vi else "best match for the wording")))
        else:
            result["primary_app"] = top[0]
    else:
        result["primary_app"] = strong[0]
    result["involved_apps"] = strong if cross else ([result["primary_app"]] if result["primary_app"] else [])
    app = result["primary_app"]
    # ---- other missing information (never guessed)
    needs_file = primary_op in ("edit", "convert", "verify", "delete") or (primary_op == "read" and app not in ("outlook", "teams", "onedrive_sharepoint"))
    names = [p.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower() for p in explicit_paths]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup and len({p for p in explicit_paths if p.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower() in dup}) > 1:
        cands = [p for p in files if p.replace("\\", "/").rsplit("/", 1)[-1].lower() in dup]
        qs.append(_question(vi, "which_file", f"Có nhiều tệp cùng tên «{dup[0]}». Dùng tệp nào?" if vi else f"Several files are called '{dup[0]}'. Which one?",
                            [(c, c, "") for c in cands], multi=False, default=None))
    elif needs_file and not explicit_paths and not cross:
        qs.append(_question(vi, "which_file", "Tệp nào cần xử lý? Hãy cho đường dẫn đầy đủ." if vi else "Which file? Please give the full path.", [], free_text=True))
    if primary_op in ("send", "share") and not emails and app in ("outlook", None, "teams", "onedrive_sharepoint"):
        qs.append(_question(vi, "recipients", "Gửi cho ai? (không tự đoán người nhận)" if vi else "Who are the recipients? (never guessed)", [], free_text=True))
    if ctx.get("output_exists") and primary_op in ("create", "convert", "edit"):
        qs.append(_question(vi, "overwrite", "Tệp đích đã tồn tại. Ghi đè hay tạo bản mới?" if vi else "The target already exists. Overwrite or create a new file?",
                            [("new_copy", "Tạo bản mới" if vi else "Create a new file", ""), ("overwrite", "Ghi đè (sau khi sao lưu)" if vi else "Overwrite (after a backup copy)", "")],
                            default="new_copy", default_why="an toàn: không mất dữ liệu cũ" if vi else "safe: nothing is lost"))
    cloud_mention = bool(re.search(r"onedrive|sharepoint|cloud|dam may", folded))
    if cloud_mention and primary_op in ("edit", "delete", "create") and not re.search(r"ban sao|\bcopy\b|\blocal\b|tren may|chi cloud|cloud only", folded):
        qs.append(_question(vi, "cloud_scope", "Cập nhật dữ liệu trên cloud hay chỉ sửa bản local?" if vi else "Update the cloud data or only edit a local copy?",
                            [("local_copy", "Chỉ sửa bản sao local" if vi else "Edit a local copy only", ""), ("cloud", "Cập nhật trên cloud (ảnh hưởng người khác)" if vi else "Update the cloud (affects others)", "")],
                            default="local_copy", default_why="an toàn, có thể phục hồi" if vi else "safe and reversible"))
    templates = ctx.get("templates") or []
    if primary_op == "create" and app in ("word", "powerpoint") and len(templates) > 1 and not ctx.get("template"):
        qs.append(_question(vi, "template", "Dùng mẫu nào?" if vi else "Which template?", [(str(t), str(t), "") for t in templates[:8]], default=None))
    result["clarifications"] = qs
    if qs:
        result.update(needs_clarification=True, clarification=qs[0])
    # ---- method selection (needs an application)
    if cross:
        result["why"].append(f"{len(strong)} applications are involved ({', '.join(strong)}): plan it as a workflow with dependencies"
                             + (f" (template {template})" if template else ""))
        result["suggested_tools"] = ["office.workflow_plan", "office.capabilities"]
        result["steps_support"] = {a: reg.snapshot(a, detail="summary")["applications"][0]["available"] for a in strong if a in {x.application_id for x in reg.apps}}
    elif app:
        _choose_method(result, reg, app, primary_op or "read", folded)
    if primary_op in ("send", "share", "delete") or (app == "outlook" and primary_op == "send"):
        result["suggested_tools"] = sorted(set(result["suggested_tools"] + ["office.check_action"]))
    return result


def _choose_method(result: dict[str, Any], reg: OfficeCapabilityRegistry, app: str, intent: str, folded: str) -> None:
    op_id = OP_MAP.get((app, intent))
    if app == "outlook" and CALENDAR.search(folded) and intent != "send":
        op_id = "outlook.calendar"
    try:
        snap = reg.snapshot(app, detail="full")["applications"][0]
    except KeyError:
        result["why"].append(f"application {app} is not in the registry")
        return
    if op_id is None:
        generic = [o for o in snap["supported_operations"] + snap["unsupported_operations"]]
        want_level = {"read": 1, "verify": 1}.get(intent, 2)
        pick = next((o for o in generic if o.get("guard_level") == want_level), generic[0] if generic else None)
        op_id = pick["operation_id"] if pick else None
    result["operation_id"] = op_id
    rec = next((o for o in snap["supported_operations"] if o["operation_id"] == op_id), None)
    unsup = next((o for o in snap["unsupported_operations"] if o["operation_id"] == op_id), None)
    graph = reg.snapshot(detail="summary")["graph"]
    considered: list[dict[str, Any]] = []
    cloud_relevant = app in ("outlook", "onedrive_sharepoint", "teams", "forms", "lists_planner", "power_automate", "power_bi", "power_apps", "onenote", "project")
    considered.append({"method": "graph_api", "available": bool(graph["available"]),
                       "reason": "ok" if graph["available"] else (graph["reason"] if cloud_relevant else f"not applicable to a local file; also {graph['reason']}")})
    if rec:
        for alt in [{"method": rec["method"], "tool": rec["tool"], "ok": True, "reasons": []}] + rec.get("alternatives", []):
            considered.append({"method": alt["method"], "tool": alt.get("tool"), "available": alt["ok"], "reason": "ok" if alt["ok"] else "; ".join(alt["reasons"])})
        level = rec.get("guard_level")
    else:
        reasons = unsup["reasons"] if unsup else ["operation is not catalogued"]
        considered.append({"method": "object_model/file_library", "available": False, "reason": "; ".join(reasons)})
        level = unsup.get("guard_level") if unsup else None
    installed = bool(snap["installed"])
    considered.append({"method": "ui_automation", "available": installed and not rec, "reason": ("generic desktop.* tools exist, but nothing here was verified for this "
                       "application; only a fallback") if installed else "application not installed"})
    considered.append({"method": "vision_mouse", "available": installed and not rec, "reason": "last resort (screenshots + mouse); slow and fragile"})
    considered.sort(key=lambda c: METHOD_PRIORITY.index(c["method"]) if c["method"] in METHOD_PRIORITY else 1)
    result["guard_level_hint"] = level
    if rec:
        better = [c for c in considered if c["method"] in METHOD_PRIORITY and METHOD_PRIORITY.index(c["method"]) < METHOD_PRIORITY.index(rec["method"]) and not c["available"]]
        result["method"] = {"selected": rec["method"], "tool": rec["tool"], "evidence": rec["evidence"], "verified_live": False, "considered": considered}
        result["why"].append(f"{op_id} via {rec['method']} ({rec['tool']}): " + ("; ".join(f"{c['method']} unavailable ({c['reason']})" for c in better) or "highest-priority method with evidence")
                             + "; evidence is registration/importability only, not a live run")
        result["suggested_tools"] = [rec["tool"]] + (["office.detect"] if intent in ("read", "edit", "verify") else []) + (["office.verify"] if intent in ("create", "edit", "convert") else [])
        if intent in ("edit", "delete"):
            result["suggested_tools"].append("office.check_action")
    elif installed:
        result["method"] = {"selected": "ui_automation", "tool": None, "evidence": [], "verified_live": False, "fallback": True, "considered": considered}
        result["why"].append(f"{op_id or intent} has no adapter ({'; '.join(unsup['reasons']) if unsup else 'not catalogued'}); the application is installed so "
                             "UI Automation (desktop.*) is a possible but unverified last-resort route - say so to the user")
        result["suggested_tools"] = ["desktop.inspect_ui", "office.check_action"]
    else:
        result["method"] = {"selected": None, "tool": None, "evidence": [], "considered": considered}
        result["why"].append(f"cannot do {op_id or intent} for {app}: " + ("; ".join(unsup["reasons"]) if unsup else "not supported") + "; the application is not installed")
