"""Extra Office tools (contract: tools/office.json): Word TOC / review / layout, Excel pivot / chart / format, PowerPoint create / read / edit.

* The COM methods live in `OfficeExtra`, a mixin of `lga_worker.tools.office.Office` (same STA thread, same helpers).
* Every handler re-reads what it wrote: once through COM (the application's own view) and once from the SAVED file
  (zip + XML, the same way office.verify does) and returns failed `Check`s when the two do not match what was asked.
  The registry turns a failed check of a `verification: "self"` tool into VERIFICATION_FAILED. No success is claimed
  from the absence of an error.
* The pure functions (`*_checks`, parsers) need no Office and are unit tested with synthetic files.
"""

from __future__ import annotations

import asyncio
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paths import absolute_path
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "office-com"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CM_TO_TWIPS = 566.929
CM_TO_PT = 28.3465
MAX_PARAS = 4000

XL_OPS = {"between": 1, "not_between": 2, "equal": 3, "not_equal": 4, "greater": 5, "less": 6, "greater_equal": 7, "less_equal": 8}
CF_OPERATOR_XML = {1: "between", 2: "notBetween", 3: "equal", 4: "notEqual", 5: "greaterThan", 6: "lessThan", 7: "greaterThanOrEqual", 8: "lessThanOrEqual"}
DV_TYPES = {"whole": 1, "decimal": 2, "list": 3, "date": 4, "time": 5, "text_length": 6, "custom": 7}
DV_XML = {"whole": "whole", "decimal": "decimal", "list": "list", "date": "date", "time": "time", "text_length": "textLength", "custom": "custom"}
CHART_TYPES = {"column": (51, "barChart"), "bar": (57, "barChart"), "line": (4, "lineChart"), "pie": (5, "pieChart"),
               "scatter": (-4169, "scatterChart"), "area": (1, "areaChart")}
PIVOT_FUNCS = {"sum": -4157, "count": -4112, "average": -4106, "max": -4136, "min": -4139}


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _lab(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _norm(v)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bgr(color: str) -> int:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color or "")
    if not m:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"colour must be #RRGGBB, got {color!r}")
    h = m.group(1)
    return int(h[4:6], 16) << 16 | int(h[2:4], 16) << 8 | int(h[0:2], 16)


def _formula_arg(v: Any) -> str:
    if isinstance(v, bool):
        return f"={str(v).upper()}"
    if isinstance(v, (int, float)):
        return f"={v}"
    s = str(v)
    return s if s.startswith("=") else f'="{s}"'


def _strip_f(s: Any) -> str:
    return ("" if s is None else str(s)).lstrip("=").replace('"', "").strip()


def _chk(name: str, ok: bool, detail: str) -> Check:
    return Check(name, bool(ok), detail)


def _read_zip(path: str | Path) -> zipfile.ZipFile | None:
    try:
        return zipfile.ZipFile(str(path))
    except (OSError, zipfile.BadZipFile):
        return None


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError):
        return None


def _para_text(p: ET.Element) -> str:
    return "".join((el.text or "") for el in p.iter(q(A, "t")))


# ============================================================================================ Word: pure readers / checks
def docx_review_info(path: str | Path) -> dict[str, Any]:
    z = _read_zip(path)
    if z is None:
        return {"ok": False}
    with z:
        doc = _xml(z, "word/document.xml")
        if doc is None:
            return {"ok": False}
        ins = len(list(doc.iter(q(W, "ins"))))
        dels = len(list(doc.iter(q(W, "del"))))
        comments: list[str] = []
        root = _xml(z, "word/comments.xml")
        if root is not None:
            for c in root.iter(q(W, "comment")):
                comments.append(_norm("".join((t.text or "") for t in c.iter(q(W, "t")))))
        return {"ok": True, "ins": ins, "del": dels, "comments": comments}


def docx_layout_info(path: str | Path) -> dict[str, Any]:
    z = _read_zip(path)
    if z is None:
        return {"ok": False}
    with z:
        doc = _xml(z, "word/document.xml")
        if doc is None:
            return {"ok": False}
        sections = []
        for sp in doc.iter(q(W, "sectPr")):
            sz, mar = sp.find(q(W, "pgSz")), sp.find(q(W, "pgMar"))
            sec: dict[str, Any] = {}
            if sz is not None:
                sec.update(w=int(sz.get(q(W, "w"), "0") or 0), h=int(sz.get(q(W, "h"), "0") or 0), orient=sz.get(q(W, "orient"), "portrait"))
            if mar is not None:
                sec["margins"] = {k: int(mar.get(q(W, k), "0") or 0) for k in ("top", "bottom", "left", "right")}
            sections.append(sec)
        parts: dict[str, list[dict[str, Any]]] = {"header": [], "footer": []}
        for name in z.namelist():
            m = re.fullmatch(r"word/(header|footer)\d*\.xml", name)
            if not m:
                continue
            root = _xml(z, name)
            if root is None:
                continue
            text = _norm("".join((t.text or "") for t in root.iter(q(W, "t"))))
            instr = [(i.text or "").strip().upper() for i in root.iter(q(W, "instrText"))] + \
                    [f.get(q(W, "instr"), "").strip().upper() for f in root.iter(q(W, "fldSimple"))]
            parts[m.group(1)].append({"part": name, "text": text, "page_field": any(x.startswith("PAGE") for x in instr)})
        return {"ok": True, "sections": sections, **parts}


def layout_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    secs = data.get("sections", [])
    for key, label in (("header_text", "header"), ("footer_text", "footer")):
        want = args.get(key)
        if want is not None:
            out.append(_chk(f"{label}_text_com", bool(secs) and all(_norm(s[label]).startswith(_norm(want)) for s in secs),
                            f"Word reads {[s[label] for s in secs]!r}, wanted text starting {want!r}"))
    pn = args.get("page_numbers")
    pos = args.get("page_number_position", "footer")
    if pn is not None:
        counts = [s[f"{pos}_page_fields"] for s in secs]
        okp = bool(secs) and (all(c > 0 for c in counts) if pn else all(c == 0 for c in counts))
        out.append(_chk("page_numbers_com", okp, f"PAGE fields in the {pos} per section: {counts}, wanted {'>0' if pn else '0'}"))
    orient = args.get("orientation")
    if orient:
        out.append(_chk("orientation_com", bool(secs) and all(s["orientation"] == orient for s in secs), f"Word reports {[s['orientation'] for s in secs]}, wanted {orient}"))
    mg = args.get("margins_cm")
    if mg:
        bad = [(s["margins_cm"]) for s in secs if any(abs(s["margins_cm"][k] - v) > 0.05 for k, v in mg.items())]
        out.append(_chk("margins_com", bool(secs) and not bad, f"Word reports {[s['margins_cm'] for s in secs]}, wanted {mg}"))
    # the saved file
    if not info.get("ok"):
        out.append(_chk("saved_file_readable", False, "the saved .docx could not be re-read as zip+XML"))
        return out
    sx = info["sections"]
    if orient:
        ok = bool(sx) and all(s.get("orient") == "landscape" and s.get("w", 0) > s.get("h", 0) if orient == "landscape"
                              else s.get("orient", "portrait") != "landscape" and s.get("w", 0) < s.get("h", 1) for s in sx)
        out.append(_chk("orientation_file", ok, f"sectPr in the saved file: {[(s.get('orient'), s.get('w'), s.get('h')) for s in sx]}"))
    if mg:
        key = {"top": "top", "bottom": "bottom", "left": "left", "right": "right"}
        ok = bool(sx) and all(abs(s.get("margins", {}).get(key[k], -9999) - v * CM_TO_TWIPS) <= 25 for s in sx for k, v in mg.items())
        out.append(_chk("margins_file", ok, f"pgMar (twips) in the saved file: {[s.get('margins') for s in sx]}, wanted {mg} cm"))
    for key, label in (("header_text", "header"), ("footer_text", "footer")):
        want = args.get(key)
        if want:
            out.append(_chk(f"{label}_text_file", any(_norm(want) in p["text"] for p in info[label]),
                            f"{label} parts of the saved file: {[p['text'] for p in info[label]]!r}"))
    if pn is not None:
        have = any(p["page_field"] for p in info[pos])
        out.append(_chk("page_numbers_file", have == bool(pn), f"PAGE field in a saved {pos} part: {have}, wanted {bool(pn)}"))
    return out


