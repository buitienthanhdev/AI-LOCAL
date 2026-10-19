"""Unit tests (no Office, mocks and synthetic OOXML) of the extra Office tools: lga_worker/tools/office_extra.py.

The point of these tests: a result that does not match what was asked must fail with VERIFICATION_FAILED, and the registry
must only report the operations as supported when the tool is declared AND the COM ProgID is registered.
"""

from __future__ import annotations

import asyncio
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.office.registry import OfficeCapabilityRegistry
from lga_worker.registry import ToolContext
from lga_worker.tools import office_extra as ox

TOOLS = Path(__file__).resolve().parents[3] / "tools"
NEW_TOOLS = ["office.word_toc", "office.word_review", "office.word_layout", "office.excel_pivot", "office.excel_chart", "office.excel_format",
             "office.ppt_create", "office.ppt_read", "office.ppt_edit"]
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _zip(path: Path, parts: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for k, v in parts.items():
            z.writestr(k, v)
    return path


# ----------------------------------------------------------------------------- contracts + registry
def test_contracts_declared_and_handlers_bound():
    from lga_worker.server import build_registry

    reg = build_registry(TOOLS)
    for t in NEW_TOOLS:
        assert t in reg.available_tools(), t


class Probe:
    def __init__(self, progids, tools):
        self.progids, self.tools = set(progids), tools

    def com_progid(self, p):
        return {"progid": p, "clsid": "{x}", "server": "C:/x.exe", "server_exists": True, "version": "16.0"} if p in self.progids else None

    def module(self, n):
        return None

    def tool_contract(self, t):
        return {"_file": "office.json", "idempotent": False, "input_schema": {}, "output_schema": {}, "timeout_ms": 1, "side_effects": "yes"} if t in self.tools else None


OPS = {"word": ["word.insert_toc", "word.track_changes", "word.headers_footers"], "excel": ["excel.pivot_table", "excel.chart", "excel.format_cells"],
       "powerpoint": ["powerpoint.create_slides", "powerpoint.edit_slides", "powerpoint.speaker_notes"]}
PROG = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}


@pytest.mark.parametrize("app", list(OPS))
def test_registry_supported_only_with_contract_and_com(app):
    full = OfficeCapabilityRegistry(Probe({PROG[app]}, set(NEW_TOOLS)))
    for op in OPS[app]:
        ok, why = full.can(app, op)
        assert ok and PROG[app] in why, (op, why)
    no_com = OfficeCapabilityRegistry(Probe(set(), set(NEW_TOOLS)))
    no_tool = OfficeCapabilityRegistry(Probe({PROG[app]}, set()))
    for op in OPS[app]:
        assert not no_com.can(app, op)[0] and "not registered" in no_com.can(app, op)[1]
        assert not no_tool.can(app, op)[0] and "not declared" in no_tool.can(app, op)[1]


def test_real_registry_never_claims_live_verification():
    snap = OfficeCapabilityRegistry().snapshot("word", "full")
    toc = [o for o in snap["applications"][0]["supported_operations"] + snap["applications"][0]["unsupported_operations"] if o["operation_id"] == "word.insert_toc"][0]
    assert toc.get("verified_live", False) is False


# ----------------------------------------------------------------------------- small helpers
def test_helpers():
    assert ox._bgr("#FF0000") == 0x0000FF and ox._bgr("00ff00") == 0x00FF00
    with pytest.raises(ToolError):
        ox._bgr("red")
    assert ox._formula_arg(100) == "=100" and ox._formula_arg("x") == '="x"' and ox._formula_arg("=$A1>2") == "=$A1>2"
    assert ox._strip_f(0) == "0" and ox._strip_f('="x"') == "x" and ox._strip_f(None) == ""


# ----------------------------------------------------------------------------- Word
def toc_data(**kw):
    d = {"toc_count": 1, "levels": 3, "headings": ["Intro", "Methods"], "toc_entries": ["Intro", "Methods"], "document": "x.docx"}
    d.update(kw)
    return d


