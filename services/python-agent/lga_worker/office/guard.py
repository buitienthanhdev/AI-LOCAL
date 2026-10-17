"""OfficePermissionGuard (spec section 11): classify an operation into level 1-4 and decide ALLOW / CONFIRM / BLOCK.

The guard is advisory for the planner and deterministic: the same operation + params + context always give the same
verdict. It never executes anything. The Rust core's policy (policy/default.toml) still has the last word on tools.

Levels  1 read | 2 reversible edit | 3 external effect (mail, share, invite, publish, cloud change) | 4 high risk.
Decisions
  ALLOW   - proceed (level 2 may carry a `copy_before_edit` precaution)
  CONFIRM - stop and ask. A confirmation is bound to the action `fingerprint`: changing recipients, files or text
            invalidates it. Level 4 needs a `special_approval` bound to the fingerprint, not a plain confirmation.
  BLOCK   - refused outright (untrusted macro/executable, security warnings disabled, internal data to a cloud model...).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lga_common.redact import REDACTED, Redactor

from lga_worker.office.filetype import EXEC_EXTENSIONS

LEVEL_NAMES = {1: "read", 2: "reversible_edit", 3: "external_effect", 4: "high_risk"}
MACRO_EXTENSIONS = {".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".xlsb", ".pptm", ".potm", ".ppsm", ".ppam", ".vba", ".bas", ".cls", ".frm"}
SECRET_KEY = re.compile(r"(?i)(pass(word|wd)?|pwd|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key|bearer|sas)")
RECIPIENT_KEYS = ("to", "cc", "bcc", "recipients", "attendees", "invitees", "share_with")
ATTACHMENT_KEYS = ("attachments", "attachment", "files")
PATH_KEYS = ("path", "paths", "file", "source", "target", "output", "workbook", "document", "presentation")
CLOUD_HINT = re.compile(r"(?i)(sharepoint\.com|onedrive|1drv\.ms|graph\.microsoft|teams\.microsoft|^https?://)")

_redactor = Redactor()


def redact_text(text: str) -> str:
    return _redactor(text)


def redact(obj: Any, *, max_str: int = 400, _depth: int = 0) -> Any:
    """Deep copy of `obj` safe for logs: secret-looking keys and token-looking strings replaced, long strings cut."""
    if _depth > 8:
        return "[too deep]"
    if isinstance(obj, dict):
        return {str(k): (REDACTED if SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list, bool)) and v not in (None, "") else
                         redact(v, max_str=max_str, _depth=_depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, max_str=max_str, _depth=_depth + 1) for v in obj[:200]]
    if isinstance(obj, str):
        text = _redactor(obj)
        return text if len(text) <= max_str else text[:max_str] + f"...[+{len(text) - max_str} chars]"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact_text(str(obj))[:max_str]


def fingerprint(operation: str, params: dict[str, Any]) -> str:
    """Stable id of one concrete action; a confirmation is only valid for the same fingerprint."""
    blob = json.dumps({"op": operation.lower(), "params": params}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------------------------------- classification
_TOKENS = lambda s: [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]  # noqa: E731

L4_PHRASES = ("run_macro", "execute_macro", "enable_macro", "enable_content", "vba_run", "run_vba", "run_script", "execute_script",
              "execute_attachment", "run_attachment", "bulk_delete", "delete_all", "empty_recycle", "change_permission", "set_permission",
              "grant_access", "revoke_access", "make_admin", "format_drive", "wipe", "purge", "mass_delete", "delete_mailbox")
L4_TOKENS = {"macro", "permissions", "permission", "acl", "admin", "purge", "wipe"}
SECURITY_OFF = re.compile(r"(?i)(disable|turn_?off|bypass|lower|skip|ignore)[ _-]*(security|warning|protected[ _-]?view|trust|antivirus|defender|macro[ _-]?security)")
L3_TOKENS = {"send", "forward", "reply", "share", "invite", "publish", "post", "upload", "sync", "broadcast", "notify", "schedule", "deploy", "refresh"}
L2_TOKENS = {"write", "edit", "create", "append", "replace", "update", "save", "rename", "move", "copy", "export", "convert", "draft", "delete",
             "remove", "format", "insert", "set", "add", "fill", "merge", "split", "compose", "clean", "sort", "fix", "apply"}
L1_TOKENS = {"read", "list", "search", "find", "get", "detect", "verify", "check", "analyze", "analyse", "summarize", "summarise", "capabilities",
             "route", "inspect", "status", "plan", "describe", "preview", "count", "extract", "audit", "query", "aggregate", "info", "tail",
             "learn", "record", "explain"}
DELETE_TOKENS = {"delete", "remove", "erase", "trash", "drop"}
KNOWN = {  # exact tool / operation ids whose level is fixed by contract
    "office.excel_read": 1, "office.word_read": 1, "office.outlook_list": 1, "office.outlook_read": 1, "office.excel_write": 2,
    "office.word_write": 2, "office.export_pdf": 2, "office.outlook_draft": 2, "office.outlook_send": 3, "office.detect": 1,
    "office.verify": 1, "office.capabilities": 1, "office.route": 1, "office.check_action": 1, "office.workflow_plan": 1,
    "office.workflow_status": 1, "office.workflow_step": 1, "office.audit_tail": 1, "office.learn": 1, "document.read": 1,
}


@dataclass
class GuardResult:
    operation: str
    level: int
    decision: str
    reasons: list[str] = field(default_factory=list)
    precautions: list[str] = field(default_factory=list)
    approval_kind: str = "none"  # none | user | special
    fingerprint: str = ""
    review: dict[str, Any] = field(default_factory=dict)
    basis: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "level": self.level, "level_name": LEVEL_NAMES[self.level], "decision": self.decision,
                "requires_confirmation": self.decision == "CONFIRM", "approval_kind": self.approval_kind, "reasons": self.reasons,
                "precautions": self.precautions, "fingerprint": self.fingerprint, "review": self.review, "classification_basis": self.basis,
                "params_redacted": self.params}


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[;,]", value) if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def inspect_attachment(path: str) -> dict[str, Any]:
    """Executable / macro status of one attachment or file: by extension AND by what the bytes are when the file exists."""
    p = Path(path)
    ext = p.suffix.lower()
    info: dict[str, Any] = {"name": p.name, "extension": ext, "executable": ext in EXEC_EXTENSIONS, "macro_enabled": ext in MACRO_EXTENSIONS,
                            "disguised": False, "checked_content": False}
    try:
        if p.is_file():
            from lga_worker.office import filetype

            with p.open("rb") as fh:
                head = fh.read(4)
            info["checked_content"] = True
            if head[:2] == b"MZ" and not info["executable"]:
                info.update(executable=True, disguised=True)
            real = filetype.detect(p)
            if real.get("macro_enabled") and not info["macro_enabled"]:
                info.update(macro_enabled=True, disguised=True)
            info["real_type"] = real.get("detected_type")
    except OSError:
        pass
    return info


def _collect_paths(params: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in PATH_KEYS + ATTACHMENT_KEYS:
        out.extend(_as_list(params.get(key)))
    return out


def classify(operation: str, params: dict[str, Any]) -> tuple[int, list[str]]:
    """(level, basis) from the operation id and the parameters; unknown operations are NOT treated as safe."""
    op = operation.lower().strip()
    basis: list[str] = []
    toks = set(_TOKENS(op))
    level = 0
    if op in KNOWN:
        level = KNOWN[op]
        basis.append(f"{op} has a fixed level {level} in the contract")
    flat = "_".join(_TOKENS(op))
    if any(p in flat for p in L4_PHRASES) or SECURITY_OFF.search(op):
        level = 4
        basis.append("operation id names a high-risk action (macro run / bulk delete / permission change / security off)")
    elif toks & L4_TOKENS and not toks & L1_TOKENS:
        level = max(level, 4)
        basis.append(f"high-risk token(s): {sorted(toks & L4_TOKENS)}")
    if level < 3 and toks & L3_TOKENS and not ("draft" in toks or "preview" in toks):
        level = 3
        basis.append(f"external-effect token(s): {sorted(toks & L3_TOKENS)}")
    if not level:
        if toks & L2_TOKENS:
            level = 2
            basis.append(f"edit token(s): {sorted(toks & L2_TOKENS)}")
        elif toks & L1_TOKENS:
            level = 1
            basis.append(f"read token(s): {sorted(toks & L1_TOKENS)}")
        else:
            level = 3
            basis.append("unknown operation: classified conservatively as level 3 (never assumed safe)")
    # parameter-driven escalation
    paths = _collect_paths(params)
    if toks & DELETE_TOKENS:
        bulk = (len(paths) > 1 or any(re.search(r"[*?]", p) for p in paths) or bool(params.get("recursive")) or bool(params.get("all"))
                or int(params.get("count", 1) or 1) > 1 or params.get("permanent") is True or params.get("skip_recycle_bin") is True)
        if bulk:
            level = 4
            basis.append("delete of several items / wildcard / recursive / permanent = bulk or irreversible")
    if level == 2 and (any(CLOUD_HINT.search(p) for p in paths) or str(params.get("location", "")).lower() in {"onedrive", "sharepoint", "cloud", "teams"}):
        level = 3
        basis.append("write to a cloud location (OneDrive/SharePoint/URL) may affect other people")
    recipients = [r for k in RECIPIENT_KEYS for r in _as_list(params.get(k))]
    if recipients and level < 3 and not toks & {"draft", "preview", "read", "list", "check"} and toks & {"calendar", "event", "meeting", "appointment"}:
        level = 3
        basis.append("calendar item with attendees notifies other people")
    return level, basis


def evaluate(operation: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None, *,
             now: float | None = None) -> GuardResult:
    params = dict(params or {})
    ctx = dict(context or {})
    level, basis = classify(operation, params)
    fp = fingerprint(operation, params)
    res = GuardResult(operation=operation, level=level, decision="ALLOW", fingerprint=fp, basis=basis, params=redact(params))
    paths = _collect_paths(params)
    recipients = [r for k in RECIPIENT_KEYS for r in _as_list(params.get(k))]
    attachments = [a for k in ATTACHMENT_KEYS for a in _as_list(params.get(k))]
    res.review = {"recipients": recipients, "attachments": attachments, "targets": [p for p in paths if p not in attachments]}
    toks = set(_TOKENS(operation))
    reads_only = level == 1
    # ---- hard blocks (never lifted by a confirmation)
    if SECURITY_OFF.search(operation) or params.get("disable_security_warnings") or params.get("enable_macros") is True:
        return _block(res, "disabling security warnings / enabling macros to finish a task is not allowed (spec 11)")
    if params.get("send_to_cloud_model") and not ctx.get("cloud_model_allowed"):
        return _block(res, "internal data may not be sent to a cloud model without explicit permission (context.cloud_model_allowed)")
    trusted = {str(Path(t)).lower() for t in _as_list(ctx.get("trusted_macro_sources"))}
    macro_run = bool({"macro", "vba"} & toks) and bool({"run", "execute", "enable", "open", "start"} & toks) or "run_macro" in "_".join(_TOKENS(operation))
    app_open = bool(re.search(r"(?i)(com|object_model|desktop|ui|application|app)", str(params.get("method", "")))) or toks & {"open", "launch"}
    flagged: list[dict[str, Any]] = []
    for p in paths:
        info = inspect_attachment(p)
        flagged.append(info)
        if info["executable"] and not reads_only:
            return _block(res, f"{info['name']} is an executable/script{' (disguised: content is a program)' if info['disguised'] else ''}; "
                               "attachments and files of this kind are never opened, run or sent automatically")
        if info["macro_enabled"]:
            is_trusted = str(Path(p)).lower() in trusted or str(Path(p).parent).lower() in trusted
            if macro_run or (app_open and not reads_only):
                if not is_trusted:
                    return _block(res, f"{info['name']} contains macros from an untrusted source; macros are not run automatically")
                res.level = max(res.level, 4)
                res.reasons.append(f"{info['name']} is macro-enabled (trusted source declared): level 4 needs special approval")
            elif toks & L3_TOKENS:
                res.level = max(res.level, 3)
                res.reasons.append(f"{info['name']} is macro-enabled: recipients may not be able to open it safely; confirm")
            else:
                res.precautions.append(f"{info['name']} is macro-enabled: use a library reader only (macros are not executed)")
    if macro_run and not paths:
        return _block(res, "running a macro without a named, trusted source file is not allowed")
    level = res.level
    # ---- decisions
    if level == 1:
        res.reasons.append("read-only: allowed when the file/mailbox access itself is permitted")
        return res
    if level == 2:
        existing = [p for p in paths if p and Path(p).is_file() and p not in attachments]
        if existing and not (params.get("target_is_copy") or ctx.get("target_is_copy") or params.get("backup_made")):
            res.precautions.append("copy_before_edit")
            res.reasons.append("editing an existing file: work on a copy first (guard.copy_before_edit) so the original can be restored")
        if params.get("overwrite_existing") is True and not (params.get("backup_made") or params.get("target_is_copy")):
            return _confirm(res, "user", "overwriting an existing file without a backup copy: ask whether to overwrite or create a new file")
        res.reasons.append("reversible edit: allowed")
        return res
    kind = "special" if level == 4 else "user"
    res.approval_kind = kind
    approval = ctx.get("special_approval") if level == 4 else ctx.get("confirmation")
    approved_fp = (approval or {}).get("fingerprint") if isinstance(approval, dict) else approval
    if approved_fp and approved_fp == fp:
        res.reasons.append(f"{'special approval' if level == 4 else 'user confirmation'} matches this exact action (fingerprint {fp})")
        res.approval_kind = "none"
        return res
    if approved_fp and approved_fp != fp:
        res.reasons.append("a confirmation exists but it was given for a different action (recipients, files or text changed); ask again")
    if level == 3:
        auth = ctx.get("authorization")
        why = _authorization_problem(auth, operation, recipients, now)
        if auth and not why:
            res.reasons.append("explicit standing authorization covers this action and these recipients")
            res.approval_kind = "none"
            return res
        if auth and why:
            res.reasons.append(f"standing authorization does not apply: {why}")
        res.precautions.append("show recipients, subject/text and attachments to the user and re-check them before executing")
        if toks & {"send", "forward", "reply"}:
            res.precautions.append("after sending, verify in Sent Items; never retry a send blindly (duplicates)")
        return _confirm(res, "user", "action has an effect outside this computer (mail, share, invite, publish or cloud change): confirmation required")
    return _confirm(res, "special", "high-risk action (bulk delete / permission change / macro): special approval required, or it stays blocked")


def _authorization_problem(auth: Any, operation: str, recipients: list[str], now: float | None) -> str | None:
    if not isinstance(auth, dict):
        return "no authorization given" if auth is None else "authorization is not a mapping"
    actions = [a.lower() for a in _as_list(auth.get("actions"))]
    if operation.lower() not in actions:
        return f"{operation} is not in the authorized actions"
    exp = auth.get("expires_at")
    if exp:
        try:
            when = datetime.fromisoformat(str(exp)).timestamp()
        except ValueError:
            return "expires_at is not an ISO timestamp"
        if when < (now if now is not None else time.time()):
            return "authorization has expired"
    allowed = [a.lower() for a in _as_list(auth.get("recipients"))]
    if recipients:
        if not allowed:
            return "authorization lists no recipients"
        for r in recipients:
            r_l = r.lower()
            if not any(r_l == a or (a.startswith("@") and r_l.endswith(a)) for a in allowed):
                return f"recipient {r} is not covered"
    return None


def _block(res: GuardResult, reason: str) -> GuardResult:
    res.decision = "BLOCK"
    res.approval_kind = "none"
    res.reasons.append(reason)
    return res


def _confirm(res: GuardResult, kind: str, reason: str) -> GuardResult:
    res.decision = "CONFIRM"
    res.approval_kind = kind
    res.reasons.append(reason)
    return res


# ----------------------------------------------------------------------------------------- copy before edit
def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def copy_before_edit(path: str | Path, dest_dir: str | Path | None = None, label: str = "lga-copy") -> dict[str, Any]:
    """Byte-exact copy of `path` next to it (or in dest_dir), verified by size + SHA-256. The original is never modified."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"cannot copy {src}: not a file")
    folder = Path(dest_dir) if dest_dir else src.parent
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{src.stem}.{label}-{stamp}{src.suffix}"
    n = 1
    while dest.exists():
        n += 1
        dest = folder / f"{src.stem}.{label}-{stamp}-{n}{src.suffix}"
    shutil.copy2(src, dest)
    before, after = sha256_file(src), sha256_file(dest)
    ok = before == after and src.stat().st_size == dest.stat().st_size
    if not ok:
        dest.unlink(missing_ok=True)
        raise OSError(f"copy of {src} failed verification (hash differs); the copy was removed")
    return {"original": str(src), "copy": str(dest), "sha256": after, "size_bytes": dest.stat().st_size, "verified": True}