def _headings_match(headings: list[str], entries: list[str]) -> tuple[bool, list[str], list[str]]:
    h = [_norm(x) for x in headings]
    e = [_norm(x) for x in entries]
    missing = [x for x in h if x not in e]
    extra = [x for x in e if x not in h]
    return (h == e), missing, extra


def toc_checks(data: dict[str, Any], args: dict[str, Any], verdict: dict[str, Any] | None) -> list[Check]:
    ok, missing, extra = _headings_match(data["headings"], data["toc_entries"])
    out = [_chk("toc_present", data["toc_count"] >= 1, f"{data['toc_count']} table(s) of contents in the document"),
           _chk("headings_found", len(data["headings"]) > 0, f"{len(data['headings'])} heading(s) up to level {data['levels']}"
                + ("" if data["headings"] else " (a TOC of nothing is not a result: add headings first)")),
           _chk("toc_entries_match_headings", ok and bool(data["headings"]),
                f"{len(data['toc_entries'])} entries vs {len(data['headings'])} headings; missing in TOC: {missing[:5]}; not a heading: {extra[:5]}")]
    if verdict is not None:
        out.append(_chk("office_verify_docx", verdict["verdict"] != "FAIL", f"office.verify verdict {verdict['verdict']}: counts {verdict.get('counts')}"))
    return out


def review_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    track = args.get("track")
    if track is not None:
        out.append(_chk("track_state", data["track_revisions_after"] == bool(track), f"TrackRevisions is {data['track_revisions_after']}, wanted {bool(track)}"))
    if args.get("replace"):
        if data["replace_occurrences"] == 0:
            out.append(_chk("replace_found", False, "none of the texts to replace was found in the document"))
        elif data["track_during_edit"]:
            new = data["revisions_after_count"] - data["revisions_before_count"]
            out.append(_chk("edit_is_tracked", new >= 1, f"{new} new revision(s) after replacing {data['replace_occurrences']} occurrence(s) with tracking on"))
        else:
            out.append(_chk("edit_untracked", data["revisions_after_count"] == data["revisions_before_count"], "tracking was off: no new revision expected"))
        out.append(_chk("replace_text_present", data["replace_new_present"], "the new text is in the document after the edit"))
    cm = args.get("comment")
    if cm:
        out.append(_chk("comment_added", any(_norm(cm["text"]) == _norm(c["text"]) for c in data["comments"]),
                        f"{len(data['comments'])} comment(s) read back from Word"))
    rs = args.get("resolve")
    if rs:
        out.append(_chk("revisions_resolved", data["revisions_after_count"] == 0, f"{data['revisions_after_count']} revision(s) left after {rs['action']} all"))
    if not info.get("ok"):
        if data.get("saved"):
            out.append(_chk("saved_file_readable", False, "the saved .docx could not be re-read as zip+XML"))
        return out
    if data.get("saved"):
        cnt = info["ins"] + info["del"]
        has_text_rev = any(r["type"] in ("insert", "delete") for r in data["revisions"])
        out.append(_chk("revisions_in_saved_file", (cnt > 0) == has_text_rev, f"saved file has {info['ins']} w:ins and {info['del']} w:del; Word lists {len(data['revisions'])} revision(s)"))
        out.append(_chk("comments_in_saved_file", [_norm(c["text"]) for c in data["comments"]] == info["comments"],
                        f"comments.xml has {len(info['comments'])} comment(s), Word lists {len(data['comments'])}"))
    return out


# ============================================================================================ Excel: pure readers / checks
def _sheet_part(z: zipfile.ZipFile, sheet: str) -> str | None:
    wb = _xml(z, "xl/workbook.xml")
    rels = _xml(z, "xl/_rels/workbook.xml.rels")
    if wb is None or rels is None:
        return None
    target = {r.get("Id"): r.get("Target", "") for r in rels}
    for sh in wb.iter(q(S, "sheet")):
        if sh.get("name", "").lower() == sheet.lower():
            t = target.get(sh.get(q(R, "id")), "")
            return t.lstrip("/") if t.startswith("/") else "xl/" + t
    return None


def xlsx_format_info(path: str | Path, sheet: str) -> dict[str, Any]:
    z = _read_zip(path)
    if z is None:
        return {"ok": False}
    with z:
        part = _sheet_part(z, sheet)
        root = _xml(z, part) if part else None
        if root is None:
            return {"ok": False}
        cfs = []
        for cf in root.iter(q(S, "conditionalFormatting")):
            for rule in cf.findall(q(S, "cfRule")):
                cfs.append({"sqref": cf.get("sqref", ""), "type": rule.get("type"), "operator": rule.get("operator"),
                            "formula": [(f.text or "") for f in rule.findall(q(S, "formula"))]})
        dvs = []
        for dv in root.iter(q(S, "dataValidation")):
            f1, f2 = dv.find(q(S, "formula1")), dv.find(q(S, "formula2"))
            dvs.append({"sqref": dv.get("sqref", ""), "type": dv.get("type"), "operator": dv.get("operator") or "between",
                        "formula1": f1.text if f1 is not None else None, "formula2": f2.text if f2 is not None else None})
        styles = _xml(z, "xl/styles.xml")
        codes = [n.get("formatCode", "") for n in styles.iter(q(S, "numFmt"))] if styles is not None else []
        return {"ok": True, "conditional": cfs, "validation": dvs, "number_formats": codes}


def format_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any]) -> list[Check]:
    out: list[Check] = []
    if args.get("number_format") is not None:
        out.append(_chk("number_format_com", data["number_format_after"] == args["number_format"],
                        f"Excel reports NumberFormat {data['number_format_after']!r}, wanted {args['number_format']!r}; first cell displays {data['display_first_cell']!r}"))
    conds = args.get("conditional") or []
    if conds:
        got = data["conditional_after"]
        want_n = len(conds) if args.get("replace_existing", True) else len(conds) + data["conditional_before"]
        out.append(_chk("conditional_count_com", len(got) == want_n, f"{len(got)} rule(s) read back, wanted {want_n}"))
        tail = got[len(got) - len(conds):] if len(got) >= len(conds) else []
        for i, (want, have) in enumerate(zip(conds, tail)):
            okk = have["type"] == (1 if want["kind"] == "cell_value" else 2)
            if want["kind"] == "cell_value":
                okk = okk and have["operator"] == XL_OPS[want["operator"]] and _strip_f(have["formula1"]) == _strip_f(_formula_arg(want["value"]))
                if want["operator"] in ("between", "not_between"):
                    okk = okk and _strip_f(have["formula2"]) == _strip_f(_formula_arg(want["value2"]))
            else:
                okk = okk and _strip_f(have["formula1"]).replace(" ", "") == _strip_f(want["formula"]).replace(" ", "")
            if want.get("fill_color"):
                okk = okk and have["fill"] == _bgr(want["fill_color"])
            out.append(_chk(f"conditional_{i + 1}_com", okk, f"read back {have}, wanted {want}"))
    dv = args.get("validation")
    if dv:
        have = data["validation_after"]
        okk = bool(have) and have["type"] == DV_TYPES[dv["type"]]
        if dv["type"] == "list":
            okk = okk and _strip_f(have["formula1"]) == _strip_f(dv["source"])
        elif dv["type"] == "custom":
            okk = okk and _strip_f(have["formula1"]) == _strip_f(dv["formula"])
        else:
            okk = okk and have["operator"] == XL_OPS[dv.get("operator", "between")] and _strip_f(have["formula1"]) == _strip_f(dv.get("min", dv.get("value")))
            if dv.get("operator", "between") in ("between", "not_between"):
                okk = okk and _strip_f(have["formula2"]) == _strip_f(dv.get("max"))
        out.append(_chk("validation_com", okk, f"read back {have}, wanted {dv}"))
    if not info.get("ok"):
        if data.get("saved"):
            out.append(_chk("saved_file_readable", False, "the saved .xlsx sheet could not be re-read as zip+XML"))
        return out
    if conds:
        want_n = len(conds) if args.get("replace_existing", True) else len(conds) + data["conditional_before"]
        types = [c["type"] for c in info["conditional"]]
        exp_types = [("cellIs" if c["kind"] == "cell_value" else "expression") for c in conds]
        out.append(_chk("conditional_in_saved_file", len(types) == want_n and types[len(types) - len(conds):] == exp_types, f"saved sheet XML has cfRule types {types}, wanted tail {exp_types}"))
    if dv:
        okk = any(d["type"] == DV_XML[dv["type"]] for d in info["validation"])
        out.append(_chk("validation_in_saved_file", okk, f"saved sheet XML has dataValidation {[(d['type'], d['formula1']) for d in info['validation']]}"))
    return out