def test_toc_checks_match_and_mismatch():
    assert all(c.passed for c in ox.toc_checks(toc_data(), {}, {"verdict": "PASS"}))
    bad = ox.toc_checks(toc_data(toc_entries=["Intro"]), {}, None)
    assert [c.name for c in bad if not c.passed] == ["toc_entries_match_headings"]
    assert not next(c for c in ox.toc_checks(toc_data(toc_count=0), {}, None) if c.name == "toc_present").passed
    empty = ox.toc_checks(toc_data(headings=[], toc_entries=[]), {}, None)
    assert not next(c for c in empty if c.name == "headings_found").passed  # a TOC of nothing is not a result
    assert not next(c for c in ox.toc_checks(toc_data(), {}, {"verdict": "FAIL", "counts": {}}) if c.name == "office_verify_docx").passed


def review_data(**kw):
    d = {"track_revisions_after": True, "replace_occurrences": 2, "track_during_edit": True, "revisions_before_count": 0, "revisions_after_count": 4,
         "replace_new_present": True, "comments": [{"author": "a", "text": "Check", "scope": ""}], "revisions": [{"type": "insert"}, {"type": "delete"}], "saved": True}
    d.update(kw)
    return d


def test_review_checks():
    info = {"ok": True, "ins": 2, "del": 2, "comments": ["Check"]}
    args = {"track": True, "replace": {"a": "b"}, "comment": {"text": "Check"}}
    assert all(c.passed for c in ox.review_checks(review_data(), args, info))
    assert not next(c for c in ox.review_checks(review_data(track_revisions_after=False), args, info) if c.name == "track_state").passed
    assert not next(c for c in ox.review_checks(review_data(revisions_after_count=0), args, info) if c.name == "edit_is_tracked").passed
    assert not next(c for c in ox.review_checks(review_data(replace_occurrences=0), args, info) if c.name == "replace_found").passed
    assert not next(c for c in ox.review_checks(review_data(comments=[]), args, info) if c.name == "comment_added").passed
    # saved file shows no w:ins/w:del although Word lists text revisions
    assert not next(c for c in ox.review_checks(review_data(), args, {"ok": True, "ins": 0, "del": 0, "comments": ["Check"]}) if c.name == "revisions_in_saved_file").passed
    # accept requested but revisions remain
    assert not next(c for c in ox.review_checks(review_data(), {"resolve": {"action": "accept"}}, info) if c.name == "revisions_resolved").passed
    # untracked edit must not create revisions
    ok = ox.review_checks(review_data(track_during_edit=False, revisions_after_count=0, revisions=[]), {"replace": {"a": "b"}}, {"ok": True, "ins": 0, "del": 0, "comments": ["Check"]})
    assert all(c.passed for c in ok)


