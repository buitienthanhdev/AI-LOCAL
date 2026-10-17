"""OfficeVerificationEngine (spec sections 3 and 15): re-read a SAVED file and check it, with evidence.

Design rules
* Only the file on disk is looked at (zip + XML / pypdf / csv). No Office application, no calculation engine, no network.
* A check reports PASS only with evidence that it looked at the thing; a missing library or an impossible check is
  NOT_RUN with the reason. "No error was raised" is never a PASS.
* Things that cannot be known without the application (page numbers, TOC freshness, formula recalculation, real
  rendering) are listed in `not_verified` instead of being passed silently.
* Verdict: FAIL if any check failed; NOT_RUN if nothing could be run; WARN if only warnings; PASS otherwise.
"""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from lga_worker.office import filetype

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
ERROR_VALUES = ("#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A", "#NULL!", "#NUM!", "#SPILL!", "#CALC!", "#GETTING_DATA")
MAX_LIST = 20
MAX_CELLS = 6_000_000
PLACEHOLDER = re.compile(r"\{\{[^}\n]{1,60}\}\}|\[\[[^\]\n]{1,60}\]\]|<<[^>\n]{1,60}>>|\bLorem ipsum\b|\bTODO\b|\bXXX+\b", re.I)


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


# ----------------------------------------------------------------------------------------- result helpers
@dataclass
class Checks:
    items: list[dict[str, Any]] = field(default_factory=list)
    not_verified: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **evidence: Any) -> None:
        assert status in ("PASS", "FAIL", "WARN", "NOT_RUN")
        self.items.append({"name": name, "status": status, "detail": detail, "evidence": evidence})

    def passed(self, name: str, detail: str, **ev: Any) -> None:
        self.add(name, "PASS", detail, **ev)

    def failed(self, name: str, detail: str, **ev: Any) -> None:
        self.add(name, "FAIL", detail, **ev)

    def warn(self, name: str, detail: str, **ev: Any) -> None:
        self.add(name, "WARN", detail, **ev)

    def not_run(self, name: str, reason: str) -> None:
        self.add(name, "NOT_RUN", reason)

    def verdict(self) -> str:
        st = [c["status"] for c in self.items]
        if "FAIL" in st:
            return "FAIL"
        if "PASS" not in st:
            return "NOT_RUN"
        return "WARN" if "WARN" in st else "PASS"


def _result(path: str | Path, kind: str, checks: Checks, **extra: Any) -> dict[str, Any]:
    counts = {s: sum(1 for c in checks.items if c["status"] == s) for s in ("PASS", "FAIL", "WARN", "NOT_RUN")}
    return {"path": str(path), "detected_type": kind, "verdict": checks.verdict(), "counts": counts, "checks": checks.items,
            "not_verified": checks.not_verified, "recalculated": False, **extra}


def _listing(items: list[Any], limit: int = MAX_LIST) -> str:
    shown = ", ".join(str(i) for i in items[:limit])
    return shown + (f" ... (+{len(items) - limit} more)" if len(items) > limit else "")


def _norm(text: str, case: bool = True) -> str:
    t = re.sub(r"\s+", " ", text.replace(" ", " ")).strip()
    return t if case else t.casefold()


def _open_zip(path: Path, checks: Checks, label: str) -> zipfile.ZipFile | None:
    try:
        z = zipfile.ZipFile(path)
        bad = z.testzip()
        if bad:
            checks.failed("opens", f"{label}: zip member {bad} is damaged (CRC)")
            z.close()
            return None
        return z
    except (zipfile.BadZipFile, OSError) as exc:
        checks.failed("opens", f"{label}: cannot open as an Office package: {type(exc).__name__}: {exc}"[:240])
        return None


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, dict[str, str]]:
    folder, _, name = part.rpartition("/")
    rel_path = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
    if rel_path not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(rel_path)):
        out[rel.get("Id", "")] = {"target": rel.get("Target", ""), "mode": rel.get("TargetMode", "Internal"), "type": rel.get("Type", "")}
    return out