def chart_part_info(path: str | Path) -> dict[str, Any]:
    z = _read_zip(path)
    if z is None:
        return {"ok": False, "charts": []}
    with z:
        charts = []
        for name in sorted(n for n in z.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)):
            root = _xml(z, name)
            if root is None:
                continue
            plot = root.find(f".//{q(C, 'plotArea')}")
            kinds = [el.tag.split("}")[1] for el in (plot if plot is not None else []) if el.tag.endswith("Chart")]
            bar_dir = next((b.get("val") for b in root.iter(q(C, "barDir"))), None)
            title_el = root.find(f"{q(C, 'chart')}/{q(C, 'title')}")
            title = _norm("".join((t.text or "") for t in title_el.iter(q(A, "t")))) if title_el is not None else ""
            charts.append({"part": name, "kinds": kinds, "bar_dir": bar_dir, "title": title, "series": len(list(root.iter(q(C, "ser"))))})
        return {"ok": True, "charts": charts}


def expected_series(rows: list[list[Any]], chart_type: str) -> tuple[int, int]:
    """(series, points per series) Excel should create for a header+data block: every fully numeric column is a series,
    text columns are category labels (a numeric first column of a scatter chart is the X axis)."""
    ncols = max((len(r) for r in rows), default=0)
    data = rows[1:]
    npts = len(data)
    numeric = [c for c in range(ncols) if data and all(_num(r[c]) is not None for r in data if len(r) > c)]
    if chart_type == "pie":
        return 1, npts
    if chart_type == "scatter":
        return max(len(numeric) - 1, 1), npts
    return max(len(numeric), 1), npts


def chart_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any]) -> list[Check]:
    ct = args.get("chart_type", "column")
    want_series, want_pts = expected_series(data["source_values"], ct)
    ch = data["chart"]
    out = [_chk("chart_exists_com", data["charts_on_sheet_after"] == data["charts_on_sheet_before"] + 1, f"charts on sheet {data['charts_on_sheet_before']} -> {data['charts_on_sheet_after']}"),
           _chk("chart_type_com", ch["chart_type"] == CHART_TYPES[ct][0], f"Excel ChartType {ch['chart_type']}, wanted {CHART_TYPES[ct][0]} ({ct})"),
           _chk("series_count_com", len(ch["series"]) == want_series, f"{len(ch['series'])} series, expected {want_series} from the data block"),
           _chk("series_points_com", bool(ch["series"]) and all(s["points"] == want_pts for s in ch["series"]), f"points per series {[s['points'] for s in ch['series']]}, expected {want_pts}")]
    if args.get("title"):
        out.append(_chk("chart_title_com", ch["title"] == args["title"], f"title {ch['title']!r}, wanted {args['title']!r}"))
    if not info.get("ok"):
        out.append(_chk("saved_file_readable", False, "the saved .xlsx could not be re-read"))
        return out
    n_before = data.get("charts_in_book_before", 0)
    out.append(_chk("chart_in_saved_file", len(info["charts"]) == n_before + 1, f"xl/charts has {len(info['charts'])} chart part(s), expected {n_before + 1}"))
    if info["charts"]:
        last = info["charts"][-1]
        kind_ok = CHART_TYPES[ct][1] in last["kinds"] and (last["bar_dir"] in (None, "col" if ct == "column" else "bar") or ct not in ("column", "bar"))
        out.append(_chk("chart_kind_file", kind_ok, f"saved chart plot types {last['kinds']} barDir={last['bar_dir']}, wanted {CHART_TYPES[ct][1]}"))
        out.append(_chk("chart_series_file", last["series"] == want_series, f"saved chart has {last['series']} series, expected {want_series}"))
        if args.get("title"):
            out.append(_chk("chart_title_file", last["title"] == _norm(args["title"]), f"saved chart title {last['title']!r}"))
    return out


def pivot_expected(rows: list[list[Any]], row_fields: list[str], values: list[dict[str, str]]) -> dict[str, Any]:
    """Aggregates computed in Python from the source block (header + data) to compare with the pivot Excel built."""
    header = [_lab(h) for h in rows[0]]
    data = rows[1:]

    def agg(fn: str, items: list[Any]) -> float:
        nums = [x for x in (_num(i) for i in items) if x is not None]
        if fn == "count":
            return float(len([i for i in items if i not in (None, "")]))
        if not nums:
            return 0.0
        return {"sum": sum(nums), "average": sum(nums) / len(nums), "max": max(nums), "min": min(nums)}[fn]

    def col(name: str) -> int:
        return header.index(name)

    def calc(subset: list[list[Any]]) -> list[float]:
        return [agg(v["function"], [r[col(v["field"])] for r in subset]) for v in values]

    out: dict[str, Any] = {"grand": calc(data), "groups": {}}
    if len(row_fields) == 1:
        c = col(row_fields[0])
        keys: dict[str, list[list[Any]]] = {}
        for r in data:
            keys.setdefault(_lab(r[c]), []).append(r)
        out["groups"] = {k: calc(v) for k, v in keys.items()}
    return out