def test_docx_review_info_and_layout_info(tmp_path):
    doc = f'<w:document {W_NS}><w:body><w:p><w:ins w:id="1"><w:r><w:t>x</w:t></w:r></w:ins><w:del w:id="2"><w:r><w:delText>y</w:delText></w:r></w:del></w:p>' \
          '<w:sectPr><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/><w:pgMar w:top="1417" w:bottom="1417" w:left="1701" w:right="1134"/></w:sectPr></w:body></w:document>'
    foot = f'<w:ftr {W_NS}><w:p><w:r><w:t>Page </w:t></w:r><w:r><w:instrText> PAGE </w:instrText></w:r></w:p></w:ftr>'
    head = f'<w:hdr {W_NS}><w:p><w:r><w:t>Report</w:t></w:r></w:p></w:hdr>'
    com = f'<w:comments {W_NS}><w:comment w:id="0"><w:p><w:r><w:t>Check</w:t></w:r></w:p></w:comment></w:comments>'
    p = _zip(tmp_path / "a.docx", {"word/document.xml": doc, "word/footer1.xml": foot, "word/header1.xml": head, "word/comments.xml": com})
    r = ox.docx_review_info(p)
    assert (r["ins"], r["del"], r["comments"]) == (1, 1, ["Check"])
    lay = ox.docx_layout_info(p)
    assert lay["sections"][0]["orient"] == "landscape" and lay["footer"][0]["page_field"] is True and lay["header"][0]["text"] == "Report"
    secs = [{"header": "Report", "footer": "Page 1", "header_page_fields": 0, "footer_page_fields": 1, "orientation": "landscape",
             "margins_cm": {"top": 2.5, "bottom": 2.5, "left": 3.0, "right": 2.0}}]
    args = {"header_text": "Report", "footer_text": "Page", "page_numbers": True, "orientation": "landscape", "margins_cm": {"top": 2.5, "bottom": 2.5, "left": 3, "right": 2}}
    checks = ox.layout_checks({"sections": secs}, args, lay)
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    # wrong things are caught
    assert not next(c for c in ox.layout_checks({"sections": secs}, {**args, "orientation": "portrait"}, lay) if c.name == "orientation_file").passed
    assert not next(c for c in ox.layout_checks({"sections": secs}, {**args, "header_text": "Other"}, lay) if c.name == "header_text_file").passed
    nofield = {**lay, "footer": [{"part": "f", "text": "Page", "page_field": False}]}
    assert not next(c for c in ox.layout_checks({"sections": secs}, args, nofield) if c.name == "page_numbers_file").passed
    assert not ox.layout_checks({"sections": secs}, args, {"ok": False})[-1].passed


# ----------------------------------------------------------------------------- Excel
def test_expected_series():
    rows = [["Region", "Product", "Sales"], ["N", "A", 1], ["S", "B", 2]]
    assert ox.expected_series(rows, "column") == (1, 2)
    assert ox.expected_series([["x", "a", "b"], ["p", 1, 2], ["q", 3, 4]], "line") == (2, 2)
    assert ox.expected_series(rows, "pie") == (1, 2)
    assert ox.expected_series([["x", "y"], [1, 2], [2, 3]], "scatter") == (1, 2)


def chart_data(**kw):
    d = {"source_values": [["Region", "Sales"], ["N", 1], ["S", 2]], "charts_on_sheet_before": 0, "charts_on_sheet_after": 1, "charts_in_book_before": 0,
         "chart": {"chart_type": 51, "title": "T", "series": [{"name": "Sales", "points": 2}]}}
    d.update(kw)
    return d


def test_chart_checks():
    info = {"ok": True, "charts": [{"kinds": ["barChart"], "bar_dir": "col", "title": "T", "series": 1}]}
    args = {"source_range": "A1:B3", "chart_type": "column", "title": "T"}
    assert all(c.passed for c in ox.chart_checks(chart_data(), args, info))
    assert not next(c for c in ox.chart_checks(chart_data(charts_on_sheet_after=0), args, info) if c.name == "chart_exists_com").passed
    assert not next(c for c in ox.chart_checks(chart_data(chart={"chart_type": 4, "title": "T", "series": [{"points": 2}]}), args, info) if c.name == "chart_type_com").passed
    assert not next(c for c in ox.chart_checks(chart_data(chart={"chart_type": 51, "title": "T", "series": [{"points": 1}]}), args, info) if c.name == "series_points_com").passed
    assert not next(c for c in ox.chart_checks(chart_data(), args, {"ok": True, "charts": []}) if c.name == "chart_in_saved_file").passed
    assert not next(c for c in ox.chart_checks(chart_data(), args, {"ok": True, "charts": [{**info["charts"][0], "kinds": ["lineChart"]}]}) if c.name == "chart_kind_file").passed