def _resolve(part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base = part.rpartition("/")[0].split("/") if "/" in part else []
    for seg in target.split("/"):
        if seg == "..":
            if base:
                base.pop()
        elif seg and seg != ".":
            base.append(seg)
    return "/".join(base)


# ============================================================================================= DOCX
@dataclass
class Para:
    text: str
    style_id: str
    style_name: str
    level: int | None


def _heading_level(style_id: str, style_name: str, outline: int | None) -> int | None:
    for s in (style_name, style_id):
        m = re.match(r"(?i)^(?:heading|ti[eê]u\s*[dđ][eề]?)\s*([1-9])$", s.strip())
        if m:
            return int(m.group(1))
    return outline + 1 if outline is not None else None


def parse_docx(z: zipfile.ZipFile) -> dict[str, Any]:
    names = set(z.namelist())
    if "word/document.xml" not in names:
        raise KeyError("word/document.xml")
    styles: dict[str, tuple[str, int | None]] = {}
    if "word/styles.xml" in names:
        for st in ET.fromstring(z.read("word/styles.xml")).iter(q(W, "style")):
            sid = st.get(q(W, "styleId"), "")
            nm = st.find(q(W, "name"))
            ol = st.find(f"{q(W, 'pPr')}/{q(W, 'outlineLvl')}")
            styles[sid] = (nm.get(q(W, "val"), "") if nm is not None else "", int(ol.get(q(W, "val"), "9")) if ol is not None and int(ol.get(q(W, "val"), "9")) < 9 else None)
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(q(W, "body"))
    if body is None:
        raise KeyError("w:body")

    def ptext(p: ET.Element) -> str:
        out = []
        for el in p.iter():
            if el.tag == q(W, "t") or el.tag == q(W, "delText"):
                out.append(el.text or "")
            elif el.tag == q(W, "tab"):
                out.append("\t")
            elif el.tag in (q(W, "br"), q(W, "cr")):
                out.append("\n")
        return "".join(out)

    def pinfo(p: ET.Element) -> Para:
        ppr = p.find(q(W, "pPr"))
        sid, outline = "", None
        if ppr is not None:
            ps = ppr.find(q(W, "pStyle"))
            sid = ps.get(q(W, "val"), "") if ps is not None else ""
            ol = ppr.find(q(W, "outlineLvl"))
            if ol is not None and ol.get(q(W, "val"), "9").isdigit() and int(ol.get(q(W, "val"), "9")) < 9:
                outline = int(ol.get(q(W, "val"), "0"))
        name, st_outline = styles.get(sid, (sid, None))
        return Para(ptext(p), sid, name, _heading_level(sid, name, outline if outline is not None else st_outline))

    paragraphs: list[Para] = []
    tables: list[dict[str, Any]] = []
    for child in body:
        if child.tag == q(W, "p"):
            paragraphs.append(pinfo(child))
        elif child.tag == q(W, "tbl"):
            grid = child.find(q(W, "tblGrid"))
            ncols = len(grid.findall(q(W, "gridCol"))) if grid is not None else 0
            rows = []
            for tr in child.findall(q(W, "tr")):
                cells, span_total = [], 0
                for tc in tr.findall(q(W, "tc")):
                    tcpr = tc.find(q(W, "tcPr"))
                    gs = tcpr.find(q(W, "gridSpan")) if tcpr is not None else None
                    span_total += int(gs.get(q(W, "val"), "1")) if gs is not None and gs.get(q(W, "val"), "1").isdigit() else 1
                    cells.append("\n".join(ptext(p) for p in tc.findall(q(W, "p"))))
                tprs = tr.find(q(W, "trPr"))
                skipped = 0
                if tprs is not None:  # gridBefore / gridAfter shift the row inside the grid
                    for tag in ("gridBefore", "gridAfter"):
                        el = tprs.find(q(W, tag))
                        skipped += int(el.get(q(W, "val"), "0")) if el is not None and el.get(q(W, "val"), "0").isdigit() else 0
                rows.append({"cells": cells, "span": span_total + skipped})
            tables.append({"grid_cols": ncols, "rows": rows, "nested": len(list(child.iter(q(W, "tbl")))) - 1})
    fields = [(el.text or "") for el in root.iter(q(W, "instrText"))]
    simple_fields = [el.get(q(W, "instr"), "") for el in root.iter(q(W, "fldSimple"))]
    all_fields = fields + simple_fields
    blips = []
    for b in root.iter("{%s}blip" % A):
        blips.append({"embed": b.get(q(R, "embed")), "link": b.get(q(R, "link"))})
    for v in root.iter("{urn:schemas-microsoft-com:vml}imagedata"):
        blips.append({"embed": v.get(q(R, "id")), "link": None})
    return {"paragraphs": paragraphs, "tables": tables, "fields": all_fields, "blips": blips,
            "tracked": {"ins": len(list(root.iter(q(W, "ins")))), "del": len(list(root.iter(q(W, "del"))))},
            "sections": len(list(root.iter(q(W, "sectPr")))), "comments": "word/comments.xml" in names,
            "footnotes": "word/footnotes.xml" in names}


def verify_docx(path: str | Path, *, expected_text: list[str] | None = None, expected_paragraphs: int | None = None,
                min_paragraphs: int | None = None, expected_tables: int | None = None, expected_headings: list[Any] | None = None,
                expect_headings: bool = False, expect_toc: bool = False, forbidden_text: list[str] | None = None,
                before: str | None = None, allowed_changes: list[str] | None = None, case_sensitive: bool = True,
                expect_no_tracked_changes: bool = False) -> dict[str, Any]:
    p = Path(path)
    checks = Checks()
    z = _open_zip(p, checks, "docx")
    model = None
    if z is not None:
        try:
            model = parse_docx(z)
            checks.passed("opens", f"valid package, word/document.xml parsed ({len(model['paragraphs'])} body paragraphs, {len(model['tables'])} tables)")
        except (KeyError, ET.ParseError) as exc:
            checks.failed("opens", f"document.xml missing or malformed: {exc}")
    if model is None or z is None:
        checks.not_verified.append("all content checks (file could not be opened)")
        return _result(p, "docx", checks)
    paras: list[Para] = model["paragraphs"]
    non_empty = [x for x in paras if x.text.strip()]
    table_text = "\n".join(c for t in model["tables"] for r in t["rows"] for c in r["cells"])
    full_text = "\n".join(x.text for x in paras) + "\n" + table_text
    # --- content present
    if not non_empty and not table_text.strip():
        checks.failed("content_present", "the document has no text at all (saved empty?)")
    else:
        detail = f"{len(non_empty)} non-empty paragraphs, {len(full_text)} characters"
        bad = []
        if expected_paragraphs is not None and len(non_empty) != expected_paragraphs:
            bad.append(f"expected {expected_paragraphs} non-empty paragraphs, found {len(non_empty)}")
        if min_paragraphs is not None and len(non_empty) < min_paragraphs:
            bad.append(f"expected at least {min_paragraphs} non-empty paragraphs, found {len(non_empty)}")
        (checks.failed if bad else checks.passed)("content_present", "; ".join(bad) or detail, paragraphs=len(non_empty))
    # --- expected / forbidden text
    if expected_text:
        hay = _norm(full_text, case_sensitive)
        missing = [t for t in expected_text if _norm(t, case_sensitive) not in hay]
        (checks.failed if missing else checks.passed)("expected_text_present", f"missing: {_listing(missing)}" if missing else f"all {len(expected_text)} expected texts found in the saved file")
    if forbidden_text:
        hay = _norm(full_text, case_sensitive)
        found = [t for t in forbidden_text if _norm(t, case_sensitive) in hay]
        (checks.failed if found else checks.passed)("forbidden_text_absent", f"still present: {_listing(found)}" if found else "none of the forbidden texts is present")
    leftovers = sorted({m.group(0) for m in PLACEHOLDER.finditer(full_text)})
    if leftovers:
        checks.warn("no_leftover_placeholders", f"template placeholders/TODO left in the text: {_listing(leftovers)}")
    else:
        checks.passed("no_leftover_placeholders", "no {{...}}, [[...]], TODO or lorem ipsum found")
    # --- headings
    heads = [(x.level, x.text.strip()) for x in paras if x.level is not None and x.text.strip()]
    if not heads:
        (checks.failed if expect_headings or expected_headings else checks.passed)("heading_hierarchy", "no headings in the document" + (" but headings were expected" if expect_headings or expected_headings else " (none expected)"))
    else:
        probs = []
        if heads[0][0] > 1:
            probs.append(f"first heading is level {heads[0][0]} ('{heads[0][1][:40]}'), not 1")
        for (l1, _), (l2, t2) in zip(heads, heads[1:]):
            if l2 > l1 + 1:
                probs.append(f"level jumps from {l1} to {l2} at '{t2[:40]}'")
        empty_heads = [x for x in paras if x.level is not None and not x.text.strip()]
        if empty_heads:
            probs.append(f"{len(empty_heads)} empty heading paragraph(s)")
        jumped = any("jumps" in x for x in probs)
        if probs:
            (checks.failed if jumped else checks.warn)("heading_hierarchy", "; ".join(probs), headings=len(heads))
        else:
            checks.passed("heading_hierarchy", f"{len(heads)} headings, levels never skip a step", levels=[h[0] for h in heads][:60])
    if expected_headings:
        wanted = [(h.get("level"), h.get("text", "")) if isinstance(h, dict) else (None, str(h)) for h in expected_headings]
        pos, missing = 0, []
        for lvl, txt in wanted:
            idx = next((i for i in range(pos, len(heads)) if _norm(heads[i][1], case_sensitive) == _norm(txt, case_sensitive) and (lvl is None or heads[i][0] == lvl)), None)
            if idx is None:
                missing.append(f"H{lvl or '?'} {txt}")
            else:
                pos = idx + 1
        (checks.failed if missing else checks.passed)("expected_headings_in_order", f"missing or out of order: {_listing(missing)}" if missing else "headings found in the expected order")
    # --- tables
    tprobs = []
    for i, t in enumerate(model["tables"], 1):
        if not t["rows"]:
            tprobs.append(f"table {i} has no rows")
            continue
        widths = {r["span"] for r in t["rows"]}
        if t["grid_cols"] and widths != {t["grid_cols"]}:
            tprobs.append(f"table {i}: rows span {sorted(widths)} columns but the grid declares {t['grid_cols']}")
        elif not t["grid_cols"]:
            tprobs.append(f"table {i} has no w:tblGrid")
        if any(not r["cells"] for r in t["rows"]):
            tprobs.append(f"table {i} has a row without cells")
    if expected_tables is not None and len(model["tables"]) != expected_tables:
        tprobs.append(f"expected {expected_tables} tables, found {len(model['tables'])}")
    if model["tables"] or expected_tables:
        (checks.failed if tprobs else checks.passed)("tables_intact", "; ".join(tprobs) if tprobs else f"{len(model['tables'])} tables: every row spans the declared grid", tables=len(model["tables"]))
    else:
        checks.passed("tables_intact", "no tables in the document")
    # --- duplicated paragraphs
    adj, far = [], {}
    prev = None
    for x in non_empty:
        t = _norm(x.text)
        if prev is not None and t == prev and len(t) >= 3:
            adj.append(t[:50])
        far[t] = far.get(t, 0) + 1
        prev = t
    dup_far = [t[:50] for t, n in far.items() if n > 1 and len(t) >= 40 and t[:50] not in adj]
    if adj:
        checks.failed("no_duplicate_paragraphs", f"{len(adj)} paragraph(s) repeated back to back: {_listing(adj, 5)}")
    elif dup_far:
        checks.warn("no_duplicate_paragraphs", f"{len(dup_far)} long paragraph(s) appear more than once (may be intended): {_listing(dup_far, 5)}")
    else:
        checks.passed("no_duplicate_paragraphs", "no repeated paragraphs")
    # --- images
    rels = _rels(z, "word/document.xml")
    broken, external = [], []
    for b in model["blips"]:
        rid = b["embed"] or b["link"]
        if not rid or rid not in rels:
            broken.append(rid or "(no id)")
            continue
        if rels[rid]["mode"] == "External":
            external.append(rels[rid]["target"][:60])
        elif _resolve("word/document.xml", rels[rid]["target"]) not in z.namelist():
            broken.append(rels[rid]["target"])
    if broken:
        checks.failed("images_resolved", f"{len(broken)} image reference(s) point to nothing: {_listing(broken, 5)}")
    elif external:
        checks.warn("images_resolved", f"{len(external)} linked (external) image(s) cannot be checked from the file: {_listing(external, 3)}")
    else:
        checks.passed("images_resolved", f"{len(model['blips'])} image reference(s), all present in the package")
    # --- TOC / fields / tracked changes
    has_toc = any(f.strip().upper().startswith("TOC") for f in model["fields"])
    if expect_toc and not has_toc:
        checks.failed("toc_present", "a table of contents was expected but no TOC field exists")
    elif has_toc:
        checks.passed("toc_present", "TOC field exists")
        checks.not_verified.append("TOC entries and page numbers are refreshed only by Word; their currency is not verified")
    tr = model["tracked"]
    if tr["ins"] or tr["del"]:
        (checks.failed if expect_no_tracked_changes else checks.warn)("tracked_changes", f"{tr['ins']} insertion(s) and {tr['del']} deletion(s) are still tracked (unaccepted)")
    elif expect_no_tracked_changes:
        checks.passed("tracked_changes", "no tracked changes left")
    # --- before / after
    if before:
        _docx_compare(checks, Path(before), non_empty, model, allowed_changes or [])
    checks.not_verified.append("page layout, page numbers and real rendering (export to PDF and verify the PDF)")
    return _result(p, "docx", checks, stats={"paragraphs": len(non_empty), "headings": len(heads), "tables": len(model["tables"]),
                                              "sections": model["sections"], "comments_part": model["comments"]})


def _docx_compare(checks: Checks, before: Path, after_paras: list[Para], after_model: dict[str, Any], allowed: list[str]) -> None:
    zb = _open_zip(before, Checks(), "before")
    if zb is None:
        checks.not_run("no_unintended_loss", f"cannot open the 'before' file {before}")
        return
    try:
        mb = parse_docx(zb)
    except (KeyError, ET.ParseError) as exc:
        checks.not_run("no_unintended_loss", f"'before' document unreadable: {exc}")
        return
    after_text = _norm("\n".join(x.text for x in after_paras) + "\n" + "\n".join(c for t in after_model["tables"] for r in t["rows"] for c in r["cells"]))
    lost = []
    for x in mb["paragraphs"]:
        t = _norm(x.text)
        if len(t) >= 3 and t not in after_text and not any(a and a in t for a in allowed):
            lost.append(t[:60])
    if len(mb["tables"]) > len(after_model["tables"]) and not any("table" in a.lower() for a in allowed):
        lost.append(f"{len(mb['tables']) - len(after_model['tables'])} table(s) disappeared")
    (checks.failed if lost else checks.passed)("no_unintended_loss", f"{len(lost)} original paragraph(s)/table(s) no longer present: {_listing(lost, 8)}" if lost
                                              else "every original paragraph is still present (changes outside the allowed list: none)")


# ============================================================================================= XLSX
def col_to_idx(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def idx_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_ref(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", ref.strip())
    if not m:
        raise ValueError(f"bad cell reference {ref!r}")
    return int(m.group(2)), col_to_idx(m.group(1))


def split_sheet_ref(ref: str) -> tuple[str, str]:
    if "!" not in ref:
        raise ValueError(f"cell reference {ref!r} needs a sheet: 'Sheet1!B2'")
    sheet, _, cell = ref.rpartition("!")
    return sheet.strip("'").replace("''", "'"), cell


@dataclass
class Cell:
    kind: str  # blank | number | string | empty_string | bool | error | formula
    value: Any = None  # constant value, or the cached result of a formula
    formula: str | None = None
    cached: bool = True
    cached_error: str | None = None


def _read_shared(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(q(SS, "si")):
        out.append("".join(t.text or "" for t in si.iter(q(SS, "t")) if True))
    return out


def iter_sheet_cells(z: zipfile.ZipFile, part: str, shared: list[str]) -> Iterator[tuple[int, int, Cell]]:
    shared_master: dict[str, str] = {}
    with z.open(part) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag != q(SS, "c"):
                continue
            ref = el.get("r", "")
            t = el.get("t")
            f = el.find(q(SS, "f"))
            v = el.find(q(SS, "v"))
            vtext = v.text if v is not None else None
            isel = el.find(q(SS, "is"))
            try:
                row, col = split_ref(ref)
            except ValueError:
                el.clear()
                continue
            if t == "s" and vtext is not None and vtext.isdigit() and int(vtext) < len(shared):
                value: Any = shared[int(vtext)]
            elif t == "inlineStr" and isel is not None:
                value = "".join(x.text or "" for x in isel.iter(q(SS, "t")))
            elif t in ("str", "e") and vtext is not None:
                value = vtext
            elif t == "b" and vtext is not None:
                value = vtext.strip() == "1"
            elif vtext not in (None, ""):
                try:
                    value = float(vtext)
                except ValueError:
                    value = vtext
            else:
                value = None
            if f is not None:
                ftext = f.text
                if f.get("t") == "shared":
                    si = f.get("si", "")
                    if ftext:
                        shared_master[si] = ftext
                    else:
                        ftext = f"(shared formula #{si}: {shared_master.get(si, '?')})"
                has_value = vtext is not None or (t == "inlineStr" and isel is not None)
                cell = Cell("formula", value if has_value else None, ftext or "", has_value, value if t == "e" else None)
            elif t == "e":
                cell = Cell("error", value, cached_error=value)
            elif value is None and t != "inlineStr" and not (t in ("s", "str") and vtext is not None):
                cell = Cell("blank")
            elif isinstance(value, bool):
                cell = Cell("bool", value)
            elif isinstance(value, str):
                cell = Cell("empty_string" if value == "" else "string", value)
            elif value is None:
                cell = Cell("empty_string", "")
            else:
                cell = Cell("number", value)
            yield row, col, cell
            el.clear()


def parse_xlsx(z: zipfile.ZipFile) -> dict[str, Any]:
    names = set(z.namelist())
    if "xl/workbook.xml" not in names:
        raise KeyError("xl/workbook.xml")
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = _rels(z, "xl/workbook.xml")
    sheets_el = wb.find(q(SS, "sheets"))
    if sheets_el is None:
        raise KeyError("sheets")
    shared = _read_shared(z)
    sheets: dict[str, dict[str, Any]] = {}
    budget = MAX_CELLS
    truncated = False
    for s in sheets_el:
        name = s.get("name", "")
        rid = s.get(q(R, "id"), "")
        target = rels.get(rid, {}).get("target")
        if not target:
            sheets[name] = {"error": f"sheet relationship {rid} missing", "cells": {}}
            continue
        part = _resolve("xl/workbook.xml", target)
        if part not in names:
            sheets[name] = {"error": f"sheet part {part} missing from the package", "cells": {}}
            continue
        cells: dict[tuple[int, int], Cell] = {}
        try:
            for row, col, cell in iter_sheet_cells(z, part, shared):
                cells[(row, col)] = cell
                budget -= 1
                if budget <= 0:
                    truncated = True
                    break
        except ET.ParseError as exc:
            sheets[name] = {"error": f"sheet XML malformed: {exc}", "cells": cells}
            continue
        sheets[name] = {"cells": cells, "state": s.get("state", "visible"), "part": part}
    names_def = []
    dn = wb.find(q(SS, "definedNames"))
    if dn is not None:
        names_def = [{"name": d.get("name", ""), "ref": d.text or ""} for d in dn]
    return {"sheets": sheets, "defined_names": names_def, "truncated": truncated, "has_calc_chain": "xl/calcChain.xml" in names,
            "has_vba": "xl/vbaProject.bin" in names, "tables": [n for n in names if n.startswith("xl/tables/")], "charts": [n for n in names if n.startswith("xl/charts/chart")]}


def sheet_stats(cells: dict[tuple[int, int], Cell]) -> dict[str, Any]:
    rows = cols = 0
    kinds: dict[str, int] = {}
    zero = 0
    for (r, c), cell in cells.items():
        kinds[cell.kind] = kinds.get(cell.kind, 0) + 1
        if cell.kind == "number" and cell.value == 0:
            zero += 1
        if cell.kind != "blank":
            rows, cols = max(rows, r), max(cols, c)
    return {"rows": rows, "cols": cols, "non_blank": sum(v for k, v in kinds.items() if k != "blank"), "kinds": kinds, "zero_numbers": zero,
            "blank_styled": kinds.get("blank", 0), "empty_strings": kinds.get("empty_string", 0)}


def _eff_value(cell: Cell) -> Any:
    return cell.value if cell.kind == "formula" else (cell.value if cell.kind in ("number", "string", "bool", "empty_string", "error") else None)


def _fnorm(f: str | None) -> str:
    return re.sub(r"\s+", "", (f or "")).lstrip("=").upper()


def _in_scope(sheet: str, row: int, col: int, scope: list[str]) -> bool:
    for s in scope:
        try:
            sh, rng = (s.rsplit("!", 1) + [""])[:2] if "!" in s else (s, "")
        except ValueError:
            continue
        if sh.strip("'") != sheet:
            continue
        if not rng:
            return True
        a, _, b = rng.partition(":")
        try:
            r1, c1 = split_ref(a)
            r2, c2 = split_ref(b or a)
        except ValueError:
            continue
        if min(r1, r2) <= row <= max(r1, r2) and min(c1, c2) <= col <= max(c1, c2):
            return True
    return False


def verify_xlsx(path: str | Path, *, before: str | None = None, expected_sheets: list[str] | None = None, expected_dims: dict[str, dict[str, int]] | None = None,
                expected_cells: dict[str, Any] | None = None, unique_key: dict[str, Any] | None = None, totals: list[dict[str, Any]] | None = None,
                edit_scope: list[str] | None = None, allow_shrink: bool = False, totals_unchanged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    p = Path(path)
    checks = Checks()
    z = _open_zip(p, checks, "xlsx")
    model = None
    if z is not None:
        try:
            model = parse_xlsx(z)
            checks.passed("opens", f"valid package, {len(model['sheets'])} sheet(s): {_listing(list(model['sheets']), 8)}")
        except (KeyError, ET.ParseError) as exc:
            checks.failed("opens", f"workbook.xml missing or malformed: {exc}")
    if model is None:
        checks.not_verified.append("all content checks (file could not be opened)")
        return _result(p, "xlsx", checks)
    sheets = model["sheets"]
    bad_sheets = {n: s["error"] for n, s in sheets.items() if s.get("error")}
    if bad_sheets:
        checks.failed("sheets_readable", "; ".join(f"{n}: {e}" for n, e in bad_sheets.items()))
    else:
        checks.passed("sheets_readable", "every sheet part is present and well-formed")
    if model["truncated"]:
        checks.warn("complete_read", f"workbook has more than {MAX_CELLS} cells; only the first part was checked")
    stats = {n: sheet_stats(s["cells"]) for n, s in sheets.items()}
    if expected_sheets is not None:
        missing = [s for s in expected_sheets if s not in sheets]
        (checks.failed if missing else checks.passed)("expected_sheets", f"missing sheets: {_listing(missing)}" if missing else "all expected sheets exist")
    # --- error values
    errs, brk = [], []
    for n, s in sheets.items():
        for (r, c), cell in sorted(s["cells"].items()):
            loc = f"{n}!{idx_to_col(c)}{r}"
            ev = cell.cached_error or (cell.value if cell.kind == "error" else None) or (cell.value if cell.kind == "formula" and isinstance(cell.value, str) and cell.value in ERROR_VALUES else None)
            if ev:
                errs.append(f"{loc} {ev}")
            if cell.kind == "formula" and "#REF!" in (cell.formula or "").upper():
                brk.append(f"{loc} ={cell.formula}")
    (checks.failed if errs else checks.passed)("no_error_values", f"{len(errs)} cell(s) hold error values: {_listing(errs)}" if errs else "no #REF!/#VALUE!/#DIV/0!/#NAME?/#N/A error value in any cell", errors=errs[:MAX_LIST])
    bad_names = [d for d in model["defined_names"] if "#REF!" in d["ref"].upper()]
    if brk or bad_names:
        checks.failed("no_broken_references", f"formulas with #REF!: {_listing(brk)}; names with #REF!: {_listing([d['name'] for d in bad_names])}")
    else:
        checks.passed("no_broken_references", "no formula or defined name contains a #REF! reference")
    # --- calculation state
    formulas = [(n, rc, c) for n, s in sheets.items() for rc, c in s["cells"].items() if c.kind == "formula"]
    uncached = [f"{n}!{idx_to_col(rc[1])}{rc[0]}" for n, rc, c in formulas if not c.cached]
    if formulas and uncached:
        checks.warn("formula_results_cached", f"{len(uncached)} of {len(formulas)} formulas have no stored result: the file was written by a library and never calculated; "
                    f"their values are UNKNOWN here (no calculation engine): {_listing(uncached, 6)}")
        checks.not_verified.append("results of formulas without cached values (needs Excel or another calculation engine; this tool never recalculates)")
    elif formulas:
        checks.passed("formula_results_cached", f"all {len(formulas)} formulas carry a stored result (as last calculated by the application that saved the file)")
    else:
        checks.passed("formula_results_cached", "no formulas in the workbook")
    # --- empty vs 0 vs ""
    info = {n: {k: st[k] for k in ("rows", "cols", "non_blank", "zero_numbers", "empty_strings", "blank_styled")} for n, st in stats.items()}
    checks.passed("empty_zero_distinction", "counted separately: blank cells, numeric 0, empty strings and errors per sheet", sheets=info)
    # --- dimensions
    if expected_dims:
        bad = []
        for sh, want in expected_dims.items():
            if sh not in stats:
                bad.append(f"sheet {sh} missing")
                continue
            for key, field_name in (("rows", "rows"), ("cols", "cols")):
                if key in want and stats[sh][field_name] != want[key]:
                    bad.append(f"{sh}: {key} {stats[sh][field_name]} != expected {want[key]}")
                if f"min_{key}" in want and stats[sh][field_name] < want[f"min_{key}"]:
                    bad.append(f"{sh}: {key} {stats[sh][field_name]} < min {want[f'min_{key}']}")
        (checks.failed if bad else checks.passed)("expected_dimensions", "; ".join(bad) if bad else "row and column counts match the expectation")
    # --- expected cells
    if expected_cells:
        bad = []
        for ref, want in expected_cells.items():
            try:
                sh, cell_ref = split_sheet_ref(ref)
                rr, cc = split_ref(cell_ref)
            except ValueError as exc:
                bad.append(str(exc))
                continue
            cell = sheets.get(sh, {"cells": {}})["cells"].get((rr, cc), Cell("blank")) if sh in sheets else None
            if cell is None:
                bad.append(f"{ref}: sheet missing")
            elif want is None:
                if cell.kind != "blank":
                    bad.append(f"{ref}: expected an empty cell, found {cell.kind} {cell.value!r}")
            elif isinstance(want, str) and want.startswith("="):
                if cell.kind != "formula":
                    bad.append(f"{ref}: expected formula {want}, found {cell.kind} {cell.value!r} (formula replaced by a value?)")
                elif _fnorm(cell.formula) != _fnorm(want):
                    bad.append(f"{ref}: formula is ={cell.formula}, expected {want}")
            elif want == "":
                if cell.kind != "empty_string":
                    bad.append(f"{ref}: expected an empty string, found {cell.kind}")
            elif isinstance(want, bool):
                if _eff_value(cell) is not want:
                    bad.append(f"{ref}: expected {want}, found {cell.value!r}")
            elif isinstance(want, (int, float)):
                v = _eff_value(cell)
                if not isinstance(v, float) or isinstance(v, bool) or abs(v - float(want)) > 1e-9 * max(1.0, abs(float(want))):
                    bad.append(f"{ref}: expected {want}, found {v!r} ({cell.kind}{', no stored result' if cell.kind == 'formula' and not cell.cached else ''})")
            elif _eff_value(cell) != want:
                bad.append(f"{ref}: expected {want!r}, found {_eff_value(cell)!r}")
        (checks.failed if bad else checks.passed)("expected_cells", _listing(bad) if bad else f"all {len(expected_cells)} expected cells match (kind-sensitive: blank vs 0 vs \"\")")
    # --- key uniqueness
    if unique_key:
        sh = unique_key.get("sheet", "")
        col = unique_key["column"]
        ci = col if isinstance(col, int) else col_to_idx(str(col))
        skip = int(unique_key.get("skip_rows", 1))
        if sh not in sheets:
            checks.failed("unique_key", f"sheet {sh} missing")
        else:
            seen: dict[Any, int] = {}
            dups, blanks = [], []
            maxr = stats[sh]["rows"]
            for r in range(skip + 1, maxr + 1):
                cell = sheets[sh]["cells"].get((r, ci), Cell("blank"))
                if cell.kind in ("blank", "empty_string"):
                    blanks.append(r)
                    continue
                key = _eff_value(cell)
                if key in seen:
                    dups.append(f"{key!r} rows {seen[key]} and {r}")
                else:
                    seen[key] = r
            bad = []
            if dups:
                bad.append(f"duplicate keys: {_listing(dups, 8)}")
            if blanks and unique_key.get("allow_blank") is not True:
                bad.append(f"blank keys in rows {_listing(blanks, 8)}")
            (checks.failed if bad else checks.passed)("unique_key", "; ".join(bad) if bad else f"{len(seen)} distinct keys in column {col}, none blank")
    # --- totals
    for tspec in totals or []:
        sh, col = tspec["sheet"], tspec["column"]
        got = column_sum(sheets.get(sh, {"cells": {}})["cells"], col if isinstance(col, int) else col_to_idx(str(col)), int(tspec.get("skip_rows", 1)))
        want = tspec["sum"]
        tol = float(tspec.get("tol", 1e-6))
        ok = got[0] is not None and math.isfinite(got[0]) and abs(got[0] - float(want)) <= tol * max(1.0, abs(float(want)))
        (checks.passed if ok else checks.failed)(f"total_{sh}_{col}", f"sum of numbers in {sh} column {col} = {got[0]} ({got[1]} numeric cells); expected {want}")
    # --- before / after
    if before:
        _xlsx_compare(checks, Path(before), model, stats, edit_scope or [], allow_shrink, totals_unchanged or [])
    checks.not_verified.append("independent recomputation of formula results (no calculation engine is used; compare against the source data with expected_cells/totals)")
    return _result(p, "xlsx", checks, stats={n: {k: v for k, v in st.items() if k in ("rows", "cols", "non_blank")} for n, st in stats.items()},
                   formulas=len(formulas), has_vba=model["has_vba"], tables=len(model["tables"]), charts=len(model["charts"]))


def column_sum(cells: dict[tuple[int, int], Cell], col: int, skip_rows: int = 1) -> tuple[float | None, int]:
    total, n = 0.0, 0
    for (r, c), cell in cells.items():
        if c == col and r > skip_rows:
            v = _eff_value(cell)
            if isinstance(v, float) and not isinstance(v, bool):
                total += v
                n += 1
    return (total if n else None), n


def _xlsx_compare(checks: Checks, before: Path, after: dict[str, Any], after_stats: dict[str, Any], scope: list[str], allow_shrink: bool,
                  totals_cols: list[dict[str, Any]]) -> None:
    zb = _open_zip(before, Checks(), "before")
    if zb is None:
        checks.not_run("before_after", f"cannot open the 'before' workbook {before}")
        return
    try:
        mb = parse_xlsx(zb)
    except (KeyError, ET.ParseError) as exc:
        checks.not_run("before_after", f"'before' workbook unreadable: {exc}")
        return
    missing = [s for s in mb["sheets"] if s not in after["sheets"]]
    (checks.failed if missing else checks.passed)("sheets_preserved", f"sheets removed: {_listing(missing)}" if missing else "every original sheet still exists")
    shrink, grow = [], []
    for n, s in mb["sheets"].items():
        if n not in after_stats:
            continue
        b = sheet_stats(s["cells"])
        a = after_stats[n]
        for k in ("rows", "cols"):
            if a[k] < b[k]:
                shrink.append(f"{n}: {k} {b[k]} -> {a[k]}")
            elif a[k] > b[k]:
                grow.append(f"{n}: {k} {b[k]} -> {a[k]}")
    if shrink and not allow_shrink:
        checks.failed("row_col_counts", f"data shrank: {_listing(shrink)}")
    else:
        checks.passed("row_col_counts", ("no row/column lost" if not shrink else f"shrinkage allowed: {_listing(shrink)}") + (f"; grew: {_listing(grow)}" if grow else ""))
    overwritten, outside, changed = [], [], 0
    for n, s in mb["sheets"].items():
        if n not in after["sheets"]:
            continue
        acells = after["sheets"][n]["cells"]
        for (r, c), bc in s["cells"].items():
            ac = acells.get((r, c), Cell("blank"))
            loc = f"{n}!{idx_to_col(c)}{r}"
            same = bc.kind == ac.kind and (bc.formula is None or _fnorm(bc.formula) == _fnorm(ac.formula)) and (bc.kind == "formula" or bc.value == ac.value)
            if same:
                continue
            changed += 1
            in_scope = _in_scope(n, r, c, scope) if scope else True
            if bc.kind == "formula" and ac.kind != "formula" and not in_scope:
                overwritten.append(f"{loc} was ={bc.formula}, now {ac.kind} {ac.value!r}")
            elif bc.kind == "formula" and ac.kind != "formula":
                overwritten.append(f"{loc} was ={bc.formula}, now {ac.kind} {ac.value!r} (inside the edit scope - confirm it was meant)")
            elif scope and not in_scope:
                outside.append(f"{loc} {bc.kind}:{bc.value!r} -> {ac.kind}:{ac.value!r}")
    hard = [o for o in overwritten if "inside the edit scope" not in o]
    if hard:
        checks.failed("formulas_not_overwritten", f"{len(hard)} formula(s) replaced by static values outside the edit scope: {_listing(hard, 8)}")
    elif overwritten:
        checks.warn("formulas_not_overwritten", f"formula(s) replaced by values inside the edit scope: {_listing(overwritten, 8)}")
    else:
        checks.passed("formulas_not_overwritten", "no formula of the original became a static value")
    if scope:
        (checks.failed if outside else checks.passed)("no_out_of_scope_changes", f"{len(outside)} cell(s) outside the edit scope changed: {_listing(outside, 8)}" if outside
                                                    else f"{changed} cell(s) changed, all inside the edit scope")
    else:
        checks.passed("changes_listed", f"{changed} cell(s) differ from the original (no edit_scope given, so scope was not enforced)")
    for tspec in totals_cols:
        sh, col = tspec["sheet"], tspec["column"]
        ci = col if isinstance(col, int) else col_to_idx(str(col))
        b = column_sum(mb["sheets"].get(sh, {"cells": {}})["cells"], ci)
        a = column_sum(after["sheets"].get(sh, {"cells": {}})["cells"], ci)
        same = (b[0] is None and a[0] is None) or (b[0] is not None and a[0] is not None and abs(a[0] - b[0]) <= 1e-6 * max(1.0, abs(b[0])))
        (checks.passed if same else checks.failed)(f"total_unchanged_{sh}_{col}", f"column {col} of {sh}: before {b[0]} ({b[1]} numbers), after {a[0]} ({a[1]} numbers)")


# ============================================================================================= CSV
def verify_csv(path: str | Path, *, expected_rows: int | None = None, expected_cols: int | None = None, expected_header: list[str] | None = None,
               expected_text: list[str] | None = None) -> dict[str, Any]:
    p = Path(path)
    checks = Checks()
    try:
        raw = p.read_bytes()
    except OSError as exc:
        checks.failed("opens", f"cannot read: {exc}")
        return _result(p, "csv", checks)
    if len(raw) > 200 * 1024 * 1024:
        checks.not_run("opens", "file larger than 200 MB; not loaded")
        return _result(p, "csv", checks)
    enc = "utf-8-sig"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        enc = "cp1252"
        text = raw.decode(enc, errors="replace")
        checks.warn("encoding", "not valid UTF-8; read as cp1252 (Vietnamese text may be damaged)")
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect=dialect))
    rows = [r for r in rows if r]
    if not rows:
        checks.failed("opens", "no rows")
        return _result(p, "csv", checks)
    checks.passed("opens", f"{len(rows)} rows, delimiter {dialect.delimiter!r}, encoding {enc}")
    widths = {}
    for i, r in enumerate(rows, 1):
        widths.setdefault(len(r), []).append(i)
    if len(widths) > 1:
        main = max(widths, key=lambda k: len(widths[k]))
        odd = [f"row {i} has {w} fields" for w, rs in widths.items() if w != main for i in rs]
        checks.failed("rectangular", f"{len(odd)} ragged row(s) (most rows have {main}): {_listing(odd, 8)}")
    else:
        checks.passed("rectangular", f"every row has {next(iter(widths))} fields")
    header = rows[0]
    if len(set(h.strip().lower() for h in header)) != len(header):
        checks.warn("header", "duplicate column names in the first row")
    if expected_header is not None:
        (checks.passed if [h.strip() for h in header] == expected_header else checks.failed)("expected_header", f"header is {header}")
    if expected_rows is not None:
        (checks.passed if len(rows) == expected_rows else checks.failed)("expected_rows", f"{len(rows)} rows (header included), expected {expected_rows}")
    if expected_cols is not None:
        (checks.passed if len(header) == expected_cols else checks.failed)("expected_cols", f"{len(header)} columns, expected {expected_cols}")
    if expected_text:
        miss = [t for t in expected_text if t not in text]
        (checks.failed if miss else checks.passed)("expected_text_present", f"missing: {_listing(miss)}" if miss else "all expected texts found")
    return _result(p, "csv", checks, stats={"rows": len(rows), "cols": len(header)})


# ============================================================================================= PPTX
EMU_PT = 12700.0
CHAR_W = 0.52  # average glyph width / font size for Latin text (Calibri ~0.49, Arial ~0.55)
LINE_H = 1.2


@dataclass
class Shape:
    name: str
    kind: str  # text | picture | table | chart | group | other
    ph: str | None
    box: tuple[float, float, float, float] | None  # x, y, w, h in pt
    paragraphs: list[tuple[str, float, bool]]  # (text, font_pt, bold)
    wrap: bool = True
    autofit: str = "none"  # none | shrink | grow
    font_scale: float = 1.0
    insets: tuple[float, float, float, float] = (7.2, 3.6, 7.2, 3.6)
    image_ref: str | None = None
    image_link: bool = False
    size_assumed: bool = False


def _para_info(p: ET.Element, default_pt: float) -> tuple[str, float, bool]:
    texts, sizes, bold = [], [], False
    for r in p:
        if r.tag in (q(A, "r"), q(A, "fld")):
            t = r.find(q(A, "t"))
            texts.append(t.text or "" if t is not None else "")
            rpr = r.find(q(A, "rPr"))
            if rpr is not None:
                if rpr.get("sz", "").isdigit():
                    sizes.append(int(rpr.get("sz", "0")) / 100.0)
                bold = bold or rpr.get("b") == "1"
        elif r.tag == q(A, "br"):
            texts.append("\n")
    if not sizes:
        ppr = p.find(q(A, "pPr"))
        d = ppr.find(q(A, "defRPr")) if ppr is not None else None
        end = p.find(q(A, "endParaRPr"))
        for src in (d, end):
            if src is not None and src.get("sz", "").isdigit():
                sizes.append(int(src.get("sz", "0")) / 100.0)
                break
    return "".join(texts), (max(sizes) if sizes else default_pt), bold


def _parse_shapes(root: ET.Element, rels: dict[str, dict[str, str]]) -> list[Shape]:
    tree = root.find(f"{q(P, 'cSld')}/{q(P, 'spTree')}")
    out: list[Shape] = []
    if tree is None:
        return out

    def xfrm_box(el: ET.Element | None) -> tuple[float, float, float, float] | None:
        if el is None:
            return None
        off, ext = el.find(q(A, "off")), el.find(q(A, "ext"))
        if off is None or ext is None:
            return None
        return (int(off.get("x", "0")) / EMU_PT, int(off.get("y", "0")) / EMU_PT, int(ext.get("cx", "0")) / EMU_PT, int(ext.get("cy", "0")) / EMU_PT)

    def walk(parent: ET.Element, in_group: bool) -> None:
        for el in parent:
            tag = el.tag
            if tag == q(P, "sp"):
                nv = el.find(f"{q(P, 'nvSpPr')}/{q(P, 'cNvPr')}")
                ph = el.find(f"{q(P, 'nvSpPr')}/{q(P, 'nvPr')}/{q(P, 'ph')}")
                phtype = (ph.get("type") or "body") if ph is not None else None
                sppr = el.find(q(P, "spPr"))
                box = None if in_group else xfrm_box(sppr.find(q(A, "xfrm")) if sppr is not None else None)
                body = el.find(q(P, "txBody"))
                paras: list[tuple[str, float, bool]] = []
                wrap, autofit, scale, insets, assumed = True, "none", 1.0, (7.2, 3.6, 7.2, 3.6), False
                if body is not None:
                    default = 44.0 if phtype in ("title", "ctrTitle") else (32.0 if phtype in ("body", "subTitle") else 18.0)
                    bpr = body.find(q(A, "bodyPr"))
                    if bpr is not None:
                        wrap = bpr.get("wrap", "square") != "none"
                        insets = tuple(int(bpr.get(k, d)) / EMU_PT for k, d in (("lIns", "91440"), ("tIns", "45720"), ("rIns", "91440"), ("bIns", "45720")))  # type: ignore[assignment]
                        if bpr.find(q(A, "normAutofit")) is not None:
                            autofit = "shrink"
                            fs = bpr.find(q(A, "normAutofit")).get("fontScale")  # type: ignore[union-attr]
                            scale = int(fs) / 100000.0 if fs and fs.isdigit() else 1.0
                        elif bpr.find(q(A, "spAutoFit")) is not None:
                            autofit = "grow"
                    for ap in body.findall(q(A, "p")):
                        text, size, bold = _para_info(ap, default)
                        if not any(r.tag == q(A, "r") and r.find(q(A, "rPr")) is not None and r.find(q(A, "rPr")).get("sz") for r in ap):  # type: ignore[union-attr]
                            assumed = assumed or bool(text.strip())
                        paras.append((text, size, bold))
                has_text = any(t.strip() for t, _, _ in paras)
                out.append(Shape(nv.get("name", "") if nv is not None else "", "text" if has_text else "other", phtype, box, paras, wrap, autofit, scale, insets, size_assumed=assumed))  # type: ignore[arg-type]
            elif tag == q(P, "pic"):
                nv = el.find(f"{q(P, 'nvPicPr')}/{q(P, 'cNvPr')}")
                blip = el.find(f"{q(P, 'blipFill')}/{q(A, 'blip')}")
                emb = blip.get(q(R, "embed")) if blip is not None else None
                lnk = blip.get(q(R, "link")) if blip is not None else None
                sppr = el.find(q(P, "spPr"))
                out.append(Shape(nv.get("name", "") if nv is not None else "", "picture", None, None if in_group else xfrm_box(sppr.find(q(A, "xfrm")) if sppr is not None else None), [],
                                 image_ref=emb or lnk, image_link=bool(lnk and not emb)))
            elif tag == q(P, "graphicFrame"):
                nv = el.find(f"{q(P, 'nvGraphicFramePr')}/{q(P, 'cNvPr')}")
                graphic = el.find(f"{q(A, 'graphic')}/{q(A, 'graphicData')}")
                uri = graphic.get("uri", "") if graphic is not None else ""
                kind = "table" if uri.endswith("/table") else ("chart" if uri.endswith("/chart") else "other")
                x = el.find(q(P, "xfrm"))
                texts = [t.text or "" for t in el.iter(q(A, "t"))]
                out.append(Shape(nv.get("name", "") if nv is not None else "", kind, None, None if in_group else xfrm_box(x), [(" ".join(texts), 18.0, False)] if texts else []))
            elif tag == q(P, "grpSp"):
                nv = el.find(f"{q(P, 'nvGrpSpPr')}/{q(P, 'cNvPr')}")
                out.append(Shape(nv.get("name", "") if nv is not None else "", "group", None, None, []))
                walk(el, True)

    walk(tree, False)
    return out


def estimate_text_height(shape: Shape) -> tuple[float, float]:
    """(needed height in pt, widest unwrapped line in pt) for the text of one shape - an ESTIMATE from font size, not rendering."""
    if shape.box is None:
        return 0.0, 0.0
    avail_w = max(1.0, shape.box[2] - shape.insets[0] - shape.insets[2])
    total, widest = 0.0, 0.0
    for text, size, bold in shape.paragraphs:
        size = max(1.0, size * shape.font_scale)
        glyph = size * CHAR_W * (1.07 if bold else 1.0)
        lines = 0
        for hard in text.split("\n") or [""]:
            width = sum(glyph * (2.0 if ord(ch) > 0x2E80 else 1.0) for ch in hard)
            widest = max(widest, width)
            if not shape.wrap or width <= avail_w:
                lines += 1
                continue
            # greedy word wrap
            cur, n = 0.0, 1
            for word in hard.split(" "):
                w = sum(glyph * (2.0 if ord(ch) > 0x2E80 else 1.0) for ch in word) + glyph
                if cur + w > avail_w and cur > 0:
                    n += 1
                    cur = w
                else:
                    cur += w
                if w > avail_w:  # a single word wider than the box breaks anyway
                    n += int(w // avail_w)
                    cur = w % avail_w
            lines += n
        total += lines * size * LINE_H
    return total + shape.insets[1] + shape.insets[3], widest


def parse_pptx(z: zipfile.ZipFile) -> dict[str, Any]:
    names = set(z.namelist())
    if "ppt/presentation.xml" not in names:
        raise KeyError("ppt/presentation.xml")
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    sz = pres.find(q(P, "sldSz"))
    width = int(sz.get("cx", "9144000")) / EMU_PT if sz is not None else 720.0
    height = int(sz.get("cy", "6858000")) / EMU_PT if sz is not None else 540.0
    rels = _rels(z, "ppt/presentation.xml")
    slides = []
    lst = pres.find(q(P, "sldIdLst"))
    for i, sid in enumerate(lst if lst is not None else [], 1):
        rid = sid.get(q(R, "id"), "")
        target = rels.get(rid, {}).get("target")
        part = _resolve("ppt/presentation.xml", target) if target else None
        entry: dict[str, Any] = {"index": i, "part": part, "shapes": [], "error": None, "title": ""}
        if not part or part not in names:
            entry["error"] = f"slide part for {rid} missing from the package"
        else:
            try:
                root = ET.fromstring(z.read(part))
                srels = _rels(z, part)
                entry["shapes"] = _parse_shapes(root, srels)
                entry["rels"] = srels
                titles = [s for s in entry["shapes"] if s.ph in ("title", "ctrTitle")]
                entry["title"] = _norm(" ".join(t for s in titles for t, _, _ in s.paragraphs)) if titles else ""
                entry["hidden"] = root.get("show") == "0"
                entry["text"] = "\n".join(t for s in entry["shapes"] for t, _, _ in s.paragraphs)
            except ET.ParseError as exc:
                entry["error"] = f"slide XML malformed: {exc}"
        slides.append(entry)
    return {"width": width, "height": height, "slides": slides, "has_vba": "ppt/vbaProject.bin" in names, "names": names}


def _inter(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def verify_pptx(path: str | Path, *, expected_slides: int | None = None, expected_titles: list[str] | None = None, expected_text: list[str] | None = None,
                min_slides: int | None = None) -> dict[str, Any]:
    p = Path(path)
    checks = Checks()
    z = _open_zip(p, checks, "pptx")
    model = None
    if z is not None:
        try:
            model = parse_pptx(z)
            checks.passed("opens", f"valid package, {len(model['slides'])} slide(s), {model['width']:.0f}x{model['height']:.0f} pt")
        except (KeyError, ET.ParseError) as exc:
            checks.failed("opens", f"presentation.xml missing or malformed: {exc}")
    if model is None or z is None:
        checks.not_verified.append("all slide checks (file could not be opened)")
        return _result(p, "pptx", checks)
    slides = model["slides"]
    W_, H_ = model["width"], model["height"]
    if expected_slides is not None:
        (checks.passed if len(slides) == expected_slides else checks.failed)("slide_count", f"{len(slides)} slides, expected {expected_slides}")
    elif min_slides is not None:
        (checks.passed if len(slides) >= min_slides else checks.failed)("slide_count", f"{len(slides)} slides, expected at least {min_slides}")
    elif not slides:
        checks.failed("slide_count", "the presentation has no slides")
    else:
        checks.passed("slide_count", f"{len(slides)} slides")
    broken = [f"slide {s['index']}: {s['error']}" for s in slides if s["error"]]
    (checks.failed if broken else checks.passed)("every_slide_readable", _listing(broken) if broken else f"all {len(slides)} slides were parsed (not only the first)")
    overflow_f, overflow_w, overlaps, oob, imgs, empty, inherited = [], [], [], [], [], [], []
    per_slide = []
    for s in slides:
        if s["error"]:
            per_slide.append({"slide": s["index"], "error": s["error"]})
            continue
        shapes: list[Shape] = s["shapes"]
        content = [x for x in shapes if x.kind in ("text", "picture", "table", "chart")]
        if not content:
            empty.append(s["index"])
        for sh in shapes:
            tag = f"slide {s['index']} '{sh.name[:30]}'"
            if sh.kind == "picture":
                rel = s.get("rels", {}).get(sh.image_ref or "", None)
                if not sh.image_ref or rel is None:
                    imgs.append(f"{tag}: picture has no resolvable image reference")
                elif sh.image_link or rel["mode"] == "External":
                    imgs.append(f"{tag}: LINKED image {rel['target'][:50]} (cannot be checked from the file)")
                elif _resolve(s["part"], rel["target"]) not in model["names"]:
                    imgs.append(f"{tag}: image {rel['target']} is missing from the package")
            if sh.box is None:
                if sh.kind == "text" and sh.ph:
                    inherited.append(s["index"])
                continue
            x, y, w, h = sh.box
            area = max(1.0, w * h)
            outside = area - _inter(sh.box, (0.0, 0.0, W_, H_))
            if outside / area > 0.02 and sh.kind in ("text", "picture", "table", "chart"):
                (oob if outside / area > 0.10 else overlaps).append(f"{tag}: {outside / area:.0%} of the shape lies outside the slide")
            if sh.kind == "text":
                need, widest = estimate_text_height(sh)
                if sh.autofit == "grow":
                    if y + need > H_ * 1.01:
                        overflow_f.append(f"{tag}: auto-grow text box would end at {y + need:.0f} pt, slide height {H_:.0f} pt")
                elif need > h * 1.02:
                    ratio = need / max(h, 1.0)
                    note = f"{tag}: text needs ~{need:.0f} pt, box is {h:.0f} pt high ({ratio:.2f}x)" + (", font size assumed" if sh.size_assumed else "")
                    if sh.autofit == "shrink":
                        overflow_w.append(note + " (shrink-on-overflow is on; PowerPoint reduces the font when it opens the slide)")
                    elif ratio > 1.35:
                        overflow_f.append(note)
                    else:
                        overflow_w.append(note)
                elif not sh.wrap and widest > w - sh.insets[0] - sh.insets[2] + 1:
                    overflow_w.append(f"{tag}: unwrapped line ~{widest:.0f} pt is wider than the box {w:.0f} pt")
        boxes = [(sh, sh.box) for sh in shapes if sh.box is not None and (sh.kind == "text" or sh.kind == "picture" or sh.kind in ("table", "chart"))]
        for i, (a, ba) in enumerate(boxes):
            for b, bb in boxes[i + 1:]:
                if a.kind == "picture" and b.kind == "picture":
                    continue
                if (a.kind == "picture" and ba[2] * ba[3] >= 0.9 * W_ * H_) or (b.kind == "picture" and bb[2] * bb[3] >= 0.9 * W_ * H_):
                    continue  # a full-slide background picture
                inter = _inter(ba, bb)
                small = max(1.0, min(ba[2] * ba[3], bb[2] * bb[3]))
                if inter / small > 0.15:
                    overlaps.append(f"slide {s['index']}: '{a.name[:25]}' overlaps '{b.name[:25]}' ({inter / small:.0%} of the smaller)")
        per_slide.append({"slide": s["index"], "title": s["title"][:60], "shapes": len(shapes), "has_content": bool(content), "hidden": s.get("hidden", False)})
    imgs_hard = [i for i in imgs if "LINKED" not in i]
    if imgs_hard:
        checks.failed("images_present", _listing(imgs_hard, 8))
    elif imgs:
        checks.warn("images_present", _listing(imgs, 5))
    else:
        checks.passed("images_present", "every picture resolves to an image inside the package")
    if overflow_f:
        checks.failed("text_overflow_estimate", _listing(overflow_f, 8) + " [estimate from box size and font size, not a rendering]")
    elif overflow_w:
        checks.warn("text_overflow_estimate", _listing(overflow_w, 8) + " [estimate]")
    else:
        checks.passed("text_overflow_estimate", f"no text box looks too small on any of the {len(slides)} slides [estimate]")
    if oob:
        checks.failed("inside_slide_bounds", _listing(oob, 8))
    elif any("outside the slide" in o for o in overlaps):
        checks.warn("inside_slide_bounds", _listing([o for o in overlaps if "outside the slide" in o], 8))
    else:
        checks.passed("inside_slide_bounds", "all shapes with a known position are inside the slide")
    pure_overlaps = [o for o in overlaps if "outside the slide" not in o]
    (checks.warn if pure_overlaps else checks.passed)("no_overlap", _listing(pure_overlaps, 8) + " [boxes overlap; may be intentional]" if pure_overlaps else "no text/picture boxes overlap")
    (checks.warn if empty else checks.passed)("no_empty_slides", f"slides without any text/picture/table/chart: {empty}" if empty else "every slide has content")
    if inherited:
        checks.not_verified.append(f"geometry inherited from the slide layout on slide(s) {sorted(set(inherited))}: overflow/overlap not computed for those placeholders")
    if expected_titles:
        got = [s["title"] for s in slides]
        bad = []
        for i, want in enumerate(expected_titles):
            if i >= len(got):
                bad.append(f"slide {i + 1}: missing (expected '{want}')")
            elif _norm(got[i], False) != _norm(want, False):
                bad.append(f"slide {i + 1}: title '{got[i]}' != expected '{want}'")
        (checks.failed if bad else checks.passed)("slide_order_titles", _listing(bad) if bad else "slide titles appear in the expected order")
    if expected_text:
        hay = _norm("\n".join(s.get("text", "") for s in slides))
        miss = [t for t in expected_text if _norm(t) not in hay]
        (checks.failed if miss else checks.passed)("expected_text_present", f"missing: {_listing(miss)}" if miss else "all expected texts found on the slides")
    checks.not_verified.append("real rendering, fonts availability and visual alignment (export to PDF/PNG and look at it)")
    return _result(p, "pptx", checks, slides=per_slide, has_vba=model["has_vba"])


# ============================================================================================= PDF
def _pdf_text(path: Path) -> tuple[list[str] | None, str | None, int | None, bool]:
    """(page texts, problem, page count, encrypted)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "pypdf is not installed", None, False
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                return None, "PDF is password protected", None, True
        n = len(reader.pages)
        texts = []
        for i in range(min(n, 200)):
            try:
                texts.append(reader.pages[i].extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                texts.append("")
                if i == 0:
                    return texts, f"text extraction failed on page 1: {type(exc).__name__}", n, False
        return texts, None, n, False
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on damaged files
        return None, f"{type(exc).__name__}: {exc}"[:200], None, False


def verify_pdf(path: str | Path, *, expected_pages: int | None = None, min_pages: int | None = None, expected_text: list[str] | None = None,
               source: str | None = None) -> dict[str, Any]:
    p = Path(path)
    checks = Checks()
    if not p.is_file():
        checks.failed("exists", f"{p} does not exist (export failed or went elsewhere)")
        return _result(p, "pdf", checks)
    size = p.stat().st_size
    head = p.read_bytes()[:1024] if size else b""
    tail = b""
    with p.open("rb") as fh:
        fh.seek(max(0, size - 8192))
        tail = fh.read()
    if size == 0 or b"%PDF-" not in head:
        checks.failed("valid_pdf", f"{size} bytes, no %PDF- header")
        return _result(p, "pdf", checks)
    checks.passed("exists", f"{size} bytes")
    (checks.passed if b"%%EOF" in tail else checks.failed)("complete_file", "ends with %%EOF" if b"%%EOF" in tail else "no %%EOF marker at the end: truncated or damaged")
    texts, problem, pages, encrypted = _pdf_text(p)
    if pages is None:
        guess = len(re.findall(rb"/Type\s*/Page[^s]", p.read_bytes())) if size < 100 * 1024 * 1024 else None
        checks.not_run("page_count", f"{problem}" + (f"; raw /Type /Page count suggests ~{guess} (unreliable)" if guess else ""))
        checks.not_verified.append("PDF text and page count" + (" (encrypted)" if encrypted else ""))
        return _result(p, "pdf", checks, encrypted=encrypted)
    if pages == 0:
        checks.failed("page_count", "the PDF has 0 pages")
    else:
        bad = []
        if expected_pages is not None and pages != expected_pages:
            bad.append(f"expected {expected_pages} pages")
        if min_pages is not None and pages < min_pages:
            bad.append(f"expected at least {min_pages} pages")
        (checks.failed if bad else checks.passed)("page_count", f"{pages} page(s)" + (": " + "; ".join(bad) if bad else ""), pages=pages)
    texts = texts or []
    empties = [i + 1 for i, t in enumerate(texts) if not t.strip()]
    if texts and len(empties) == len(texts):
        checks.warn("text_layer", "no page has a text layer (scanned or image-only PDF); OCR is not part of this check")
        checks.not_verified.append("PDF content (no text layer; OCR needed)")
    elif empties:
        checks.warn("text_layer", f"page(s) {_listing(empties, 10)} have no text (blank page, image or scan)")
    elif texts:
        checks.passed("text_layer", f"text extracted from all {len(texts)} checked page(s)")
    joined = _norm("\n".join(texts))
    if expected_text:
        miss = [t for t in expected_text if _norm(t) not in joined]
        (checks.failed if miss else checks.passed)("expected_text_present", f"missing: {_listing(miss)}" if miss else "all expected texts found in the PDF")
    if source:
        sp = Path(source)
        kind = filetype.detect(sp)["detected_type"] if sp.is_file() else None
        sample: list[str] = []
        if kind in ("docx", "docm"):
            zs = _open_zip(sp, Checks(), "source")
            if zs is not None:
                try:
                    m = parse_docx(zs)
                    cand = [_norm(x.text) for x in m["paragraphs"] if len(_norm(x.text)) >= 12]
                    sample = (cand[:6] + cand[-4:]) if len(cand) > 10 else cand
                except (KeyError, ET.ParseError):
                    pass
        if not sample:
            checks.not_run("source_text_in_pdf", f"source {source} is not a readable .docx (type {kind}); content comparison skipped")
        else:
            nospace = joined.replace(" ", "")
            miss = [s[:50] for s in sample if _norm(s)[:60].replace(" ", "") not in nospace]
            (checks.failed if miss else checks.passed)("source_text_in_pdf", f"{len(miss)} of {len(sample)} sampled source paragraphs missing from the PDF: {_listing(miss, 5)}" if miss
                                                      else f"{len(sample)} sampled paragraphs of the source (start and end) are present in the PDF")
    checks.not_verified.append("visual layout fidelity of the PDF versus the source (look at a rendering)")
    return _result(p, "pdf", checks, stats={"pages": pages, "size_bytes": size}, encrypted=encrypted)


# ============================================================================================= dispatcher
def verify_file(path: str | Path, **options: Any) -> dict[str, Any]:
    """Detect the REAL format of `path`, then run the matching verifier with `options` (unknown options are rejected)."""
    p = Path(path)
    info = filetype.detect(p)
    kind = info["detected_type"]
    checks = Checks()
    if info["status"] in ("missing", "directory", "unreadable", "empty"):
        checks.failed("exists", f"{p}: {info['status']}: {_listing(info['warnings'], 3)}")
        return _result(p, kind or "unknown", checks, detection=info)
    table = {"docx": verify_docx, "docm": verify_docx, "dotx": verify_docx, "xlsx": verify_xlsx, "xlsm": verify_xlsx, "xltx": verify_xlsx,
             "pptx": verify_pptx, "pptm": verify_pptx, "ppsx": verify_pptx, "pdf": verify_pdf, "csv": verify_csv, "tsv": verify_csv}
    fn = table.get(kind or "")
    if fn is None:
        checks.not_run("verifier", f"no verifier for detected type {kind!r} ({info['label']}): {info['handler'].get('reason') or 'no content checks exist'}")
        return _result(p, kind or "unknown", checks, detection=info)
    import inspect

    allowed = set(inspect.signature(fn).parameters) - {"path"}
    unknown = sorted(set(options) - allowed)
    if unknown:
        checks.failed("options", f"option(s) not valid for {kind}: {unknown}; valid: {sorted(allowed)}")
        return _result(p, kind, checks, detection=info)
    result = fn(p, **options)
    pre: list[dict[str, Any]] = []
    if info["extension_matches"] is False:
        pre.append({"name": "format_matches_extension", "status": "WARN", "detail": f"extension {info['extension']} but the content is {kind}", "evidence": {}})
    if info["macro_enabled"]:
        pre.append({"name": "macro_free", "status": "WARN", "detail": "macro-enabled format: macros were NOT executed and are not inspected", "evidence": {}})
    if info["status"] in ("corrupt", "encrypted", "oversized"):
        pre.append({"name": "container_integrity", "status": "FAIL" if info["status"] == "corrupt" else "WARN",
                    "detail": f"{info['status']}: {info.get('corrupt_reason') or _listing(info['warnings'], 2)}", "evidence": {}})
    if pre:
        result["checks"] = pre + result["checks"]
        c = Checks(items=result["checks"])
        result["verdict"] = c.verdict()
        result["counts"] = {s: sum(1 for x in result["checks"] if x["status"] == s) for s in ("PASS", "FAIL", "WARN", "NOT_RUN")}
    result["detection"] = {k: info[k] for k in ("detected_type", "label", "status", "extension_matches", "size_bytes", "macro_enabled", "encrypted")}
    return result