def pivot_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any]) -> list[Check]:
    out = [_chk("pivot_exists_com", data["pivot_name"] in data["pivots_after"], f"pivot tables in the workbook after: {data['pivots_after']}"),
           _chk("pivot_data_fields_com", [d["field"] for d in data["data_fields"]] == [v["field"] for v in args["values"]],
                f"data fields {data['data_fields']}"),
           _chk("pivot_has_result", len(data["result"]) >= 2, f"{len(data['result'])} row(s) in the pivot range {data['pivot_range']}")]
    exp = pivot_expected(data["source_values"], args.get("rows", []), args["values"])
    nv = len(args["values"])
    result = data["result"]
    if not args.get("columns"):
        body = [r for r in result[1:] if r and _lab(r[0]) not in ("",)]
        grand = next((r for r in reversed(body) if _lab(r[0]).lower() in ("grand total", "total general", "tổng cộng")), None)
        if not args.get("rows"):
            grand = result[-1]
            vals = [_num(x) for x in grand[-nv:]] if grand else []
        else:
            vals = [_num(x) for x in grand[1:1 + nv]] if grand else []
        out.append(_chk("pivot_grand_total", bool(vals) and all(v is not None and abs(v - e) < 1e-6 for v, e in zip(vals, exp["grand"])) and len(vals) == nv,
                        f"pivot grand total {vals}, computed from the source {exp['grand']}"))
        if exp["groups"]:
            seen = {_lab(r[0]): [_num(x) for x in r[1:1 + nv]] for r in body if _lab(r[0]).lower() not in ("grand total", "total general", "tổng cộng")}
            bad = [k for k, e in exp["groups"].items() if k not in seen or any(v is None or abs(v - x) > 1e-6 for v, x in zip(seen[k], e))]
            out.append(_chk("pivot_groups", not bad and len(seen) == len(exp["groups"]), f"{len(seen)} pivot row labels vs {len(exp['groups'])} groups in the source; wrong/missing: {bad[:5]}"))
    else:
        out.append(_chk("pivot_cells_not_compared", True, "column fields used: only existence, data fields and size were verified (cell values not compared)"))
    if not info.get("ok"):
        out.append(_chk("saved_file_readable", False, "the saved .xlsx could not be re-read"))
        return out
    out.append(_chk("pivot_in_saved_file", data["pivot_name"] in info["pivot_names"], f"pivotTableDefinition names in the saved package: {info['pivot_names']}"))
    return out


def pivot_part_info(path: str | Path) -> dict[str, Any]:
    z = _read_zip(path)
    if z is None:
        return {"ok": False, "pivot_names": []}
    with z:
        names = []
        for n in z.namelist():
            if re.fullmatch(r"xl/pivotTables/pivotTable\d+\.xml", n):
                root = _xml(z, n)
                if root is not None:
                    names.append(root.get("name", ""))
        return {"ok": True, "pivot_names": names}


# ============================================================================================ PowerPoint: pure reader / checks
def pptx_read_zip(path: str | Path) -> dict[str, Any]:
    """Slides in order with title, body paragraphs (bullets) and notes, straight from the saved package."""
    z = _read_zip(path)
    if z is None:
        return {"ok": False, "slides": []}
    with z:
        pres = _xml(z, "ppt/presentation.xml")
        rels = _xml(z, "ppt/_rels/presentation.xml.rels")
        if pres is None or rels is None:
            return {"ok": False, "slides": []}
        target = {r.get("Id"): r.get("Target", "") for r in rels}
        slides = []
        for sid in pres.iter(q(P, "sldId")):
            t = target.get(sid.get(q(R, "id")), "")
            part = t.lstrip("/") if t.startswith("/") else "ppt/" + t
            root = _xml(z, part)
            if root is None:
                continue
            title, body = "", []
            for sp in root.iter(q(P, "sp")):
                ph = sp.find(f"{q(P, 'nvSpPr')}/{q(P, 'nvPr')}/{q(P, 'ph')}")
                paras = [_para_text(p) for p in sp.iter(q(A, "p"))]
                paras = [x for x in paras if x.strip()]
                if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                    title = " ".join(paras)
                elif paras:
                    body.extend(paras)
            notes = ""
            relf = f"{part.rsplit('/', 1)[0]}/_rels/{part.rsplit('/', 1)[1]}.rels"
            srels = _xml(z, relf)
            if srels is not None:
                for r in srels:
                    if r.get("Type", "").endswith("/notesSlide"):
                        nt = r.get("Target", "")
                        npart = nt.lstrip("/") if nt.startswith("/") else _join(part, nt)
                        nroot = _xml(z, npart)
                        if nroot is not None:
                            for sp in nroot.iter(q(P, "sp")):
                                ph = sp.find(f"{q(P, 'nvSpPr')}/{q(P, 'nvPr')}/{q(P, 'ph')}")
                                if ph is not None and ph.get("type") == "body":
                                    notes = "\n".join(x for x in (_para_text(p) for p in sp.iter(q(A, "p"))) if x.strip())
            slides.append({"title": title, "bullets": body, "notes": notes})
        return {"ok": True, "slides": slides}


def _join(part: str, rel: str) -> str:
    base = part.rsplit("/", 1)[0].split("/")
    for seg in rel.split("/"):
        if seg == "..":
            base.pop()
        elif seg and seg != ".":
            base.append(seg)
    return "/".join(base)


def _slides_equal(com: list[dict[str, Any]], zipped: list[dict[str, Any]]) -> tuple[bool, str]:
    if len(com) != len(zipped):
        return False, f"COM sees {len(com)} slide(s), the saved file has {len(zipped)}"
    for i, (a, b) in enumerate(zip(com, zipped), 1):
        if _norm(a["title"]) != _norm(b["title"]) or [_norm(x) for x in a["bullets"]] != [_norm(x) for x in b["bullets"]] or _norm(a["notes"]) != _norm(b["notes"]):
            return False, f"slide {i} differs: COM {a!r} vs file {b!r}"
    return True, f"{len(com)} slide(s) identical in COM and in the saved file"


def ppt_create_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any], verdict: dict[str, Any] | None) -> list[Check]:
    want = [{"title": args["title"], "bullets": [args["subtitle"]] if args.get("subtitle") else [], "notes": ""}]
    for s in args.get("slides", []):
        want.append({"title": s["title"], "bullets": list(s.get("bullets", [])), "notes": s.get("notes", "")})
    out: list[Check] = []
    if not info.get("ok"):
        return [_chk("saved_file_readable", False, "the saved .pptx could not be re-read as zip+XML")]
    got = info["slides"]
    out.append(_chk("slide_count_file", len(got) == len(want), f"{len(got)} slide(s) in the saved file, wanted {len(want)}"))
    bad = [i + 1 for i, (w, g) in enumerate(zip(want, got))
           if _norm(w["title"]) != _norm(g["title"]) or [_norm(x) for x in w["bullets"]] != [_norm(x) for x in g["bullets"]] or _norm(w["notes"]) != _norm(g["notes"])]
    out.append(_chk("slide_content_file", not bad and len(got) == len(want), f"slides whose title/bullets/notes differ from the request: {bad}"))
    ok, detail = _slides_equal(data["slides"], got)
    out.append(_chk("com_matches_file", ok, detail))
    if verdict is not None:
        out.append(_chk("office_verify_pptx", verdict["verdict"] != "FAIL", f"office.verify verdict {verdict['verdict']}: counts {verdict.get('counts')}"))
    return out


