"""Office spec modules (docs/specs/SPEC_OFFICE.txt): offline unit tests, no Office, no network."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from lga_worker.office import filetype, guard, router, verify, workflow
from lga_worker.office.audit import AuditLog, LearningRefused, LearningStore
from lga_worker.office.graph import GraphAdapter, GraphNotConfigured
from lga_worker.office.registry import NotSupported, OfficeCapabilityRegistry, operation_shape_problems, validate_record_shape


# ----------------------------------------------------------------------------- builders
def make_xlsx(path: Path, cells: dict[str, list[str]]) -> Path:
    """cells: sheet -> list of raw <c> xml strings grouped by row via r attribute."""
    sheets = list(cells)
    wb = "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1))
    rels = "".join(f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{wb}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for i, n in enumerate(sheets, 1):
            rows: dict[int, list[str]] = {}
            for c in cells[n]:
                r = int("".join(ch for ch in c.split('r="')[1].split('"')[0] if ch.isdigit()))
                rows.setdefault(r, []).append(c)
            body = "".join(f'<row r="{r}">{"".join(cs)}</row>' for r, cs in sorted(rows.items()))
            z.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    return path


def num(ref, v): return f'<c r="{ref}"><v>{v}</v></c>'
def txt(ref, s): return f'<c r="{ref}" t="inlineStr"><is><t>{s}</t></is></c>'
def fml(ref, f, v=None): return f'<c r="{ref}"><f>{f}</f>' + (f"<v>{v}</v>" if v is not None else "") + "</c>"
def err(ref, e): return f'<c r="{ref}" t="e"><v>{e}</v></c>'


def make_pptx(path: Path, slides: list[str], size=(9144000, 6858000)) -> Path:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(1, len(slides) + 1))
    rels = "".join(f'<Relationship Id="rId{i}" Type="x" Target="slides/slide{i}.xml"/>' for i in range(1, len(slides) + 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
        z.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<p:sldIdLst>{ids}</p:sldIdLst><p:sldSz cx="{size[0]}" cy="{size[1]}"/></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for i, body in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                       f'<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>')
    return path


def box(name, x, y, w, h, text, sz=1800, title=False, extra_body=""):
    ph = '<p:nvPr><p:ph type="title"/></p:nvPr>' if title else "<p:nvPr/>"
    pt = 12700
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="{name}"/><p:cNvSpPr/>{ph}</p:nvSpPr><p:spPr><a:xfrm><a:off x="{x * pt}" y="{y * pt}"/><a:ext cx="{w * pt}" cy="{h * pt}"/></a:xfrm></p:spPr>'
            f'<p:txBody><a:bodyPr>{extra_body}</a:bodyPr><a:p><a:r><a:rPr sz="{sz}"/><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')


# ----------------------------------------------------------------------------- filetype
def test_detect_by_content_not_extension(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document(); d.add_paragraph("hello"); real = tmp_path / "report.xlsx"; d.save(real)
    info = filetype.detect(real)
    assert info["detected_type"] == "docx" and info["extension_matches"] is False and info["status"] == "ok"
    assert any("does not match" in w for w in info["warnings"])
    assert "office.word_read" in info["handler"]["read"]


def test_detect_pdf_csv_json_eml_ics_and_garbage(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    assert filetype.detect(tmp_path / "a.pdf")["detected_type"] == "pdf"
    (tmp_path / "b.dat").write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
    assert filetype.detect(tmp_path / "b.dat")["detected_type"] == "csv"
    (tmp_path / "c.txt").write_text('{"k": [1,2]}', encoding="utf-8")
    assert filetype.detect(tmp_path / "c.txt")["detected_type"] == "json"
    (tmp_path / "d.x").write_text("From: a@b.com\nTo: c@d.com\nSubject: hi\nDate: x\n\nbody", encoding="utf-8")
    assert filetype.detect(tmp_path / "d.x")["detected_type"] == "eml"
    (tmp_path / "e.x").write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n", encoding="utf-8")
    assert filetype.detect(tmp_path / "e.x")["detected_type"] == "ics"
    (tmp_path / "f.bin").write_bytes(bytes(range(256)) * 4)
    assert filetype.detect(tmp_path / "f.bin")["detected_type"] == "binary"


def test_detect_problem_states(tmp_path):
    assert filetype.detect(tmp_path / "nope.docx")["status"] == "missing"
    (tmp_path / "z.docx").write_bytes(b"")
    assert filetype.detect(tmp_path / "z.docx")["status"] == "empty"
    (tmp_path / "bad.docx").write_bytes(b"PK\x03\x04garbage-not-a-zip" * 5)
    bad = filetype.detect(tmp_path / "bad.docx")
    assert bad["status"] == "corrupt" and bad["corrupt_reason"]
    (tmp_path / "fake.docx").write_text("just text", encoding="utf-8")
    assert filetype.detect(tmp_path / "fake.docx")["status"] == "corrupt"
    (tmp_path / "enc.pdf").write_bytes(b"%PDF-1.6\n/Encrypt 5 0 R\n%%EOF\n")
    assert filetype.detect(tmp_path / "enc.pdf")["status"] == "encrypted"
    (tmp_path / "trunc.pdf").write_bytes(b"%PDF-1.4\n1 0 obj")
    assert filetype.detect(tmp_path / "trunc.pdf")["status"] == "corrupt"
    (tmp_path / "big.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert filetype.detect(tmp_path / "big.pdf", size_limit=5)["oversized"] is True
    assert filetype.detect(tmp_path)["status"] == "directory"


def test_detect_access_ole_and_macro(tmp_path):
    (tmp_path / "x.accdb").write_bytes(b"\x00\x01\x00\x00Standard ACE DB\x00" + b"\0" * 100)
    assert filetype.detect(tmp_path / "x.accdb")["detected_type"] == "accdb"
    (tmp_path / "t.dat").write_bytes(filetype.OLE_MAGIC + b"\0" * 8)  # header only: damaged OLE
    assert filetype.detect(tmp_path / "t.dat")["status"] == "corrupt"
    m = tmp_path / "m.xlsm"
    with zipfile.ZipFile(m, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" '
                   'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/></Types>')
        z.writestr("xl/workbook.xml", "<workbook/>")
    info = filetype.detect(m)
    assert info["detected_type"] == "xlsm" and info["macro_enabled"] is True
    z2 = tmp_path / "arch.zip"
    with zipfile.ZipFile(z2, "w") as z:
        z.writestr("a.docx", "x"); z.writestr("run.exe", "MZ")
    ai = filetype.detect(z2)
    assert ai["detected_type"] == "zip" and ai["archive_office_members"] == ["a.docx"] and any("executable" in w for w in ai["warnings"])


def test_ole_directory_classification():
    assert filetype.classify_ole(["Root Entry", "WordDocument", "1Table"])[0] == "doc"
    assert filetype.classify_ole(["Workbook"])[0] == "xls"
    assert filetype.classify_ole(["EncryptionInfo", "EncryptedPackage"])[0] == "ooxml_encrypted"
    assert filetype.classify_ole(["__substg1.0_0037001F"])[0] == "msg"


# ----------------------------------------------------------------------------- registry
class FakeProbe:
    def __init__(self, progids=(), modules=(), tools=None):
        self.progids, self.modules = set(progids), set(modules)
        self.tools = tools if tools is not None else {n: {"_file": "office.json", "idempotent": True, "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
                                                           "timeout_ms": 1000, "side_effects": "none"} for n in
                                                      ["office.word_read", "office.word_write", "office.excel_read", "office.excel_write", "office.export_pdf", "document.read",
                                                       "data.aggregate", "office.verify", "office.outlook_list", "office.outlook_read", "office.outlook_draft", "office.outlook_send"]}

    def com_progid(self, p): return {"progid": p, "clsid": "{x}", "server": "C:/x.exe", "server_exists": True, "version": "16.0"} if p in self.progids else None
    def module(self, n): return {"module": n, "version": "1"} if n in self.modules else None
    def tool_contract(self, t): return self.tools.get(t)


def test_registry_schema_and_no_claim_without_evidence():
    snap = OfficeCapabilityRegistry(FakeProbe()).snapshot(detail="full")
    assert snap["graph"]["available"] is False and "not configured/no credentials" in snap["graph"]["reason"]
    for a in snap["applications"]:
        assert validate_record_shape(a) == []
        assert a["last_health_check"]
        for op in a["supported_operations"] + a["unsupported_operations"]:
            assert operation_shape_problems(op) == []
    word = next(a for a in snap["applications"] if a["application_id"] == "word")
    ops = {o["operation_id"]: o for o in word["supported_operations"]}
    assert "word.read_text" in ops and ops["word.read_text"]["method"] == "file_library"  # no Word COM registered here
    assert "word.write_text" not in ops
    assert any(o["operation_id"] == "word.write_text" and "not registered" in o["reasons"][0] for o in word["unsupported_operations"])
    assert any(o["operation_id"] == "word.compare_versions" and "not implemented" in o["reasons"][0] for o in word["unsupported_operations"])
    assert word["installed"] is False


def test_registry_with_com_and_installed_without_adapter():
    reg = OfficeCapabilityRegistry(FakeProbe(progids={"Word.Application", "Access.Application"}))
    ok, why = reg.can("word", "word.write_text")
    assert ok and "Word.Application" in why
    acc = reg.get("access")
    assert acc["installed"] is True and acc["available"] is False and acc["supported_operations"] == []
    assert any("NOT claimed" in x for x in acc["known_limitations"])
    assert reg.can("word", "word.nonsense")[0] is False and reg.can("nope", "x")[0] is False
    with pytest.raises(NotSupported):
        reg.require("excel", "excel.write_range")
    with pytest.raises(KeyError):
        reg.snapshot("nope")


def test_registry_missing_tool_contract_is_unsupported():
    reg = OfficeCapabilityRegistry(FakeProbe(progids={"Word.Application"}, tools={}))
    ok, why = reg.can("word", "word.write_text")
    assert not ok and "not declared" in why


def test_real_system_probe_runs():
    snap = OfficeCapabilityRegistry().snapshot()
    assert {a["application_id"] for a in snap["applications"]} >= {"word", "excel", "outlook", "power_bi"}
    cloud = next(a for a in snap["applications"] if a["application_id"] == "teams")
    assert cloud["available"] is False and cloud["authenticated"] is False


def test_graph_stub_never_calls_network():
    g = GraphAdapter()
    assert g.status()["available"] is False and g.status()["network_calls_made"] == 0
    with pytest.raises(GraphNotConfigured):
        g.send_mail("a@b.com")


# ----------------------------------------------------------------------------- guard
@pytest.mark.parametrize("op,level,decision", [
    ("office.excel_read", 1, "ALLOW"), ("word.read_text", 1, "ALLOW"), ("office.excel_write", 2, "ALLOW"), ("outlook.create_draft", 2, "ALLOW"),
    ("office.outlook_send", 3, "CONFIRM"), ("teams.send_message", 3, "CONFIRM"), ("share_file", 3, "CONFIRM"), ("frobnicate", 3, "CONFIRM"),
    ("run_macro", 4, "BLOCK"), ("change_permissions", 4, "CONFIRM"),
])
def test_guard_levels(op, level, decision):
    r = guard.evaluate(op, {"path": "C:/x/new.docx"} if level == 4 and op != "run_macro" else {})
    assert (r.level, r.decision) == (level, decision), r.reasons


def test_guard_bulk_delete_and_single_delete():
    assert guard.evaluate("delete_file", {"path": "C:/a/x.txt"}).level == 2
    r = guard.evaluate("delete_files", {"paths": ["C:/a/1.txt", "C:/a/2.txt"]})
    assert r.level == 4 and r.decision == "CONFIRM" and r.approval_kind == "special"
    assert guard.evaluate("delete_file", {"path": "C:/a/*.txt"}).level == 4


def test_guard_confirmation_bound_to_fingerprint():
    params = {"to": "boss@x.com", "subject": "s", "attachments": ["C:/r.docx"]}
    r = guard.evaluate("office.outlook_send", params)
    assert r.decision == "CONFIRM" and r.review["recipients"] == ["boss@x.com"]
    ok = guard.evaluate("office.outlook_send", params, {"confirmation": r.fingerprint})
    assert ok.decision == "ALLOW"
    changed = guard.evaluate("office.outlook_send", {**params, "to": "other@x.com"}, {"confirmation": r.fingerprint})
    assert changed.decision == "CONFIRM" and any("different action" in x for x in changed.reasons)


def test_guard_standing_authorization_scope_and_expiry():
    p = {"to": "a@corp.com"}
    auth = {"actions": ["office.outlook_send"], "recipients": ["@corp.com"], "expires_at": "2999-01-01T00:00:00"}
    assert guard.evaluate("office.outlook_send", p, {"authorization": auth}).decision == "ALLOW"
    assert guard.evaluate("office.outlook_send", {"to": "a@evil.com"}, {"authorization": auth}).decision == "CONFIRM"
    assert guard.evaluate("office.outlook_send", p, {"authorization": {**auth, "expires_at": "2000-01-01T00:00:00"}}).decision == "CONFIRM"


def test_guard_blocks_executables_macros_security_and_cloud_model(tmp_path):
    assert guard.evaluate("office.outlook_send", {"to": "a@b.c", "attachments": ["C:/t/setup.exe"]}).decision == "BLOCK"
    disguised = tmp_path / "invoice.pdf"; disguised.write_bytes(b"MZ\x90\x00" + b"\0" * 50)
    assert guard.evaluate("outlook.create_draft", {"attachments": [str(disguised)]}).decision == "BLOCK"
    assert guard.evaluate("excel.open", {"path": "C:/t/m.xlsm", "method": "com"}).decision == "BLOCK"
    trusted = guard.evaluate("excel.open", {"path": "C:/t/m.xlsm", "method": "com"}, {"trusted_macro_sources": ["C:/t"]})
    assert trusted.level == 4 and trusted.decision == "CONFIRM"
    assert guard.evaluate("excel.read_range", {"path": "C:/t/m.xlsm"}).decision == "ALLOW"
    assert guard.evaluate("disable_macro_security", {}).decision == "BLOCK"
    assert guard.evaluate("word.read_text", {"path": "C:/a.docx", "send_to_cloud_model": True}).decision == "BLOCK"
    assert guard.evaluate("word.read_text", {"path": "C:/a.docx", "send_to_cloud_model": True}, {"cloud_model_allowed": True}).decision == "ALLOW"


def test_guard_cloud_write_overwrite_and_copy_precaution(tmp_path):
    assert guard.evaluate("word.write_text", {"path": "https://x.sharepoint.com/a.docx"}).level == 3
    f = tmp_path / "a.docx"; f.write_bytes(b"x")
    r = guard.evaluate("word.write_text", {"path": str(f)})
    assert r.decision == "ALLOW" and "copy_before_edit" in r.precautions
    assert guard.evaluate("word.write_text", {"path": str(f), "overwrite_existing": True}).decision == "CONFIRM"
    assert "copy_before_edit" not in guard.evaluate("word.write_text", {"path": str(f), "target_is_copy": True}).precautions


def test_copy_before_edit(tmp_path):
    src = tmp_path / "a.xlsx"; src.write_bytes(b"data" * 100)
    res = guard.copy_before_edit(src)
    assert Path(res["copy"]).read_bytes() == src.read_bytes() and res["verified"] and res["copy"] != str(src)
    with pytest.raises(FileNotFoundError):
        guard.copy_before_edit(tmp_path / "missing.xlsx")


def test_redaction():
    out = guard.redact({"password": "hunter2-long", "token": "abc", "nested": {"Authorization": "Bearer abcdefghijklmnop"}, "note": "api_key=sk-ABCDEFGHIJKLMNOPQRSTUV", "n": 3})
    s = json.dumps(out)
    assert "hunter2" not in s and "abcdefghijklmnop" not in s and "sk-ABCDEF" not in s and out["n"] == 3


# ----------------------------------------------------------------------------- audit + learning
def test_audit_log_redacts_and_chains(tmp_path):
    log = AuditLog(tmp_path / "office")
    log.record("office.outlook_send", level=3, decision="CONFIRM", params={"to": "a@b.c", "password": "pw-secret-1"}, result={"token": "tok-secret-1"},
               provenance={"tool": "t", "task_id": "T1"})
    f = tmp_path / "office"; raw = "".join(p.read_text(encoding="utf-8") for p in f.glob("audit-*.jsonl"))
    assert "pw-secret-1" not in raw and "tok-secret-1" not in raw and "T1" in raw
    log.record("office.verify", status="PASS")
    assert log.verify_chain() == {"ok": True, "checked": 2}
    assert len(log.tail(5)) == 2 and AuditLog(f).record("x")["prev"] != "0" * 64  # chain continues across instances
    file = next(f.glob("audit-*.jsonl")); lines = file.read_text(encoding="utf-8").splitlines()
    file.write_text("\n".join([lines[0].replace("CONFIRM", "ALLOW")] + lines[1:]) + "\n", encoding="utf-8")
    bad = log.verify_chain()
    assert bad["ok"] is False and bad["line"] == 1


def test_audit_hashes_target(tmp_path):
    t = tmp_path / "f.docx"; t.write_bytes(b"abc")
    e = AuditLog(tmp_path / "a").record("x", target=str(t), hash_target=True)
    assert e["target_sha256"] and e["target_size"] == 3


def good_record(**kw):
    base = dict(task_type="excel.write", application="excel", environment="desktop_com", observed_failure="formula became value", root_cause="wrote constant",
                corrective_action="write formula string", verification_result="verified", confidence=0.8,
                provenance={"source": "verification_engine", "evidence": "audit:abc123"}, regression_test_reference="tests/test_office_spec_unit.py::test_x")
    base.update(kw)
    return base


def test_learning_store_rules(tmp_path):
    store = LearningStore(tmp_path / "l.jsonl")
    first = store.store(good_record())
    assert first["occurrences"] == 1 and first["generalizable"] is False and first["applied"] is False
    second = store.store(good_record())
    assert second["occurrences"] == 2 and second["generalizable"] is True
    for bad in (dict(verification_result="unverified"), dict(verification_result="failed"), dict(provenance={"source": "assistant_answer", "evidence": "x"}),
                dict(provenance={"source": "tool_result"}), dict(confidence=1.5), dict(root_cause=" "), dict(provenance={"source": "gossip", "evidence": "x"})):
        with pytest.raises(LearningRefused):
            store.store(good_record(**bad))
    with pytest.raises(LearningRefused):
        store.store({"task_type": "x"})
    with pytest.raises(LearningRefused):
        store.store({**good_record(), "extra": 1})
    pend = store.store(good_record(root_cause="other", regression_test_reference="PENDING: add test"))
    assert pend["status"] == "pending_regression"
    sec = store.store(good_record(root_cause="leak", observed_failure="password=hunter2-secret in doc"))
    assert "hunter2" not in json.dumps(sec)


def test_failed_strategy_memory(tmp_path):
    store = LearningStore(tmp_path / "l.jsonl")
    with pytest.raises(LearningRefused):
        store.store_failed_strategy("t", "excel", "retry same", {"source": "model"}, "r")
    store.store_failed_strategy("t", "excel", "Retry same", {"source": "test_run", "evidence": "run 5"}, "still locked")
    assert store.strategy_failed_before("t", "excel", "retry  SAME")


# ----------------------------------------------------------------------------- workflow
def wf3():
    return workflow.Workflow("w1", "t", [
        workflow.Step("a", "read", "excel.read_range", "excel"),
        workflow.Step("b", "docx", "word.write_text", "word", ["a"]),
        workflow.Step("c", "draft", "outlook.create_draft", "outlook", ["b"]),
        workflow.Step("d", "send", "outlook.send_mail", "outlook", ["c"], requires_confirmation=True),
    ])


def test_graph_validation():
    with pytest.raises(workflow.WorkflowError, match="cycle"):
        workflow.Workflow("w", "t", [workflow.Step("a", "", "x", depends_on=["b"]), workflow.Step("b", "", "x", depends_on=["a"])])
    with pytest.raises(workflow.WorkflowError, match="unknown step"):
        workflow.Workflow("w", "t", [workflow.Step("a", "", "x", depends_on=["zz"])])
    with pytest.raises(workflow.WorkflowError, match="duplicate"):
        workflow.Workflow("w", "t", [workflow.Step("a", "", "x"), workflow.Step("a", "", "x")])
    with pytest.raises(workflow.WorkflowError):
        workflow.Workflow("../evil", "t", [])


def test_dependencies_and_success_needs_evidence():
    wf = wf3()
    assert [s.id for s in wf.ready()] == ["a"]
    with pytest.raises(workflow.WorkflowError, match="not ready"):
        wf.start("b")
    wf.start("a")
    wf.succeed("a", None)  # no evidence -> UNKNOWN
    assert wf.steps["a"].status == "UNKNOWN" and wf.ready() == []
    assert wf.reconcile("a", {"done": True, "evidence": [{"detail": "range read, 12 rows"}]})["result"] == "SUCCEEDED"
    assert [s.id for s in wf.ready()] == ["b"]


def test_confirmation_gate_and_user_decision():
    wf = wf3()
    for sid in "abc":
        wf.start(sid); wf.succeed(sid, "ok")
    assert wf.ready() == [] and [s.id for s in wf.needs_confirmation()] == ["d"]
    with pytest.raises(workflow.WorkflowError, match="confirmation"):
        wf.start("d")
    wf.wait_for_user("d", "send to boss@x.com?")
    assert wf.overall() == "WAITING_FOR_USER"
    wf.user_decision("d", True)
    wf.start("d"); wf.succeed("d", {"detail": "found in Sent Items"})
    assert wf.overall() == "SUCCEEDED"


def test_decline_cancels_dependents():
    wf = wf3()
    wf.wait_for_user("a", "ok?")
    wf.user_decision("a", False)
    assert {s.status for s in wf.steps.values()} == {"CANCELLED"}


def test_retry_policy_never_blind_for_non_idempotent():
    wf = wf3()
    for sid in "ab":
        wf.start(sid); wf.succeed(sid, "ok")
    wf.start("c")
    d = wf.fail("c", "busy", "busy")  # create_draft is not idempotent
    assert d["action"] == "check_state_first"
    with pytest.raises(workflow.WorkflowError, match="refused"):
        wf.retry("c")
    wf.reconcile("c", {"done": False, "evidence": "Drafts folder has no such draft"})
    assert wf.steps["c"].status == "PENDING" and wf.retry_decision("c")["action"] == "none"
    wf.start("c")
    assert wf.fail("c", "boom", "invalid_argument")["action"] == "no_retry"


def test_idempotent_transient_retry_and_timeout_is_unknown():
    wf = wf3()
    wf.start("a")
    assert wf.fail("a", "locked up", "busy")["action"] == "retry"
    wf.retry("a")
    wf.start("a")
    wf.fail("a", "slow", "timeout")
    assert wf.steps["a"].status == "UNKNOWN"
    assert wf.retry_decision("a")["action"] == "check_state_first"
    wf.steps["a"].attempts = 3
    assert wf.retry_decision("a")["action"] == "no_retry"


def test_partial_and_overall_status():
    wf = wf3()
    wf.start("a"); wf.succeed("a", "ok"); wf.start("b")
    wf.partial("b", ["page1"], ["page2"], "1 of 2")
    rep = wf.report()
    assert rep["overall"] == "PARTIAL" and rep["partial"][0]["remaining"] == ["page2"] and rep["completed_steps"] == ["a"]
    assert rep["remaining_steps"] == ["b", "c", "d"] and "c" in rep["blocked"]
    with pytest.raises(workflow.WorkflowError):
        wf.start("b")  # PARTIAL is not PENDING
    with pytest.raises(workflow.WorkflowError):
        wf3().partial("a", [], [])


def test_checkpoint_resume_and_interrupted_running(tmp_path):
    wf = wf3(); wf.save(tmp_path)
    wf.start("a"); wf.succeed("a", "ok"); wf.start("b")  # crash while b RUNNING
    again = workflow.Workflow.load(tmp_path / "w1.json")
    assert again.steps["a"].status == "SUCCEEDED" and again.steps["b"].status == "UNKNOWN"
    plan = again.resume_plan()
    assert plan["skip_completed"] == ["a"] and plan["reconcile_first"] == ["b"] and plan["run_next"] == []
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(workflow.WorkflowError, match="damaged"):
        workflow.Workflow.load(tmp_path / "bad.json")
    with pytest.raises(workflow.WorkflowError, match="no checkpoint"):
        workflow.Workflow.open(tmp_path, "ghost")
    assert workflow.list_workflows(tmp_path)[0]["workflow_id"] in ("w1", "bad")


def test_reconcile_local_file_check(tmp_path):
    out = tmp_path / "r.pdf"
    wf = workflow.Workflow("w", "t", [workflow.Step("p", "pdf", "word.export_pdf", "word", verify={"kind": "file_valid", "path": str(out), "type": "pdf"})])
    wf.start("p"); wf.fail("p", "hung", "timeout")
    assert wf.reconcile("p")["result"] == "PENDING"  # file absent: verified not done
    wf.start("p"); wf.fail("p", "hung", "timeout")
    out.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert wf.reconcile("p")["result"] == "SUCCEEDED"


def test_reconcile_without_check_stays_unknown():
    wf = wf3()
    wf.start("a"); wf.fail("a", "x", "timeout")
    assert wf.reconcile("a")["result"] == "UNKNOWN"
    wf2 = workflow.Workflow("w2", "t", [workflow.Step("s", "send", "outlook.send_mail", "outlook", verify={"kind": "sent_items"})])
    wf2.start("s"); wf2.fail("s", "x", "timeout")
    assert wf2.reconcile("s")["result"] == "UNKNOWN"


def test_cancel_workflow_keeps_running_step():
    wf = wf3(); wf.start("a")
    res = wf.cancel()
    assert res["still_running"] == ["a"] and wf.steps["b"].status == "CANCELLED" and wf.ready() == []


def test_offline_plan():
    wf = wf3()
    off = wf.offline_plan(False)
    assert [x["step"] for x in off["can_continue"]] == ["a", "b", "c"]
    assert [x["step"] for x in off["must_wait"]] == ["d"] and "NOT simulated" in off["must_wait"][0]["reason"]
    on = wf.offline_plan(True)
    assert on["must_wait"] == []
    wf2 = workflow.Workflow("w", "t", [workflow.Step("up", "", "onedrive_sharepoint.upload", "onedrive_sharepoint"), workflow.Step("n", "", "teams.notify", "teams", ["up"]),
                                       workflow.Step("loc", "", "excel.aggregate", "excel")])
    o = wf2.offline_plan(False)
    assert {x["step"] for x in o["must_wait"]} == {"up", "n"} and [x["step"] for x in o["can_continue"]] == ["loc"]
    assert wf2.steps["up"].idempotent is False


def test_templates_and_support_annotation():
    reg = OfficeCapabilityRegistry(FakeProbe(progids={"Word.Application", "Excel.Application"}))
    for t in workflow.TEMPLATES:
        steps = workflow.template_steps(t, {"with_pptx": True, "with_dashboard": True})
        wf = workflow.Workflow("w", t, steps)
        workflow.annotate_support(wf, reg)
        assert wf.order()
    wf = workflow.Workflow("w", "t", workflow.template_steps("weekly_report", {"with_pptx": True}))
    workflow.annotate_support(wf, reg)
    rep = wf.report()
    assert "pptx" in rep["unsupported"] and "draft" in rep["unsupported"] and wf.steps["send"].requires_confirmation
    assert "docx" not in rep["unsupported"]
    assert "pptx" in wf.blocked()["pptx"] or "no adapter" in wf.blocked()["pptx"]
    with pytest.raises(workflow.WorkflowError):
        workflow.template_steps("nope")


# ----------------------------------------------------------------------------- verify: docx
def test_verify_docx_good_and_bad(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Title", 1); d.add_paragraph("Body text number one is long enough here."); d.add_heading("Sec", 2)
    t = d.add_table(rows=2, cols=3)
    for i, c in enumerate(t.rows[0].cells): c.text = f"h{i}"
    p = tmp_path / "ok.docx"; d.save(p)
    r = verify.verify_docx(p, expected_text=["Body text number one"], expected_tables=1, expect_headings=True, expected_headings=["Title", {"text": "Sec", "level": 2}])
    assert r["verdict"] == "PASS", [c for c in r["checks"] if c["status"] != "PASS"]
    assert any("page layout" in x for x in r["not_verified"])
    r2 = verify.verify_docx(p, expected_text=["absent phrase"], forbidden_text=["Title"], expected_tables=2)
    names = {c["name"]: c["status"] for c in r2["checks"]}
    assert r2["verdict"] == "FAIL" and names["expected_text_present"] == "FAIL" and names["forbidden_text_absent"] == "FAIL" and names["tables_intact"] == "FAIL"


def test_verify_docx_heading_jump_duplicates_placeholder_and_before(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("A", 1); d.add_heading("C", 3); d.add_paragraph("same same same"); d.add_paragraph("same same same"); d.add_paragraph("Dear {{name}}")
    p = tmp_path / "bad.docx"; d.save(p)
    st = {c["name"]: c["status"] for c in verify.verify_docx(p)["checks"]}
    assert st["heading_hierarchy"] == "FAIL" and st["no_duplicate_paragraphs"] == "FAIL" and st["no_leftover_placeholders"] == "WARN"
    d2 = docx.Document(); d2.add_paragraph("Keep this original paragraph"); d2.add_paragraph("Second original paragraph here")
    b = tmp_path / "before.docx"; d2.save(b)
    d3 = docx.Document(); d3.add_paragraph("Keep this original paragraph"); a = tmp_path / "after.docx"; d3.save(a)
    res = verify.verify_docx(a, before=str(b))
    assert {c["name"]: c["status"] for c in res["checks"]}["no_unintended_loss"] == "FAIL"
    assert {c["name"]: c["status"] for c in verify.verify_docx(a, before=str(b), allowed_changes=["Second original"])["checks"]}["no_unintended_loss"] == "PASS"


def test_verify_docx_empty_corrupt_and_missing(tmp_path):
    docx = pytest.importorskip("docx")
    e = tmp_path / "e.docx"; docx.Document().save(e)
    assert {c["name"]: c["status"] for c in verify.verify_docx(e)["checks"]}["content_present"] == "FAIL"
    (tmp_path / "x.docx").write_bytes(b"not a zip")
    assert verify.verify_docx(tmp_path / "x.docx")["verdict"] == "FAIL"
    assert verify.verify_file(tmp_path / "nope.docx")["verdict"] == "FAIL"


# ----------------------------------------------------------------------------- verify: xlsx
def test_verify_xlsx_errors_formulas_and_kinds(tmp_path):
    p = make_xlsx(tmp_path / "a.xlsx", {"S1": [txt("A1", "k"), txt("B1", "v"), txt("A2", "x"), num("B2", 0), txt("A3", "y"), fml("B3", "SUM(B2:B2)", 0),
                                              '<c r="A4" t="inlineStr"><is><t></t></is></c>', '<c r="B4" s="1"/>']})
    r = verify.verify_xlsx(p, expected_cells={"S1!B2": 0, "S1!B3": "=sum(b2:b2)", "S1!A4": "", "S1!B4": None, "S1!A2": "x"}, expected_dims={"S1": {"rows": 4, "cols": 2}},
                           unique_key={"sheet": "S1", "column": "A", "allow_blank": True}, totals=[{"sheet": "S1", "column": "B", "sum": 0}])
    assert r["verdict"] == "PASS", [c for c in r["checks"] if c["status"] != "PASS"]
    assert r["recalculated"] is False
    bad = verify.verify_xlsx(p, expected_cells={"S1!B4": 0, "S1!A4": None, "S1!B2": None, "S1!B3": 5})
    assert {c["name"]: c["status"] for c in bad["checks"]}["expected_cells"] == "FAIL"
    detail = next(c for c in bad["checks"] if c["name"] == "expected_cells")["detail"]
    assert "B4" in detail and "A4" in detail and "B2" in detail


def test_verify_xlsx_error_values_ref_uncached_and_duplicates(tmp_path):
    p = make_xlsx(tmp_path / "e.xlsx", {"S": [txt("A1", "id"), txt("A2", "1"), txt("A3", "1"), err("B1", "#DIV/0!"), fml("B2", "A1/0", "#VALUE!").replace("<c r", '<c t="e" r'),
                                               fml("B3", "#REF!+1", 1), fml("C1", "A2+1")]})
    r = verify.verify_xlsx(p, unique_key={"sheet": "S", "column": "A"})
    st = {c["name"]: c for c in r["checks"]}
    assert r["verdict"] == "FAIL"
    assert st["no_error_values"]["status"] == "FAIL" and "#DIV/0!" in st["no_error_values"]["detail"]
    assert st["no_broken_references"]["status"] == "FAIL"
    assert st["formula_results_cached"]["status"] == "WARN" and any("formulas without cached" in x for x in r["not_verified"])
    assert st["unique_key"]["status"] == "FAIL"


def test_verify_xlsx_before_after_detects_overwritten_formula_and_loss(tmp_path):
    before = make_xlsx(tmp_path / "b.xlsx", {"S": [num("A1", 1), num("A2", 2), fml("A3", "SUM(A1:A2)", 3), num("B1", 7), num("A4", 9)]})
    after = make_xlsx(tmp_path / "a.xlsx", {"S": [num("A1", 1), num("A2", 2), num("A3", 3), num("B1", 8)]})
    r = verify.verify_xlsx(after, before=str(before), edit_scope=["S!B1:B1"], totals_unchanged=[{"sheet": "S", "column": "A"}])
    st = {c["name"]: c for c in r["checks"]}
    assert st["formulas_not_overwritten"]["status"] == "FAIL" and "A3" in st["formulas_not_overwritten"]["detail"]
    assert st["row_col_counts"]["status"] == "FAIL"
    assert st["no_out_of_scope_changes"]["status"] == "FAIL"
    assert st["total_unchanged_S_A"]["status"] == "FAIL"
    good = make_xlsx(tmp_path / "g.xlsx", {"S": [num("A1", 1), num("A2", 2), fml("A3", "SUM(A1:A2)", 3), num("B1", 8), num("A4", 9)]})
    rg = verify.verify_xlsx(good, before=str(before), edit_scope=["S!B1"])
    assert rg["verdict"] in ("PASS", "WARN") and not [c for c in rg["checks"] if c["status"] == "FAIL"]


def test_verify_xlsx_corrupt_and_missing_sheet(tmp_path):
    (tmp_path / "c.xlsx").write_bytes(b"PK\x03\x04junk")
    assert verify.verify_xlsx(tmp_path / "c.xlsx")["verdict"] == "FAIL"
    p = make_xlsx(tmp_path / "s.xlsx", {"One": [num("A1", 1)]})
    assert {c["name"]: c["status"] for c in verify.verify_xlsx(p, expected_sheets=["Two"])["checks"]}["expected_sheets"] == "FAIL"
    assert {c["name"]: c["status"] for c in verify.verify_xlsx(p, expected_cells={"Nope!A1": 1, "bad": 1})["checks"]}["expected_cells"] == "FAIL"


# ----------------------------------------------------------------------------- verify: pptx
def test_verify_pptx_checks_every_slide(tmp_path):
    good = box("T", 40, 20, 640, 60, "Intro", 3200, title=True)
    ok_body = box("B", 40, 100, 640, 200, "short text")
    overflow = box("Long", 40, 100, 200, 40, "word " * 120, 2000)
    outside = box("Out", 600, 400, 300, 100, "falls off the slide")
    overlap_a, overlap_b = box("A", 40, 100, 300, 100, "one"), box("B2", 100, 120, 300, 100, "two")
    p = make_pptx(tmp_path / "d.pptx", [good + ok_body, good + overflow, good + outside, good + overlap_a + overlap_b, ""])
    r = verify.verify_pptx(p, expected_slides=5)
    st = {c["name"]: c for c in r["checks"]}
    assert len(r["slides"]) == 5
    assert st["text_overflow_estimate"]["status"] == "FAIL" and "slide 2" in st["text_overflow_estimate"]["detail"]
    assert st["inside_slide_bounds"]["status"] in ("FAIL", "WARN") and "slide 3" in st["inside_slide_bounds"]["detail"]
    assert st["no_overlap"]["status"] == "WARN" and "slide 4" in st["no_overlap"]["detail"]
    assert st["no_empty_slides"]["status"] == "WARN" and "[5]" in st["no_empty_slides"]["detail"]
    assert r["verdict"] == "FAIL"
    assert {c["name"]: c["status"] for c in verify.verify_pptx(p, expected_slides=4)["checks"]}["slide_count"] == "FAIL"
    t = {c["name"]: c["status"] for c in verify.verify_pptx(p, expected_titles=["Intro", "Intro", "Wrong"])["checks"]}
    assert t["slide_order_titles"] == "FAIL"


def test_verify_pptx_clean_deck_and_missing_image(tmp_path):
    p = make_pptx(tmp_path / "c.pptx", [box("T", 40, 20, 640, 60, "Hello", 3200, title=True) + box("B", 40, 100, 640, 200, "fits")])
    assert verify.verify_pptx(p, expected_slides=1, expected_text=["fits"])["verdict"] == "PASS"
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="3" name="Pic"/></p:nvPicPr><p:blipFill><a:blip r:embed="rIdX"/></p:blipFill>'
           '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1270000" cy="1270000"/></a:xfrm></p:spPr></p:pic>')
    q = make_pptx(tmp_path / "pic.pptx", [pic])
    assert {c["name"]: c["status"] for c in verify.verify_pptx(q)["checks"]}["images_present"] == "FAIL"
    (tmp_path / "x.pptx").write_bytes(b"zzzz")
    assert verify.verify_pptx(tmp_path / "x.pptx")["verdict"] == "FAIL"


# ----------------------------------------------------------------------------- verify: pdf / csv / dispatcher
def test_verify_pdf_and_csv_and_dispatch(tmp_path):
    assert verify.verify_pdf(tmp_path / "none.pdf")["verdict"] == "FAIL"
    (tmp_path / "e.pdf").write_bytes(b"")
    assert verify.verify_pdf(tmp_path / "e.pdf")["verdict"] == "FAIL"
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter(); w.add_blank_page(200, 200); w.add_blank_page(200, 200)
    with open(tmp_path / "ok.pdf", "wb") as fh: w.write(fh)
    r = verify.verify_pdf(tmp_path / "ok.pdf", expected_pages=2, expected_text=["hello"])
    st = {c["name"]: c["status"] for c in r["checks"]}
    assert st["page_count"] == "PASS" and st["text_layer"] == "WARN" and st["expected_text_present"] == "FAIL"
    (tmp_path / "t.csv").write_text("a,b\n1,2\n3\n", encoding="utf-8")
    c = verify.verify_csv(tmp_path / "t.csv")
    assert {x["name"]: x["status"] for x in c["checks"]}["rectangular"] == "FAIL"
    (tmp_path / "u.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    d = verify.verify_file(tmp_path / "u.csv", expected_rows=3)
    assert d["detection"]["detected_type"] == "csv"
    bad = verify.verify_file(tmp_path / "u.csv", nonsense=1)
    assert bad["verdict"] == "FAIL"
    (tmp_path / "x.msg").write_bytes(b"\x00\x01\x02" * 50)
    assert verify.verify_file(tmp_path / "x.msg")["verdict"] == "NOT_RUN"


def test_verify_file_flags_extension_mismatch(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document(); d.add_paragraph("some content"); p = tmp_path / "wrongname.xlsx"; d.save(p)
    r = verify.verify_file(p)
    assert r["detection"]["detected_type"] == "docx" and r["checks"][0]["name"] == "format_matches_extension"


# ----------------------------------------------------------------------------- router
@pytest.fixture(scope="module")
def reg():
    return OfficeCapabilityRegistry(FakeProbe(progids={"Word.Application", "Excel.Application", "Outlook.Application", "PowerPoint.Application"}, modules={"pypdf"}))


def test_router_picks_com_over_library_and_explains(reg):
    r = router.route(r"Đọc file C:\data\bao_cao.xlsx", reg)
    assert r["primary_app"] == "excel" and r["operation"] == "read" and r["operation_id"] == "excel.read_range"
    assert r["method"]["selected"] == "object_model" and r["method"]["tool"] == "office.excel_read"
    assert r["method"]["considered"][0]["method"] == "graph_api" and r["method"]["considered"][0]["available"] is False
    assert not r["needs_clarification"]


def test_router_english_and_accentless(reg):
    r = router.route("Please update the formula in C:\\a\\budget.xlsx", reg)
    assert r["primary_app"] == "excel" and r["operation"] == "edit"
    r = router.route("gui email cho sep kem file", reg)
    assert r["primary_app"] == "outlook" and r["operation"] == "send"


def test_router_ambiguity_questions(reg):
    r = router.route("Tạo báo cáo", reg)
    q = r["clarification"]
    assert r["needs_clarification"] and q["kind"] == "application" and q["multi_select"] is True
    assert q["options"][-1]["label"] == "Khác" and q["default"] and q["default_reason"]
    r = router.route("hello", reg)
    assert r["clarification"]["kind"] == "application"
    assert router.route("Edit the budget", reg)["clarifications"] != []


def test_router_never_guesses_recipients_or_files(reg):
    r = router.route("Send the report by email", reg)
    kinds = [c["kind"] for c in r["clarifications"]]
    assert "recipients" in kinds
    r = router.route("Send the report to boss@corp.com by email", reg)
    assert "recipients" not in [c["kind"] for c in r["clarifications"]]
    r = router.route("Sửa file Word", reg)
    assert "which_file" in [c["kind"] for c in r["clarifications"]]
    r = router.route("update both", reg, {"files": [r"C:\a\x.docx", r"C:\b\x.docx"]})
    assert r["primary_app"] == "word" and "which_file" in [c["kind"] for c in r["clarifications"]]


def test_router_cloud_and_overwrite_questions(reg):
    r = router.route("sửa file Word C:\\a\\x.docx trên OneDrive", reg)
    assert "cloud_scope" in [c["kind"] for c in r["clarifications"]]
    r = router.route("create docx report C:\\a\\x.docx", reg, {"output_exists": True})
    assert "overwrite" in [c["kind"] for c in r["clarifications"]]


def test_router_cross_app_workflow_and_unsupported(reg):
    r = router.route("Tạo báo cáo tuần Word từ file Excel rồi gửi email cho sếp kèm PDF", reg)
    assert r["cross_application"] and set(r["involved_apps"]) >= {"word", "excel", "outlook"} and r["workflow_template"] == "weekly_report"
    assert "office.workflow_plan" in r["suggested_tools"]
    r = router.route("Tạo slide PowerPoint trình chiếu", reg)
    assert r["primary_app"] == "powerpoint" and r["operation_id"] == "powerpoint.create_slides"
    assert r["method"]["selected"] == "ui_automation" and r["method"]["fallback"] is True  # no adapter: only an unverified fallback
    r = router.route("Mở Visio vẽ flowchart", reg)
    assert r["method"]["selected"] is None and "not installed" in r["why"][0]


def test_router_graph_unavailable_for_mail(reg):
    r = router.route("đọc email trong Outlook", reg)
    g = r["method"]["considered"][0]
    assert g["method"] == "graph_api" and "not configured/no credentials" in g["reason"]


# ----------------------------------------------------------------------------- tool bindings (registry contract + handlers)
def test_tools_registered_and_workflow_roundtrip(tmp_path):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    from lga_worker.registry import ToolContext
    from lga_worker.server import build_registry

    reg_ = build_registry(Path(__file__).resolve().parents[3] / "tools")
    for t in ("capabilities", "detect", "check_action", "route", "workflow_plan", "workflow_status", "workflow_step", "verify", "learn", "audit_tail"):
        assert f"office.{t}" in reg_.available_tools()
    cfg = SimpleNamespace(path=lambda k: tmp_path / k.split(".")[-1])
    ctx = ToolContext("r", "task1", "s1", time.monotonic() + 30, cfg, None, threading.Event())  # type: ignore[arg-type]

    def call(tool, args):
        return asyncio.run(reg_.invoke(ctx, tool, args))[1]

    plan = call("office.workflow_plan", {"template": "weekly_report", "title": "wk"})
    wid = plan.data["workflow_id"]
    assert "send" in plan.data["report"]["needs_confirmation"] + [s for s in plan.data["order"]]
    out = call("office.workflow_step", {"workflow_id": wid, "step_id": "gather", "action": "start"})
    assert out.data["status"] == "RUNNING"
    out = call("office.workflow_step", {"workflow_id": wid, "step_id": "gather", "action": "succeed"})
    assert out.data["status"] == "UNKNOWN"  # no evidence
    st = call("office.workflow_status", {"workflow_id": wid})
    assert st.data["report"]["overall"] in ("PENDING", "RUNNING", "PARTIAL", "UNKNOWN")
    assert call("office.check_action", {"operation": "office.outlook_send", "params": {"to": "a@b.c"}}).data["decision"] == "CONFIRM"
    assert call("office.audit_tail", {}).data["chain"]["ok"] is True
    assert call("office.capabilities", {"application": "word"}).data["applications"][0]["application_id"] == "word"