def test_pivot_expected_and_checks():
    src = [["Region", "Sales"], ["N", 100], ["S", 250], ["N", 50]]
    exp = ox.pivot_expected(src, ["Region"], [{"field": "Sales", "function": "sum"}])
    assert exp["grand"] == [400.0] and exp["groups"] == {"N": [150.0], "S": [250.0]}
    assert ox.pivot_expected(src, ["Region"], [{"field": "Sales", "function": "average"}])["groups"]["N"] == [75.0]
    assert ox.pivot_expected(src, [], [{"field": "Sales", "function": "count"}])["grand"] == [3.0]
    data = {"pivot_name": "P1", "pivots_after": ["P1"], "data_fields": [{"field": "Sales", "name": "Sum of Sales"}], "pivot_range": "A3:B6", "source_values": src,
            "result": [["Row Labels", "Sum of Sales"], ["N", 150.0], ["S", 250.0], ["Grand Total", 400.0]]}
    args = {"rows": ["Region"], "values": [{"field": "Sales", "function": "sum"}]}
    info = {"ok": True, "pivot_names": ["P1"]}
    assert all(c.passed for c in ox.pivot_checks(data, args, info))
    wrong = {**data, "result": [["Row Labels", "Sum of Sales"], ["N", 151.0], ["S", 250.0], ["Grand Total", 400.0]]}
    assert not next(c for c in ox.pivot_checks(wrong, args, info) if c.name == "pivot_groups").passed
    wrong = {**data, "result": [["Row Labels", "Sum of Sales"], ["N", 150.0], ["S", 250.0], ["Grand Total", 999.0]]}
    assert not next(c for c in ox.pivot_checks(wrong, args, info) if c.name == "pivot_grand_total").passed
    assert not next(c for c in ox.pivot_checks(data, args, {"ok": True, "pivot_names": []}) if c.name == "pivot_in_saved_file").passed


def test_format_checks_and_sheet_xml(tmp_path):
    S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sheet = f'<worksheet {S}><conditionalFormatting sqref="C2:C7"><cfRule type="cellIs" operator="greaterThan" priority="1"><formula>100</formula></cfRule></conditionalFormatting>' \
            '<dataValidations count="1"><dataValidation type="whole" operator="between" sqref="C9"><formula1>0</formula1><formula2>1000</formula2></dataValidation></dataValidations></worksheet>'
    p = _zip(tmp_path / "a.xlsx", {
        "xl/workbook.xml": f'<workbook {S}><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>',
        "xl/worksheets/sheet1.xml": sheet, "xl/styles.xml": f'<styleSheet {S}><numFmts><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts></styleSheet>'})
    info = ox.xlsx_format_info(p, "data")
    assert info["conditional"][0]["type"] == "cellIs" and info["validation"][0]["type"] == "whole" and "#,##0.00" in info["number_formats"]
    args = {"number_format": "#,##0.00", "conditional": [{"kind": "cell_value", "operator": "greater", "value": 100, "fill_color": "#FFC7CE"}],
            "validation": {"type": "whole", "operator": "between", "min": 0, "max": 1000}}
    data = {"number_format_after": "#,##0.00", "display_first_cell": "1.00", "conditional_before": 0, "saved": True,
            "conditional_after": [{"type": 1, "operator": 5, "formula1": "=100", "formula2": None, "fill": ox._bgr("#FFC7CE")}],
            "validation_after": {"type": 1, "operator": 1, "formula1": "0", "formula2": "1000"}}
    checks = ox.format_checks(data, args, info)
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    bad = {**data, "number_format_after": "General"}
    assert not next(c for c in ox.format_checks(bad, args, info) if c.name == "number_format_com").passed
    bad = {**data, "conditional_after": []}
    assert not next(c for c in ox.format_checks(bad, args, info) if c.name == "conditional_count_com").passed
    bad = {**data, "validation_after": {"type": 3, "operator": None, "formula1": "a,b", "formula2": None}}
    assert not next(c for c in ox.format_checks(bad, args, info) if c.name == "validation_com").passed
    assert not next(c for c in ox.format_checks(data, args, {**info, "conditional": []}) if c.name == "conditional_in_saved_file").passed