def ppt_edit_checks(data: dict[str, Any], args: dict[str, Any], info: dict[str, Any], verdict: dict[str, Any] | None) -> list[Check]:
    out: list[Check] = []
    if not info.get("ok"):
        return [_chk("saved_file_readable", False, "the saved .pptx could not be re-read as zip+XML")]
    got = info["slides"]
    before = data["slides_before"]
    add = args.get("add_slide")
    want_n = len(before) + (1 if add else 0)
    out.append(_chk("slide_count_file", len(got) == want_n, f"{len(got)} slide(s) in the saved file, expected {want_n}"))
    alltext = "\n".join(f"{s['title']}\n" + "\n".join(s["bullets"]) for s in got)
    for old, new in (args.get("replace") or {}).items():
        occ = data["replace_counts"].get(old, 0)
        out.append(_chk(f"replace_found_{old[:20]}", occ > 0, f"{occ} occurrence(s) of {old!r} found on slides"))
        out.append(_chk(f"replace_new_in_file_{old[:20]}", new in alltext, f"{new!r} present in the saved slides"))
        if old not in new:
            was = "\n".join(f"{x['title']}\n" + "\n".join(x["bullets"]) for x in before).count(old)
            now = alltext.count(old)
            out.append(_chk(f"replace_old_gone_{old[:20]}", now <= was - occ, f"{old!r}: {was} occurrence(s) before, {now} in the saved slides, {occ} replaced"))
    if add:
        idx = (add.get("position") or len(before) + 1) - 1
        okk = 0 <= idx < len(got) and _norm(got[idx]["title"]) == _norm(add["title"]) and [_norm(x) for x in got[idx]["bullets"]] == [_norm(x) for x in add.get("bullets", [])] \
            and _norm(got[idx]["notes"]) == _norm(add.get("notes", ""))
        out.append(_chk("added_slide_file", okk, f"slide {idx + 1} in the saved file: {got[idx] if 0 <= idx < len(got) else None!r}"))
    ns = args.get("notes")
    if ns:
        idx = ns["slide"] - 1
        out.append(_chk("notes_file", 0 <= idx < len(got) and _norm(got[idx]["notes"]) == _norm(ns["text"]), f"notes of slide {ns['slide']} in the saved file: {got[idx]['notes'] if 0 <= idx < len(got) else None!r}"))
    ok, detail = _slides_equal(data["slides_after"], got)
    out.append(_chk("com_matches_file", ok, detail))
    if verdict is not None:
        out.append(_chk("office_verify_pptx", verdict["verdict"] != "FAIL", f"office.verify verdict {verdict['verdict']}: counts {verdict.get('counts')}"))
    return out


