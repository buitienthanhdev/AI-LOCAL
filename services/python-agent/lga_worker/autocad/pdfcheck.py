"""PDF checks for plotted sheet sets (spec 13 and 18): page count, page size, rendered image not blank, text layer.

The worker venv has no PDF renderer, so the work is done by PyMuPDF in a *system* Python started as a subprocess. The
script lives in its own empty temporary folder, which is also the working directory, so nothing else can be imported
from there (`-I` is not used: it would hide the user site-packages where PyMuPDF is usually installed). When no interpreter with PyMuPDF is found the
functions say so; callers must report that the PDF was NOT rendered, never assume it was fine.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_S = 30
RENDER_TIMEOUT_S = 600

RENDER_SCRIPT = r'''
import json, sys
import pymupdf

pdf, out_dir, dpi, spec_json = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
spec = json.loads(spec_json)
doc = pymupdf.open(pdf)
pages = []
for i, page in enumerate(doc):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    samples = pix.samples
    n = len(samples)
    white = samples.count(255) if n else 0
    near_white = sum(1 for b in samples if b >= 245)
    text = page.get_text()
    png = None
    if out_dir:
        png = out_dir + "/page_%02d.png" % (i + 1)
        pix.save(png)
    expected = spec[i] if i < len(spec) else {}
    code = expected.get("code")
    pages.append({
        "page": i + 1, "size_mm": [round(page.rect.width / 72 * 25.4, 1), round(page.rect.height / 72 * 25.4, 1)],
        "pixels": [pix.width, pix.height], "nonwhite_ratio": round(1 - near_white / n, 6) if n else 0.0,
        "vector_paths": len(page.get_drawings()), "text_chars": len(text), "text_head": text[:200],
        "sheet_code": code, "codes_found": [c["code"] for c in spec if c.get("code") and c["code"] in text],
        "paper_mm": expected.get("paper_mm"), "png": png,
    })
print(json.dumps({"pymupdf": pymupdf.__doc__.split(chr(10))[0] if pymupdf.__doc__ else "pymupdf",
                  "page_count": len(doc), "pages": pages}))
'''

MERGE_SCRIPT = r'''
import json, sys
import pymupdf

out, parts = sys.argv[1], json.loads(sys.argv[2])
merged = pymupdf.open()
for p in parts:
    src = pymupdf.open(p)
    merged.insert_pdf(src)
    src.close()
merged.save(out)
print(json.dumps({"pages": len(merged)}))
'''


def _candidates(configured: str | None) -> list[str]:
    found: list[str] = []
    if configured:
        found.append(configured)
    found += sorted(glob.glob(r"C:\Python3*\python.exe"), reverse=True)
    which = shutil.which("python")
    if which:
        found.append(which)
    found += sorted(glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe")), reverse=True)
    own = os.path.normcase(os.path.abspath(sys.executable))
    out: list[str] = []
    for c in found:
        if os.path.normcase(os.path.abspath(c)) != own and c not in out and os.path.isfile(c):
            out.append(c)
    return out


def find_system_python(configured: str | None = None) -> str | None:
    """A Python (not this venv's) that can `import pymupdf`, or None."""
    for cand in _candidates(configured):
        try:
            with tempfile.TemporaryDirectory(prefix="lga_pdfprobe_") as empty:
                run = subprocess.run([cand, "-c", "import pymupdf"], capture_output=True, text=True,
                                     timeout=PROBE_TIMEOUT_S, cwd=empty)
        except (OSError, subprocess.SubprocessError):
            continue
        if run.returncode == 0:
            return cand
    return None


def _run_script(python: str, script: str, args: Sequence[str], timeout: float) -> tuple[dict[str, Any] | None, str]:
    work = Path(tempfile.mkdtemp(prefix="lga_pdfcheck_"))  # its own folder: nothing else is on the interpreter's path
    path = work / "probe.py"
    path.write_text(script, encoding="utf-8")
    try:
        run = subprocess.run([python, str(path), *args], capture_output=True, text=True, timeout=timeout, cwd=str(work))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if run.returncode != 0:
        return None, run.stderr.strip()[-400:] or f"exit code {run.returncode}"
    try:
        return json.loads(run.stdout.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, f"unreadable output: {run.stdout[-200:]!r}"


def render_check(pdf: Path, sheets: Sequence[Mapping[str, Any]], *, out_dir: Path | None = None, dpi: int = 50,
                 python: str | None = None, min_nonwhite: float = 0.0005, size_tol_mm: float = 2.0) -> dict[str, Any]:
    """Render every page of `pdf` and check it against the sheet list [{code, paper_mm}] in plotting order.

    Returns {"ok", "rendered", "note", "pages", "expected_pages", "page_checks": [...], ...}; `rendered` is False when no
    renderer was available (then nothing about the content is known and `ok` is False).
    """
    result: dict[str, Any] = {"ok": False, "rendered": False, "expected_pages": len(sheets), "pdf": str(pdf),
                              "page_checks": [], "note": ""}
    if not pdf.is_file() or pdf.stat().st_size == 0:
        result["note"] = f"{pdf} does not exist or is empty"
        return result
    python = python or find_system_python(None)
    if python is None:
        result["note"] = ("no Python with PyMuPDF was found (system Python); the PDF was NOT rendered or inspected. "
                          "Install pymupdf in a system Python to enable the check.")
        return result
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    spec = [{"code": s.get("code"), "paper_mm": s.get("paper_mm")} for s in sheets]
    data, err = _run_script(python, RENDER_SCRIPT, [str(pdf), str(out_dir) if out_dir else "", str(dpi), json.dumps(spec)],
                            RENDER_TIMEOUT_S)
    if data is None:
        result["note"] = f"the renderer failed: {err}"
        return result
    result.update({"rendered": True, "renderer": f"{data['pymupdf']} via {python}", "pages": data["page_count"],
                   "page_checks": data["pages"]})
    problems: list[str] = []
    if data["page_count"] != len(sheets):
        problems.append(f"{data['page_count']} pages for {len(sheets)} sheets")
    for page in data["pages"]:
        n = page["page"]
        if page["nonwhite_ratio"] < min_nonwhite:
            problems.append(f"page {n} is blank")
        if not page["text_chars"]:
            problems.append(f"page {n} has no text")
        if page["sheet_code"] and page["sheet_code"] not in page["codes_found"]:
            problems.append(f"page {n}: sheet code {page['sheet_code']} not in its text")
        want = page.get("paper_mm")
        if want:
            got = page["size_mm"]
            if abs(got[0] - want[0]) > size_tol_mm or abs(got[1] - want[1]) > size_tol_mm:
                problems.append(f"page {n} is {got[0]:g} x {got[1]:g} mm, layout paper is {want[0]:g} x {want[1]:g} mm")
    result["problems"] = problems
    result["ok"] = not problems
    result["note"] = (f"{data['page_count']} pages rendered at {dpi} dpi; " + ("; ".join(problems) if problems else
                      "all non-blank with text, sheet codes found, sizes match the layouts"))
    return result


def merge_pdfs(parts: Sequence[Path], out: Path, python: str | None = None) -> dict[str, Any]:
    """Merge single-page PDFs into one multi-page PDF (order kept). {"ok", "pages", "note"}."""
    python = python or find_system_python(None)
    if python is None:
        return {"ok": False, "pages": 0, "note": "no Python with PyMuPDF was found: the PDFs could not be merged"}
    data, err = _run_script(python, MERGE_SCRIPT, [str(out), json.dumps([str(p) for p in parts])], RENDER_TIMEOUT_S)
    if data is None:
        return {"ok": False, "pages": 0, "note": f"merge failed: {err}"}
    return {"ok": True, "pages": data["pages"], "note": f"merged {len(parts)} PDFs with PyMuPDF ({python})"}