def test_chart_and_pivot_part_info(tmp_path):
    chart = '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart>' \
            '<c:title><c:tx><c:rich><a:p><a:r><a:t>Sales</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:barChart><c:barDir val="col"/><c:ser/><c:ser/></c:barChart></c:plotArea></c:chart></c:chartSpace>'
    pv = '<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="P1"/>'
    p = _zip(tmp_path / "b.xlsx", {"xl/charts/chart1.xml": chart, "xl/pivotTables/pivotTable1.xml": pv})
    c = ox.chart_part_info(p)["charts"][0]
    assert c["kinds"] == ["barChart"] and c["bar_dir"] == "col" and c["title"] == "Sales" and c["series"] == 2
    assert ox.pivot_part_info(p)["pivot_names"] == ["P1"]
    assert ox.chart_part_info(tmp_path / "missing.xlsx")["ok"] is False


# ----------------------------------------------------------------------------- PowerPoint
def make_pptx(path: Path, slides: list[dict]) -> Path:
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    def sp(ph, paras):
        body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras)
        return f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="x"/><p:cNvSpPr/><p:nvPr><p:ph type="{ph}"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody>{body}</p:txBody></p:sp>'

    parts = {"ppt/presentation.xml": f'<p:presentation {ns}><p:sldIdLst>' + "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides))) + "</p:sldIdLst></p:presentation>"}
    rels = "".join(f'<Relationship Id="rId{i + 1}" Target="slides/slide{i + 1}.xml" Type="x/slide"/>' for i in range(len(slides)))
    parts["ppt/_rels/presentation.xml.rels"] = f'<Relationships xmlns="{ox.PKG_REL}">{rels}</Relationships>'
    for i, s in enumerate(slides, 1):
        shapes = sp("title", [s["title"]]) + (sp("body", s["bullets"]) if s["bullets"] else "")
        parts[f"ppt/slides/slide{i}.xml"] = f"<p:sld {ns}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
        if s["notes"]:
            parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = f'<Relationships xmlns="{ox.PKG_REL}"><Relationship Id="rId1" Target="../notesSlides/notesSlide{i}.xml" Type="x/notesSlide"/></Relationships>'
            parts[f"ppt/notesSlides/notesSlide{i}.xml"] = f"<p:notes {ns}><p:cSld><p:spTree>{sp('body', [s['notes']])}</p:spTree></p:cSld></p:notes>"
    return _zip(path, parts)


def test_pptx_read_zip(tmp_path):
    slides = [{"title": "Deck", "bullets": ["Sub"], "notes": ""}, {"title": "Facts", "bullets": ["a", "b"], "notes": "Say hi"}]
    got = ox.pptx_read_zip(make_pptx(tmp_path / "d.pptx", slides))
    assert got["ok"] and got["slides"] == slides


def test_ppt_create_and_edit_checks(tmp_path):
    slides = [{"title": "Deck", "bullets": ["Sub"], "notes": ""}, {"title": "Facts", "bullets": ["alpha", "b"], "notes": "Say hi"}]
    info = ox.pptx_read_zip(make_pptx(tmp_path / "d.pptx", slides))
    args = {"title": "Deck", "subtitle": "Sub", "slides": [{"title": "Facts", "bullets": ["alpha", "b"], "notes": "Say hi"}]}
    data = {"slides": slides}
    assert all(c.passed for c in ox.ppt_create_checks(data, args, info, {"verdict": "PASS"}))
    assert not next(c for c in ox.ppt_create_checks(data, {**args, "title": "Other"}, info, None) if c.name == "slide_content_file").passed
    assert not next(c for c in ox.ppt_create_checks({"slides": slides[:1]}, args, info, None) if c.name == "com_matches_file").passed
    assert not next(c for c in ox.ppt_create_checks(data, args, info, {"verdict": "FAIL"}) if c.name == "office_verify_pptx").passed
    # edit: one slide added, text replaced, notes set
    after = slides + [{"title": "Risks", "bullets": ["Delays"], "notes": ""}]
    after[1] = {"title": "Facts", "bullets": ["ALPHA2", "b"], "notes": "New notes"}
    info2 = ox.pptx_read_zip(make_pptx(tmp_path / "e.pptx", after))
    eargs = {"replace": {"alpha": "ALPHA2"}, "add_slide": {"title": "Risks", "bullets": ["Delays"]}, "notes": {"slide": 2, "text": "New notes"}}
    edata = {"slides_before": slides, "slides_after": after, "replace_counts": {"alpha": 1}}
    checks = ox.ppt_edit_checks(edata, eargs, info2, {"verdict": "WARN"})
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    # the saved file does not hold the edit although COM said so
    stale = ox.pptx_read_zip(make_pptx(tmp_path / "f.pptx", slides))
    failed = [c.name for c in ox.ppt_edit_checks(edata, eargs, stale, None) if not c.passed]
    assert "slide_count_file" in failed and "added_slide_file" in failed and "notes_file" in failed
    assert not next(c for c in ox.ppt_edit_checks({**edata, "replace_counts": {"alpha": 0}}, eargs, info2, None) if c.name.startswith("replace_found")).passed


# ----------------------------------------------------------------------------- handlers with a fake Office
class FakeOffice:
    def __init__(self, returns):
        self.returns = returns
        self.calls = []
        self.executor = SimpleNamespace(run=self._run)

    async def _run(self, timeout, fn, *a):
        return fn(*a)

    def __getattr__(self, name):
        def method(*a):
            self.calls.append((name, a))
            return self.returns[name]

        return method


class Services:
    def __init__(self, office):
        self.office = office

    async def get_or_create(self, name, factory):
        return self.office


def invoke(tool, args, office):
    from lga_worker.server import build_registry

    reg = build_registry(TOOLS)
    ctx = ToolContext("r", "t", "s", time.monotonic() + 30, SimpleNamespace(path=lambda k: Path(".")), Services(office), threading.Event())  # type: ignore[arg-type]
    return asyncio.run(reg.invoke(ctx, tool, args))[1]


def test_handler_word_toc_fails_when_entries_differ(tmp_path):
    doc = tmp_path / "x.docx"
    doc.write_bytes(b"not a docx")
    office = FakeOffice({"word_toc": toc_data(toc_entries=["Intro"], document=str(doc), inserted=True)})
    with pytest.raises(ToolError) as e:
        invoke("office.word_toc", {"path": str(doc)}, office)
    assert e.value.code == ErrorCode.VERIFICATION_FAILED and "toc_entries_match_headings" in str(e.value)


def test_handler_word_review_success_and_failure(tmp_path):
    doc = _zip(tmp_path / "x.docx", {"word/document.xml": f'<w:document {W_NS}><w:body><w:p><w:ins w:id="1"><w:r><w:t>x</w:t></w:r></w:ins></w:p></w:body></w:document>',
                                      "word/comments.xml": f'<w:comments {W_NS}><w:comment w:id="0"><w:p><w:r><w:t>Check</w:t></w:r></w:p></w:comment></w:comments>'})
    good = review_data(document=str(doc), revisions=[{"type": "insert"}], comments=[{"author": "a", "text": "Check", "scope": ""}])
    out = invoke("office.word_review", {"path": str(doc), "track": True, "replace": {"a": "b"}, "comment": {"text": "Check"}}, FakeOffice({"word_review": good}))
    assert out.checks and all(c.passed for c in out.checks) and out.data["verification"]
    bad = {**good, "track_revisions_after": False}
    with pytest.raises(ToolError) as e:
        invoke("office.word_review", {"path": str(doc), "track": True}, FakeOffice({"word_review": bad}))
    assert e.value.code == ErrorCode.VERIFICATION_FAILED


def test_handler_word_layout_requires_something_and_verifies(tmp_path):
    with pytest.raises(ToolError) as e:
        invoke("office.word_layout", {"path": str(tmp_path / "x.docx")}, FakeOffice({}))
    assert e.value.code == ErrorCode.INVALID_ARGUMENT
    doc = _zip(tmp_path / "x.docx", {"word/document.xml": f'<w:document {W_NS}><w:body><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'})
    secs = [{"header": "", "footer": "", "header_page_fields": 0, "footer_page_fields": 0, "orientation": "landscape", "margins_cm": {}}]
    with pytest.raises(ToolError) as e:  # Word says landscape, the saved file is still portrait
        invoke("office.word_layout", {"path": str(doc), "orientation": "landscape"}, FakeOffice({"word_layout": {"document": str(doc), "sections": secs, "saved": True}}))
    assert e.value.code == ErrorCode.VERIFICATION_FAILED and "orientation_file" in str(e.value)


def test_handler_excel_pivot_detects_wrong_result(tmp_path):
    book = _zip(tmp_path / "p.xlsx", {"xl/pivotTables/pivotTable1.xml": '<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="P1"/>'})
    src = [["Region", "Sales"], ["N", 100], ["S", 250]]
    base = {"workbook": str(book), "sheet": "Pivot", "pivot_name": "P1", "pivots_after": ["P1"], "pivot_range": "A3:B6", "source_values": src,
            "data_fields": [{"name": "Sum of Sales", "field": "Sales"}], "saved": True,
            "result": [["Row Labels", "Sum of Sales"], ["N", 100.0], ["S", 250.0], ["Grand Total", 350.0]]}
    args = {"path": str(book), "sheet": "Data", "source_range": "A1:B3", "rows": ["Region"], "values": ["Sales"]}
    out = invoke("office.excel_pivot", args, FakeOffice({"excel_pivot": base}))
    assert all(c.passed for c in out.checks)
    wrong = {**base, "result": [["Row Labels", "Sum of Sales"], ["N", 100.0], ["S", 999.0], ["Grand Total", 1099.0]]}
    with pytest.raises(ToolError) as e:
        invoke("office.excel_pivot", args, FakeOffice({"excel_pivot": wrong}))
    assert e.value.code == ErrorCode.VERIFICATION_FAILED
    with pytest.raises(ToolError):
        invoke("office.excel_pivot", {"path": str(book)}, FakeOffice({}))


def test_handler_ppt_edit_requires_change_and_ppt_read_is_read_only():
    with pytest.raises(ToolError) as e:
        invoke("office.ppt_edit", {"path": "C:/x.pptx"}, FakeOffice({}))
    assert e.value.code == ErrorCode.INVALID_ARGUMENT
    data = {"presentation": "C:/x.pptx", "slide_count": 1, "slides": [{"index": 1, "title": "T", "bullets": ["b"], "notes": "", "layout": "Title"}]}
    out = invoke("office.ppt_read", {"path": "C:/x.pptx"}, FakeOffice({"ppt_read": data}))
    assert out.data["slide_count"] == 1 and "T" in out.observation


def test_handler_ppt_create_fails_when_saved_file_differs(tmp_path):
    p = make_pptx(tmp_path / "d.pptx", [{"title": "Other", "bullets": [], "notes": ""}])
    data = {"presentation": str(p), "slides": [{"index": 1, "title": "Deck", "bullets": [], "notes": "", "layout": "Title"}], "saved": True}
    with pytest.raises(ToolError) as e:
        invoke("office.ppt_create", {"path": str(p), "title": "Deck"}, FakeOffice({"ppt_create": data}))
    assert e.value.code == ErrorCode.VERIFICATION_FAILED