# ============================================================================================ COM mixin
class OfficeExtra:
    """Mixed into `Office`: needs _app, _workbook, _sheet, _document, _finish, _same."""

    # ------------------------------------------------------------------ Word
    def word_toc(self, path: str, action: str, levels: int, location: str) -> dict[str, Any]:
        word, doc, started, opened = self._document(path)  # type: ignore[attr-defined]
        try:
            before = int(doc.TablesOfContents.Count)
            if before == 0:
                if action == "update":
                    raise ToolError(ErrorCode.NOT_FOUND, "the document has no table of contents to update (use action=insert)")
                pos = 0 if location == "start" else int(doc.Content.End) - 1
                doc.TablesOfContents.Add(doc.Range(pos, pos), True, 1, levels)
            for i in range(1, int(doc.TablesOfContents.Count) + 1):
                doc.TablesOfContents.Item(i).Update()
            doc.Fields.Update()
            spans = [(int(doc.TablesOfContents.Item(i).Range.Start), int(doc.TablesOfContents.Item(i).Range.End)) for i in range(1, int(doc.TablesOfContents.Count) + 1)]
            headings: list[str] = []
            n = int(doc.Paragraphs.Count)
            if n > MAX_PARAS:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{n} paragraphs; over {MAX_PARAS} the heading scan is refused (work on a smaller document)")
            for para in doc.Paragraphs:
                lvl = int(para.OutlineLevel)
                if lvl > levels:
                    continue
                st = int(para.Range.Start)
                if any(a <= st < b for a, b in spans):
                    continue
                text = str(para.Range.Text).rstrip("\r\x07\x0b\n").strip()
                if text:
                    headings.append(text)
            entries: list[str] = []
            pages: list[str] = []
            if spans:
                for para in doc.TablesOfContents.Item(1).Range.Paragraphs:
                    t = str(para.Range.Text).rstrip("\r\x07\x0b\n")
                    parts = t.split("\t")
                    if _norm(parts[0]):
                        entries.append(_norm(parts[0]))
                        pages.append(parts[-1].strip() if len(parts) > 1 else "")
            return {"document": doc.FullName, "action": action, "toc_count": int(doc.TablesOfContents.Count), "levels": levels,
                    "headings": headings, "toc_entries": entries, "toc_pages": pages, "inserted": before == 0, "saved": True}
        finally:
            self._finish(word, started, doc, opened, save=True)  # type: ignore[attr-defined]

    @staticmethod
    def _revisions(doc: Any) -> list[dict[str, Any]]:
        names = {1: "insert", 2: "delete", 3: "property", 4: "paragraph_number", 5: "display_field", 6: "reconcile", 7: "conflict", 8: "style", 9: "replace",
                 10: "paragraph_property", 11: "table_property", 12: "section_property", 13: "style_definition", 14: "moved_from", 15: "moved_to", 16: "cell_insertion",
                 17: "cell_deletion", 18: "cell_merge"}
        out = []
        for rev in doc.Revisions:
            out.append({"type": names.get(int(rev.Type), str(int(rev.Type))), "author": str(rev.Author), "text": str(rev.Range.Text)[:200]})
        return out

    @staticmethod
    def _comments(doc: Any) -> list[dict[str, Any]]:
        out = []
        for c in doc.Comments:
            try:
                scope = str(c.Scope.Text)[:100]
            except Exception:
                scope = ""
            out.append({"author": str(c.Author), "text": str(c.Range.Text), "scope": scope})
        return out

    def word_review(self, path: str, track: bool | None, replace: dict[str, str], comment: dict[str, Any] | None, resolve: dict[str, Any] | None) -> dict[str, Any]:
        word, doc, started, opened = self._document(path)  # type: ignore[attr-defined]
        changed = False
        try:
            track_before = bool(doc.TrackRevisions)
            revs_before = len(self._revisions(doc))
            if track is not None and bool(track) != track_before:
                doc.TrackRevisions = bool(track)
                changed = True
            during = bool(doc.TrackRevisions)
            occurrences = 0
            new_present = True
            for old, new in replace.items():
                occurrences += str(doc.Content.Text).count(old)
                find = doc.Content.Find
                find.ClearFormatting()
                find.Replacement.ClearFormatting()
                find.Execute(old, True, False, False, False, False, True, 0, False, new, 2)
                changed = True
            if replace:
                text = str(doc.Content.Text)
                new_present = all(new in text for new in replace.values())
            if comment:
                rng = doc.Content
                anchor = comment.get("anchor")
                if anchor:
                    if not rng.Find.Execute(anchor):
                        raise ToolError(ErrorCode.NOT_FOUND, f"comment anchor text {anchor!r} not found in the document")
                else:
                    rng = doc.Paragraphs.Item(1).Range
                c = doc.Comments.Add(rng, comment["text"])
                if comment.get("author"):
                    c.Author = comment["author"]
                changed = True
            if resolve:
                if resolve["action"] == "accept":
                    doc.Revisions.AcceptAll()
                else:
                    doc.Revisions.RejectAll()
                changed = True
            revs = self._revisions(doc)
            return {"document": doc.FullName, "track_revisions_before": track_before, "track_revisions_after": bool(doc.TrackRevisions),
                    "track_during_edit": during, "replace_occurrences": occurrences, "replace_new_present": new_present,
                    "revisions_before_count": revs_before, "revisions_after_count": len(revs), "revisions": revs, "comments": self._comments(doc),
                    "saved": changed}
        finally:
            self._finish(word, started, doc, opened, save=changed)  # type: ignore[attr-defined]

    @staticmethod
    def _set_hf(hf: Any, text: str | None, page: bool | None, align: int) -> None:
        if text is None and page is None:
            return
        rng = hf.Range
        if page is None or page is False:
            rng.Text = text or ""
            return
        rng.Text = (text + " ") if text else ""
        r = hf.Range
        pos = int(r.End) - 1
        r.SetRange(pos, pos)
        hf.Range.Fields.Add(r, 33)  # wdFieldPage
        hf.Range.ParagraphFormat.Alignment = align

    def word_layout(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        word, doc, started, opened = self._document(path)  # type: ignore[attr-defined]
        try:
            align = {"left": 0, "center": 1, "right": 2}[args.get("page_number_alignment", "center")]
            pos = args.get("page_number_position", "footer")
            for sec in doc.Sections:
                ps = sec.PageSetup
                if args.get("orientation"):
                    ps.Orientation = 1 if args["orientation"] == "landscape" else 0
                for k, v in (args.get("margins_cm") or {}).items():
                    setattr(ps, {"top": "TopMargin", "bottom": "BottomMargin", "left": "LeftMargin", "right": "RightMargin"}[k], float(v) * CM_TO_PT)
                header, footer = sec.Headers.Item(1), sec.Footers.Item(1)
                pn = args.get("page_numbers")
                self._set_hf(header, args.get("header_text"), pn if pos == "header" else None, align)
                self._set_hf(footer, args.get("footer_text"), pn if pos == "footer" else None, align)
            doc.Fields.Update()
            secs = []
            for sec in doc.Sections:
                ps = sec.PageSetup

                def pages(hf: Any) -> int:
                    return sum(1 for f in hf.Range.Fields if int(f.Type) == 33)

                secs.append({"orientation": "landscape" if int(ps.Orientation) == 1 else "portrait",
                             "page_width_cm": round(float(ps.PageWidth) / CM_TO_PT, 2), "page_height_cm": round(float(ps.PageHeight) / CM_TO_PT, 2),
                             "margins_cm": {"top": round(float(ps.TopMargin) / CM_TO_PT, 2), "bottom": round(float(ps.BottomMargin) / CM_TO_PT, 2),
                                            "left": round(float(ps.LeftMargin) / CM_TO_PT, 2), "right": round(float(ps.RightMargin) / CM_TO_PT, 2)},
                             "header": str(sec.Headers.Item(1).Range.Text).rstrip("\r\x07"), "footer": str(sec.Footers.Item(1).Range.Text).rstrip("\r\x07"),
                             "header_page_fields": pages(sec.Headers.Item(1)), "footer_page_fields": pages(sec.Footers.Item(1))})
            return {"document": doc.FullName, "sections": secs, "saved": True}
        finally:
            self._finish(word, started, doc, opened, save=True)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ Excel
    @staticmethod
    def _grid(v: Any) -> list[list[Any]]:
        if not isinstance(v, tuple):
            return [[v]]
        return [[("" if x is None else x) for x in row] for row in v]

    def excel_pivot(self, path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        excel, book, started, opened = self._workbook(path)  # type: ignore[attr-defined]
        save = False
        try:
            def pivot_list() -> list[str]:
                return [f"{ws.Name}!{pt.Name}" for ws in book.Worksheets for pt in ws.PivotTables()]

            if args.get("action", "create") == "read":
                tables = []
                for ws in book.Worksheets:
                    for pt in ws.PivotTables():
                        tables.append({"sheet": ws.Name, "name": pt.Name, "range": pt.TableRange1.Address.replace("$", ""),
                                       "result": self._grid(pt.TableRange1.Value), "data_fields": [str(d.Name) for d in pt.DataFields]})
                return {"workbook": book.FullName, "pivot_tables": tables, "saved": False}
            ws = self._sheet(book, args.get("sheet"))  # type: ignore[attr-defined]
            src = ws.Range(args["source_range"])
            source_values = self._grid(src.Value)
            header = [_lab(h) for h in source_values[0]]
            if len(set(header)) != len(header) or "" in header:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"the first row of {args['source_range']} must be unique non-empty column names, got {header}")
            for f in args.get("rows", []) + args.get("columns", []) + [v["field"] for v in args["values"]]:
                if f not in header:
                    raise ToolError(ErrorCode.INVALID_ARGUMENT, f"field {f!r} is not a column of the source ({header})")
            dest_name = args.get("dest_sheet", "Pivot")
            if dest_name.lower() == ws.Name.lower():
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "dest_sheet must differ from the source sheet")
            name = args.get("name", "PivotTable1")
            if any(p.split("!", 1)[1] == name for p in pivot_list()):
                raise ToolError(ErrorCode.CONFLICT, f"a pivot table named {name!r} already exists in the workbook")
            dest = self._sheet(book, dest_name)  # type: ignore[attr-defined]
            save = True
            cache = book.PivotCaches().Create(1, src)  # xlDatabase
            pt = cache.CreatePivotTable(dest.Range(args.get("dest_cell", "A3")), name)
            for f in args.get("rows", []):
                pt.PivotFields(f).Orientation = 1  # xlRowField
            for f in args.get("columns", []):
                pt.PivotFields(f).Orientation = 2  # xlColumnField
            for v in args["values"]:
                fn = v.get("function", "sum")
                pt.AddDataField(pt.PivotFields(v["field"]), f"{fn.title()} of {v['field']}", PIVOT_FUNCS[fn])
            pt.RefreshTable()
            result = self._grid(pt.TableRange1.Value)
            return {"workbook": book.FullName, "sheet": dest.Name, "pivot_name": pt.Name, "pivot_range": pt.TableRange1.Address.replace("$", ""),
                    "source_values": source_values, "result": result, "pivots_after": [p.split("!", 1)[1] for p in pivot_list()],
                    "data_fields": [{"name": str(d.Name), "field": str(d.SourceName)} for d in pt.DataFields], "saved": True}
        finally:
            self._finish(excel, started, book, opened, save=save)  # type: ignore[attr-defined]

    def excel_chart(self, path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        excel, book, started, opened = self._workbook(path)  # type: ignore[attr-defined]
        try:
            ws = self._sheet(book, args.get("sheet"))  # type: ignore[attr-defined]
            src = ws.Range(args["source_range"])
            source_values = self._grid(src.Value)
            if len(source_values) < 2:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, "the source range needs a header row and at least one data row")
            n_book_before = sum(int(s.ChartObjects().Count) for s in book.Worksheets)
            before = int(ws.ChartObjects().Count)
            ct = args.get("chart_type", "column")
            anchor = ws.Range(args["dest_cell"]) if args.get("dest_cell") else ws.Cells(src.Row, src.Column + src.Columns.Count + 1)
            shape = ws.Shapes.AddChart2(-1, CHART_TYPES[ct][0], anchor.Left, anchor.Top, 420, 260)
            chart = shape.Chart
            chart.SetSourceData(src)
            if args.get("title"):
                chart.HasTitle = True
                chart.ChartTitle.Text = args["title"]
            series = []
            for i in range(1, int(chart.SeriesCollection().Count) + 1):
                s = chart.SeriesCollection(i)
                series.append({"name": str(s.Name), "formula": str(s.Formula), "points": len(s.Values)})
            return {"workbook": book.FullName, "sheet": ws.Name, "source_values": source_values, "charts_on_sheet_before": before,
                    "charts_on_sheet_after": int(ws.ChartObjects().Count), "charts_in_book_before": n_book_before,
                    "chart": {"name": str(shape.Name), "chart_type": int(chart.ChartType), "title": str(chart.ChartTitle.Text) if chart.HasTitle else "", "series": series},
                    "saved": True}
        finally:
            self._finish(excel, started, book, opened, save=True)  # type: ignore[attr-defined]

    def excel_format(self, path: str | None, args: dict[str, Any]) -> dict[str, Any]:
        excel, book, started, opened = self._workbook(path)  # type: ignore[attr-defined]
        try:
            ws = self._sheet(book, args.get("sheet"))  # type: ignore[attr-defined]
            rng = ws.Range(args["range"])
            if args.get("number_format") is not None:
                rng.NumberFormat = args["number_format"]
            conds = args.get("conditional") or []
            before_n = int(rng.FormatConditions.Count)
            if conds and args.get("replace_existing", True):
                rng.FormatConditions.Delete()
                before_n = 0
            for c in conds:
                if c["kind"] == "cell_value":
                    op = XL_OPS[c["operator"]]
                    f2 = _formula_arg(c["value2"]) if c["operator"] in ("between", "not_between") else None
                    fc = rng.FormatConditions.Add(1, op, _formula_arg(c["value"]), f2) if f2 else rng.FormatConditions.Add(1, op, _formula_arg(c["value"]))
                else:
                    fc = rng.FormatConditions.Add(2, None, c["formula"])
                if c.get("fill_color"):
                    fc.Interior.Color = _bgr(c["fill_color"])
                if c.get("font_color"):
                    fc.Font.Color = _bgr(c["font_color"])
            dv = args.get("validation")
            if dv:
                v = rng.Validation
                v.Delete()
                t = DV_TYPES[dv["type"]]
                if dv["type"] == "list":
                    v.Add(t, 1, 1, dv["source"])
                elif dv["type"] == "custom":
                    v.Add(t, 1, 1, dv["formula"])
                else:
                    op = XL_OPS[dv.get("operator", "between")]
                    lo = str(dv.get("min", dv.get("value")))
                    if dv.get("operator", "between") in ("between", "not_between"):
                        v.Add(t, 1, op, lo, str(dv["max"]))
                    else:
                        v.Add(t, 1, op, lo)
                if dv.get("error_message"):
                    v.ErrorMessage = dv["error_message"]
                    v.ShowError = True
                if dv.get("prompt"):
                    v.InputMessage = dv["prompt"]
                    v.ShowInput = True
            first = rng.Cells(1, 1)
            got_cf = []
            for i in range(1, int(rng.FormatConditions.Count) + 1):
                fc = rng.FormatConditions.Item(i)
                rec: dict[str, Any] = {"type": int(fc.Type), "operator": None, "formula1": None, "formula2": None, "fill": None}
                try:
                    rec["operator"] = int(fc.Operator)
                except Exception:
                    pass
                try:
                    rec["formula1"] = str(fc.Formula1)
                    rec["formula2"] = str(fc.Formula2)
                except Exception:
                    pass
                try:
                    rec["fill"] = int(fc.Interior.Color)
                except Exception:
                    pass
                got_cf.append(rec)
            val_after = None
            if dv:
                try:
                    vv = first.Validation
                    val_after = {"type": int(vv.Type), "operator": int(vv.Operator) if int(vv.Type) not in (3, 7) else None, "formula1": str(vv.Formula1),
                                 "formula2": str(vv.Formula2) if int(vv.Type) not in (3, 7) else None}
                except Exception:
                    val_after = {}
            return {"workbook": book.FullName, "sheet": ws.Name, "range": rng.Address.replace("$", ""), "number_format_after": str(first.NumberFormat),
                    "display_first_cell": str(first.Text), "conditional_before": before_n, "conditional_after": got_cf, "validation_after": val_after, "saved": True}
        finally:
            self._finish(excel, started, book, opened, save=True)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ PowerPoint
    def _deck(self, path: str, readonly: bool) -> tuple[Any, Any, bool, bool]:
        pp, started = self._app("PowerPoint.Application")  # type: ignore[attr-defined]
        target = absolute_path(path)
        for pres in pp.Presentations:
            if self._same(pres.FullName, str(target)):  # type: ignore[attr-defined]
                return pp, pres, started, False
        if not target.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"{target} does not exist")
        return pp, pp.Presentations.Open(str(target), -1 if readonly else 0, 0, 0), started, True

    @staticmethod
    def _close_deck(pp: Any, pres: Any, started: bool, opened: bool) -> None:
        try:
            if opened:
                pres.Close()
        finally:
            if started:
                try:
                    pp.Quit()
                except Exception:
                    pass

    @staticmethod
    def _read_slides(pres: Any) -> list[dict[str, Any]]:
        out = []
        for slide in pres.Slides:
            title, body = "", []
            title_shape = slide.Shapes.Title if slide.Shapes.HasTitle else None
            if title_shape is not None:
                title = str(title_shape.TextFrame.TextRange.Text).replace("\r", " ").strip()
            for shape in slide.Shapes:
                if title_shape is not None and shape.Name == title_shape.Name:
                    continue
                if shape.HasTextFrame and shape.TextFrame.HasText:
                    body.extend(x for x in str(shape.TextFrame.TextRange.Text).replace("\x0b", "\n").split("\r") if x.strip())
            notes = ""
            try:
                for ns in slide.NotesPage.Shapes:
                    if ns.Type == 14 and int(ns.PlaceholderFormat.Type) == 2 and ns.HasTextFrame:  # msoPlaceholder / ppPlaceholderBody
                        notes = "\n".join(x for x in str(ns.TextFrame.TextRange.Text).split("\r") if x.strip())
            except Exception:
                pass
            out.append({"index": int(slide.SlideIndex), "title": title, "bullets": body, "notes": notes, "layout": str(slide.CustomLayout.Name)})
        return out

    @staticmethod
    def _fill_slide(slide: Any, title: str, bullets: list[str], notes: str | None) -> None:
        slide.Shapes.Title.TextFrame.TextRange.Text = title
        if bullets:
            slide.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = "\r".join(bullets)
        if notes:
            slide.NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = notes

    def ppt_create(self, path: str, title: str, subtitle: str | None, slides: list[dict[str, Any]], overwrite: bool) -> dict[str, Any]:
        target = absolute_path(path)
        if target.exists() and not overwrite:
            raise ToolError(ErrorCode.CONFLICT, f"{target} already exists (pass overwrite=true to replace it)")
        target.parent.mkdir(parents=True, exist_ok=True)
        pp, started = self._app("PowerPoint.Application")  # type: ignore[attr-defined]
        pres = pp.Presentations.Add(0)
        try:
            layouts = pres.SlideMaster.CustomLayouts
            first = pres.Slides.AddSlide(1, layouts.Item(1))
            first.Shapes.Title.TextFrame.TextRange.Text = title
            if subtitle:
                first.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = subtitle
            else:
                first.Shapes.Placeholders.Item(2).Delete()
            for s in slides:
                slide = pres.Slides.AddSlide(int(pres.Slides.Count) + 1, layouts.Item(2))
                self._fill_slide(slide, s["title"], list(s.get("bullets", [])), s.get("notes"))
            pres.SaveAs(str(target), 24)  # ppSaveAsOpenXMLPresentation
            result = self._read_slides(pres)
        finally:
            try:
                pres.Close()
            finally:
                if started:
                    try:
                        pp.Quit()
                    except Exception:
                        pass
        return {"presentation": str(target), "theme": "default (Office theme of the installed PowerPoint)", "slides": result, "saved": True}

    def ppt_read(self, path: str) -> dict[str, Any]:
        pp, pres, started, opened = self._deck(path, True)
        try:
            return {"presentation": pres.FullName, "slide_count": int(pres.Slides.Count), "slides": self._read_slides(pres)}
        finally:
            self._close_deck(pp, pres, started, opened)

    def ppt_edit(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        pp, pres, started, opened = self._deck(path, False)
        try:
            before = self._read_slides(pres)
            counts: dict[str, int] = {}
            for old, new in (args.get("replace") or {}).items():
                total = 0
                for slide in pres.Slides:
                    for shape in slide.Shapes:
                        if not (shape.HasTextFrame and shape.TextFrame.HasText):
                            continue
                        tr = shape.TextFrame.TextRange
                        n = str(tr.Text).count(old)
                        for _ in range(n):
                            if tr.Replace(old, new) is None:
                                break
                            total += 1
                counts[old] = total
            add = args.get("add_slide")
            if add:
                pos = int(add.get("position") or int(pres.Slides.Count) + 1)
                if not 1 <= pos <= int(pres.Slides.Count) + 1:
                    raise ToolError(ErrorCode.INVALID_ARGUMENT, f"position {pos} out of range 1..{int(pres.Slides.Count) + 1}")
                layout = pres.SlideMaster.CustomLayouts.Item(2 if add.get("bullets") else 6)  # Title and Content / Title Only
                slide = pres.Slides.AddSlide(pos, layout)
                self._fill_slide(slide, add["title"], list(add.get("bullets", [])), add.get("notes"))
            ns = args.get("notes")
            if ns:
                if not 1 <= ns["slide"] <= int(pres.Slides.Count):
                    raise ToolError(ErrorCode.INVALID_ARGUMENT, f"notes: slide {ns['slide']} does not exist ({int(pres.Slides.Count)} slides)")
                pres.Slides.Item(ns["slide"]).NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = ns["text"]
            pres.Save()
            return {"presentation": pres.FullName, "slides_before": before, "slides_after": self._read_slides(pres), "replace_counts": counts, "saved": True}
        finally:
            self._close_deck(pp, pres, started, opened)


# ============================================================================================ handlers
def _verify_file(path: str, **opts: Any) -> dict[str, Any] | None:
    try:
        from lga_worker.office import verify

        return verify.verify_file(path, **opts)
    except Exception:  # noqa: BLE001 - a verifier crash must not look like a pass
        return None


def _outcome(data: dict[str, Any], checks: list[Check], observation: str, path: str | None = None) -> ToolOutcome:
    data["verification"] = [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks]
    failed = [c for c in checks if not c.passed]
    obs = observation + (f"\nVERIFICATION FAILED: {'; '.join(f'{c.name}: {c.detail}' for c in failed)}" if failed else f"\nVerified: {len(checks)} check(s) passed (COM read-back and saved file).")
    return ToolOutcome(data=data, backend=BACKEND, checks=checks, observation=obs, artifacts=[{"kind": "file", "path": path}] if path else [])


def register_extra(registry: ToolRegistry, get_office: Callable[..., Any], run: Callable[..., Any]) -> None:
    async def call(ctx: ToolContext, method: str, *a: Any) -> Any:
        office = await get_office(ctx)
        return await run(ctx, getattr(office, method), *a)

    @registry.handler("office.word_toc")
    async def word_toc(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await call(ctx, "word_toc", args["path"], args.get("action", "insert"), int(args.get("levels", 3)), args.get("location", "start"))
        verdict = await asyncio.to_thread(_verify_file, data["document"], expect_toc=True)
        checks = toc_checks(data, args, verdict)
        return _outcome(data, checks, f"{data['document']}: TOC with {len(data['toc_entries'])} entries for {len(data['headings'])} headings ({'inserted' if data['inserted'] else 'updated'}).", data["document"])

    @registry.handler("office.word_review")
    async def word_review(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await call(ctx, "word_review", args["path"], args.get("track"), args.get("replace") or {}, args.get("comment"), args.get("resolve"))
        info = await asyncio.to_thread(docx_review_info, data["document"])
        checks = review_checks(data, args, info)
        return _outcome(data, checks, f"{data['document']}: tracking={'on' if data['track_revisions_after'] else 'off'}, {data['revisions_after_count']} revision(s), "
                        f"{len(data['comments'])} comment(s).", data["document"])

    @registry.handler("office.word_layout")
    async def word_layout(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if not any(args.get(k) is not None for k in ("page_numbers", "header_text", "footer_text", "margins_cm", "orientation")):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give at least one of page_numbers, header_text, footer_text, margins_cm, orientation")
        data = await call(ctx, "word_layout", args["path"], {k: v for k, v in args.items() if k != "path"})
        info = await asyncio.to_thread(docx_layout_info, data["document"])
        checks = layout_checks(data, args, info)
        return _outcome(data, checks, f"{data['document']}: layout set on {len(data['sections'])} section(s).", data["document"])

    @registry.handler("office.excel_pivot")
    async def excel_pivot(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if args.get("action", "create") == "read":
            data = await call(ctx, "excel_pivot", args.get("path"), {"action": "read"})
            return ToolOutcome(data=data, backend=BACKEND, observation=f"{len(data['pivot_tables'])} pivot table(s): " + "; ".join(f"{t['sheet']}!{t['name']} {t['range']}" for t in data["pivot_tables"]))
        for need in ("source_range", "values"):
            if not args.get(need):
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{need} is required to create a pivot table")
        args = {**args, "values": [{"field": v, "function": "sum"} if isinstance(v, str) else v for v in args["values"]]}
        data = await call(ctx, "excel_pivot", args.get("path"), args)
        info = await asyncio.to_thread(pivot_part_info, data["workbook"])
        checks = pivot_checks(data, args, info)
        table = "\n".join(" | ".join(str(x) for x in r) for r in data["result"][:25])
        return _outcome(data, checks, f"Pivot {data['pivot_name']} on [{data['sheet']}] {data['pivot_range']}:\n{table}", data["workbook"])

    @registry.handler("office.excel_chart")
    async def excel_chart(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await call(ctx, "excel_chart", args.get("path"), args)
        info = await asyncio.to_thread(chart_part_info, data["workbook"])
        checks = chart_checks(data, args, info)
        return _outcome(data, checks, f"{args.get('chart_type', 'column')} chart with {len(data['chart']['series'])} series on [{data['sheet']}] from {args['source_range']}.", data["workbook"])

    @registry.handler("office.excel_format")
    async def excel_format(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if not any(args.get(k) is not None for k in ("number_format", "conditional", "validation")):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give at least one of number_format, conditional, validation")
        data = await call(ctx, "excel_format", args.get("path"), args)
        info = await asyncio.to_thread(xlsx_format_info, data["workbook"], data["sheet"])
        checks = format_checks(data, args, info)
        return _outcome(data, checks, f"[{data['sheet']}] {data['range']}: number format {data['number_format_after']!r}, {len(data['conditional_after'])} conditional rule(s), "
                        f"validation {'set' if data['validation_after'] else 'none'}.", data["workbook"])

    @registry.handler("office.ppt_create")
    async def ppt_create(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await call(ctx, "ppt_create", args["path"], args["title"], args.get("subtitle"), args.get("slides", []), bool(args.get("overwrite", False)))
        info = await asyncio.to_thread(pptx_read_zip, data["presentation"])
        verdict = await asyncio.to_thread(_verify_file, data["presentation"], expected_slides=len(data["slides"]))
        checks = ppt_create_checks(data, args, info, verdict)
        return _outcome(data, checks, f"{data['presentation']}: {len(data['slides'])} slide(s) created (title slide + {len(data['slides']) - 1} content).", data["presentation"])

    @registry.handler("office.ppt_read")
    async def ppt_read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        data = await call(ctx, "ppt_read", args["path"])
        lines = [f"{s['index']}. {s['title']} | " + "; ".join(s["bullets"])[:160] + (f" | notes: {s['notes'][:80]}" if s["notes"] else "") for s in data["slides"]]
        return ToolOutcome(data=data, backend=BACKEND, observation=f"{data['presentation']} ({data['slide_count']} slides):\n" + "\n".join(lines))

    @registry.handler("office.ppt_edit")
    async def ppt_edit(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        if not any(args.get(k) for k in ("add_slide", "replace", "notes")):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "give at least one of add_slide, replace, notes")
        data = await call(ctx, "ppt_edit", args["path"], args)
        info = await asyncio.to_thread(pptx_read_zip, data["presentation"])
        verdict = await asyncio.to_thread(_verify_file, data["presentation"], expected_slides=len(data["slides_after"]))
        checks = ppt_edit_checks(data, args, info, verdict)
        return _outcome(data, checks, f"{data['presentation']}: {len(data['slides_before'])} -> {len(data['slides_after'])} slide(s), replacements {data['replace_counts']}.", data["presentation"])
