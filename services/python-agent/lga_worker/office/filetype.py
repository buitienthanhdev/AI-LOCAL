"""Real file-type detection for Office files (spec section 5): by magic bytes and package content, never by extension.

`detect(path)` reads at most a few hundred KB (header, zip directory, OLE2 directory, a text head), reports the
format it found, whether the extension agrees, and whether the file looks encrypted / corrupt / oversized, and
routes it to the handlers that really exist in this repository (or to none, with the reason).
Nothing is opened with Office and nothing is executed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import struct
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

DEFAULT_SIZE_LIMIT = 250 * 1024 * 1024
HEAD_BYTES = 64 * 1024
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ONENOTE_MAGIC = bytes.fromhex("e4525c7b8cd8a74daeb15378d02996d3")
FREESECT = 0xFFFFFFFF
MAX_CHAIN = 20000

# type -> (family, label, extensions that are normal for it, macro_enabled)
TYPES: dict[str, tuple[str, str, tuple[str, ...], bool]] = {
    "docx": ("document", "Word document (OOXML)", (".docx",), False),
    "docm": ("document", "Word macro-enabled document", (".docm",), True),
    "dotx": ("document", "Word template", (".dotx",), False),
    "dotm": ("document", "Word macro-enabled template", (".dotm",), True),
    "xlsx": ("spreadsheet", "Excel workbook (OOXML)", (".xlsx",), False),
    "xlsm": ("spreadsheet", "Excel macro-enabled workbook", (".xlsm",), True),
    "xlsb": ("spreadsheet", "Excel binary workbook", (".xlsb",), True),
    "xltx": ("spreadsheet", "Excel template", (".xltx",), False),
    "xltm": ("spreadsheet", "Excel macro-enabled template", (".xltm",), True),
    "xlam": ("spreadsheet", "Excel add-in", (".xlam",), True),
    "pptx": ("presentation", "PowerPoint presentation (OOXML)", (".pptx",), False),
    "pptm": ("presentation", "PowerPoint macro-enabled presentation", (".pptm",), True),
    "ppsx": ("presentation", "PowerPoint slide show", (".ppsx",), False),
    "ppsm": ("presentation", "PowerPoint macro-enabled slide show", (".ppsm",), True),
    "potx": ("presentation", "PowerPoint template", (".potx",), False),
    "vsdx": ("diagram", "Visio drawing (OOXML)", (".vsdx", ".vstx"), False),
    "vsdm": ("diagram", "Visio macro-enabled drawing", (".vsdm", ".vstm"), True),
    "odt": ("document", "OpenDocument text", (".odt", ".ott"), False),
    "ods": ("spreadsheet", "OpenDocument spreadsheet", (".ods", ".ots"), False),
    "odp": ("presentation", "OpenDocument presentation", (".odp", ".otp"), False),
    "odg": ("diagram", "OpenDocument drawing", (".odg",), False),
    "xps": ("fixed", "XPS fixed document", (".xps", ".oxps"), False),
    "doc": ("document", "Word 97-2003 document (OLE2)", (".doc", ".dot"), False),
    "xls": ("spreadsheet", "Excel 97-2003 workbook (OLE2)", (".xls", ".xlt"), False),
    "ppt": ("presentation", "PowerPoint 97-2003 presentation (OLE2)", (".ppt", ".pps", ".pot"), False),
    "msg": ("mail", "Outlook message (OLE2)", (".msg",), False),
    "vsd": ("diagram", "Visio 97-2003 drawing (OLE2)", (".vsd",), False),
    "pub": ("document", "Publisher document (OLE2)", (".pub",), False),
    "mpp": ("project", "Microsoft Project plan (OLE2, heuristic)", (".mpp", ".mpt"), False),
    "ole2": ("unknown", "OLE2 compound file of an unknown kind", (), False),
    "ooxml_encrypted": ("document", "Password-protected Office file (encrypted OOXML package)",
                        (".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"), False),
    "accdb": ("database", "Access database (ACE engine)", (".accdb", ".accde"), False),
    "mdb": ("database", "Access database (Jet engine)", (".mdb", ".mde"), False),
    "onenote": ("notes", "OneNote section", (".one",), False),
    "pst": ("mail", "Outlook data file (PST/OST)", (".pst", ".ost"), False),
    "pdf": ("fixed", "PDF document", (".pdf",), False),
    "rtf": ("document", "Rich Text Format", (".rtf",), False),
    "eml": ("mail", "E-mail message (RFC 822)", (".eml",), False),
    "ics": ("calendar", "iCalendar", (".ics", ".ical"), False),
    "csv": ("spreadsheet", "Comma-separated values", (".csv",), False),
    "tsv": ("spreadsheet", "Tab-separated values", (".tsv", ".tab"), False),
    "json": ("data", "JSON", (".json",), False),
    "xml": ("data", "XML", (".xml",), False),
    "html": ("data", "HTML", (".html", ".htm"), False),
    "text": ("text", "Plain text", (".txt", ".md", ".log", ".text"), False),
    "zip": ("archive", "ZIP archive", (".zip",), False),
    "sevenzip": ("archive", "7-Zip archive", (".7z",), False),
    "rar": ("archive", "RAR archive", (".rar",), False),
    "gzip": ("archive", "GZip archive", (".gz", ".tgz"), False),
    "binary": ("unknown", "Unrecognised binary data", (), False),
}

# What exists in this repo for each type: real tool names only. Empty read list = no handler (honest).
ROUTES: dict[str, dict[str, Any]] = {
    "docx": {"read": ["office.word_read", "document.read"], "edit": ["office.word_write"], "library": "python-docx / zip+XML",
             "verify": "office.verify"},
    "docm": {"read": ["office.word_read"], "edit": ["office.word_write"], "library": "Word COM only (macros: do NOT auto-run)",
             "verify": "office.verify"},
    "dotx": {"read": ["office.word_read"], "edit": [], "library": "Word COM", "verify": None},
    "doc": {"read": ["office.word_read"], "edit": ["office.word_write"], "library": "Word COM (no pure-Python .doc parser installed)",
            "verify": None},
    "odt": {"read": ["office.word_read"], "edit": [], "library": "Word COM (import)", "verify": None},
    "rtf": {"read": ["office.word_read"], "edit": [], "library": "Word COM", "verify": None},
    "xlsx": {"read": ["office.excel_read", "document.read", "data.aggregate"], "edit": ["office.excel_write"],
             "library": "Excel COM / zip+XML (openpyxl not required for office.verify)", "verify": "office.verify"},
    "xlsm": {"read": ["office.excel_read"], "edit": ["office.excel_write"], "library": "Excel COM only (macros: do NOT auto-run)",
             "verify": "office.verify"},
    "xlsb": {"read": ["office.excel_read"], "edit": [], "library": "Excel COM", "verify": None},
    "xls": {"read": ["office.excel_read"], "edit": ["office.excel_write"], "library": "Excel COM (no xlrd installed)", "verify": None},
    "ods": {"read": ["office.excel_read"], "edit": [], "library": "Excel COM (import)", "verify": None},
    "csv": {"read": ["document.read", "data.aggregate"], "edit": ["office.excel_write"], "library": "csv module", "verify": "office.verify"},
    "tsv": {"read": ["document.read", "data.aggregate"], "edit": [], "library": "csv module", "verify": "office.verify"},
    "pptx": {"read": ["document.read"], "edit": [], "library": "zip+XML (python-pptx optional); PowerPoint COM edit not exposed as a tool",
             "verify": "office.verify"},
    "pptm": {"read": ["document.read"], "edit": [], "library": "zip+XML; macros: do NOT auto-run", "verify": "office.verify"},
    "ppsx": {"read": ["document.read"], "edit": [], "library": "zip+XML", "verify": "office.verify"},
    "ppt": {"read": [], "edit": [], "library": "no parser installed (PowerPoint COM could open it; no tool for it)", "verify": None},
    "odp": {"read": [], "edit": [], "library": "no parser installed", "verify": None},
    "pdf": {"read": ["document.read"], "edit": [], "library": "pypdf", "verify": "office.verify", "convert": ["office.export_pdf"]},
    "xps": {"read": [], "edit": [], "library": "no parser installed", "verify": None},
    "msg": {"read": [], "edit": [], "library": "needs extract_msg or Outlook COM (not wired to a file tool)", "verify": None},
    "eml": {"read": ["document.read"], "edit": [], "library": "email stdlib", "verify": None},
    "ics": {"read": ["document.read"], "edit": [], "library": "plain text", "verify": None},
    "pst": {"read": [], "edit": [], "library": "needs a PST parser or Outlook profile; none installed", "verify": None},
    "accdb": {"read": [], "edit": [], "library": "needs Access Database Engine (ACE) + pyodbc; not verified here", "verify": None},
    "mdb": {"read": [], "edit": [], "library": "needs Jet/ACE driver + pyodbc; not verified here", "verify": None},
    "vsdx": {"read": [], "edit": [], "library": "zip+XML possible; no tool yet", "verify": None},
    "vsd": {"read": [], "edit": [], "library": "no parser installed", "verify": None},
    "pub": {"read": [], "edit": [], "library": "Publisher desktop only", "verify": None},
    "mpp": {"read": [], "edit": [], "library": "needs MPXJ or Project; none installed", "verify": None},
    "onenote": {"read": [], "edit": [], "library": "OneNote desktop/Graph only", "verify": None},
    "json": {"read": ["document.read"], "edit": [], "library": "json", "verify": None},
    "xml": {"read": ["document.read"], "edit": [], "library": "xml.etree", "verify": None},
    "html": {"read": ["document.read"], "edit": [], "library": "text", "verify": None},
    "text": {"read": ["document.read"], "edit": [], "library": "text", "verify": None},
    "zip": {"read": [], "edit": [], "library": "zipfile: list members, then detect each member separately", "verify": None},
    "ooxml_encrypted": {"read": [], "edit": [], "library": "password required; the agent does not guess or break passwords", "verify": None},
}

CT_MAP: tuple[tuple[str, str], ...] = (
    ("wordprocessingml.document.macroenabled.main+xml", "docm"),
    ("wordprocessingml.template.macroenabled.main+xml", "dotm"),
    ("wordprocessingml.template.main+xml", "dotx"),
    ("wordprocessingml.document.main+xml", "docx"),
    ("ms-word.document.macroenabled.main+xml", "docm"),
    ("ms-word.template.macroenabled.main+xml", "dotm"),
    ("ms-excel.sheet.binary.macroenabled.main", "xlsb"),
    ("ms-excel.sheet.macroenabled.main+xml", "xlsm"),
    ("ms-excel.template.macroenabled.main+xml", "xltm"),
    ("ms-excel.addin.macroenabled.main+xml", "xlam"),
    ("spreadsheetml.template.main+xml", "xltx"),
    ("spreadsheetml.sheet.main+xml", "xlsx"),
    ("ms-powerpoint.presentation.macroenabled.main+xml", "pptm"),
    ("ms-powerpoint.slideshow.macroenabled.main+xml", "ppsm"),
    ("presentationml.slideshow.main+xml", "ppsx"),
    ("presentationml.template.main+xml", "potx"),
    ("presentationml.presentation.main+xml", "pptx"),
    ("ms-visio.drawing.macroenabled.main+xml", "vsdm"),
    ("ms-visio.drawing.main+xml", "vsdx"),
    ("ms-visio.template.main+xml", "vsdx"),
)
ODF_MIME = {"application/vnd.oasis.opendocument.text": "odt", "application/vnd.oasis.opendocument.spreadsheet": "ods",
            "application/vnd.oasis.opendocument.presentation": "odp", "application/vnd.oasis.opendocument.graphics": "odg"}
EXEC_EXTENSIONS = {".exe", ".dll", ".bat", ".cmd", ".com", ".scr", ".msi", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".jar", ".lnk",
                   ".hta", ".cpl", ".reg", ".pif", ".sh"}


# --------------------------------------------------------------------------------------------- OLE2
def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def ole_entry_names(fh: Any, size: int) -> list[str]:
    """Names of the directory entries of an OLE2 compound file. Raises ValueError when the structure is damaged."""
    fh.seek(0)
    hdr = fh.read(512)
    if len(hdr) < 512 or hdr[:8] != OLE_MAGIC:
        raise ValueError("not an OLE2 header")
    shift = _u16(hdr, 0x1E)
    if shift not in (9, 12):
        raise ValueError(f"bad OLE2 sector shift {shift}")
    sec = 1 << shift
    n_fat, dir_start, first_difat, n_difat = _u32(hdr, 0x2C), _u32(hdr, 0x30), _u32(hdr, 0x44), _u32(hdr, 0x48)
    if n_fat == 0 or n_fat > size // sec + 1:
        raise ValueError("bad OLE2 FAT size")
    difat = [x for x in struct.unpack_from("<109I", hdr, 0x4C) if x != FREESECT]
    s, hops = first_difat, 0
    while n_difat and s < 0xFFFFFFFA and hops < n_difat and hops < MAX_CHAIN:
        off = (s + 1) * sec
        if off + sec > size:
            raise ValueError("DIFAT sector beyond end of file (truncated)")
        fh.seek(off)
        block = fh.read(sec)
        vals = struct.unpack_from(f"<{sec // 4}I", block, 0)
        difat.extend(v for v in vals[:-1] if v != FREESECT)
        s, hops = vals[-1], hops + 1
    fat: list[int] = []
    for fs in difat[:n_fat]:
        off = (fs + 1) * sec
        if off + sec > size:
            raise ValueError("FAT sector beyond end of file (truncated)")
        fh.seek(off)
        fat.extend(struct.unpack_from(f"<{sec // 4}I", fh.read(sec), 0))
    names: list[str] = []
    s, hops = dir_start, 0
    while s < 0xFFFFFFFA and hops < MAX_CHAIN:
        off = (s + 1) * sec
        if off + sec > size:
            raise ValueError("directory sector beyond end of file (truncated)")
        fh.seek(off)
        data = fh.read(sec)
        for i in range(0, len(data) - 127, 128):
            ent = data[i:i + 128]
            n = _u16(ent, 64)
            if ent[66] in (1, 2, 5) and 2 <= n <= 64:
                names.append(ent[:n - 2].decode("utf-16-le", errors="replace"))
        if s >= len(fat):
            raise ValueError("directory chain leaves the FAT")
        s, hops = fat[s], hops + 1
    if not names:
        raise ValueError("OLE2 directory is empty")
    return names


def classify_ole(names: list[str]) -> tuple[str, str]:
    """(type, confidence) from the storage/stream names."""
    low = set(names)
    if "EncryptedPackage" in low and "EncryptionInfo" in low:
        return "ooxml_encrypted", "structure"
    if "WordDocument" in low:
        return "doc", "structure"
    if "Workbook" in low or "Book" in low:
        return "xls", "structure"
    if "PowerPoint Document" in low:
        return "ppt", "structure"
    if "VisioDocument" in low:
        return "vsd", "structure"
    if "__properties_version1.0" in low or any(n.startswith("__substg1.0_") for n in names):
        return "msg", "structure"
    if "Quill" in low:
        return "pub", "structure"
    if any(re.fullmatch(r"Props\d*", n) for n in names) and any("TBkndTask" in n or "Props" in n for n in names):
        return "mpp", "heuristic"
    return "ole2", "structure"


# --------------------------------------------------------------------------------------------- ZIP
def _content_types(z: zipfile.ZipFile) -> tuple[dict[str, str], list[str]]:
    """(content type -> first part name, all override content types)."""
    try:
        info = z.getinfo("[Content_Types].xml")
    except KeyError:
        return {}, []
    if info.file_size > 2_000_000:
        return {}, []
    root = ET.fromstring(z.read(info))
    by_type: dict[str, str] = {}
    for el in root:
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Override" and el.get("ContentType") and el.get("PartName"):
            by_type.setdefault(el.get("ContentType", "").lower(), el.get("PartName", ""))
    return by_type, list(by_type)


def _analyse_zip(path: Path, out: dict[str, Any]) -> str:
    """Fills `out` and returns the detected type for a file that starts with PK."""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        out["zip_members"] = len(names)
        total = sum(i.file_size for i in infos)
        packed = sum(i.compress_size for i in infos) or 1
        if total > 2 * 1024**3 or (total > 100 * 1024**2 and total / packed > 200):
            out["warnings"].append(f"suspicious compression: {total} bytes unpacked from {packed} (possible zip bomb); not extracted")
        if any(i.flag_bits & 0x1 for i in infos):
            out["encrypted"] = True
            out["evidence"].append("zip member(s) flagged encrypted")
        if "mimetype" in names:
            mime = z.read("mimetype")[:100].decode("ascii", "ignore").strip()
            if mime in ODF_MIME:
                out["evidence"].append(f"mimetype entry: {mime}")
                return ODF_MIME[mime]
        if any(n.endswith("FixedDocumentSequence.fdseq") or n.endswith("FixedDocSeq.fdseq") for n in names):
            out["evidence"].append("XPS FixedDocumentSequence part")
            return "xps"
        by_type, _ = _content_types(z) if "[Content_Types].xml" in names else ({}, [])
        if by_type:
            for needle, kind in CT_MAP:
                for ctype, part in by_type.items():
                    if ctype.endswith(needle) or needle in ctype:
                        main = part.lstrip("/")
                        out["evidence"].append(f"content type {ctype} -> {kind}")
                        if main not in names:
                            out["corrupt"] = True
                            out["corrupt_reason"] = f"main part {part} named in [Content_Types].xml is missing from the package"
                        if any(n.lower().endswith("vbaproject.bin") for n in names):
                            out["has_vba_project"] = True
                        return kind
        # No (usable) content-types: fall back to the part layout and say so.
        layout = (("word/document.xml", "docx"), ("xl/workbook.xml", "xlsx"), ("ppt/presentation.xml", "pptx"), ("visio/document.xml", "vsdx"))
        for part, kind in layout:
            if part in names:
                out["corrupt"] = True
                out["corrupt_reason"] = "[Content_Types].xml missing or unusable; type guessed from part layout"
                out["evidence"].append(f"part {part} present")
                return kind
        office = [n for n in names if Path(n).suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls", ".ppt", ".msg", ".eml"}]
        out["archive_office_members"] = office[:50]
        if any(Path(n).suffix.lower() in EXEC_EXTENSIONS for n in names):
            out["warnings"].append("archive contains executable/script members; never run them automatically")
        return "zip"


# --------------------------------------------------------------------------------------------- text
def _decode_head(head: bytes) -> str | None:
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
        enc = "utf-8"
    elif head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return head.decode("utf-16")
        except UnicodeDecodeError:
            return None
    else:
        enc = "utf-8"
    if b"\x00" in head:
        return None
    try:
        return head.decode(enc)
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 4:  # a multi-byte character cut by the read size
            return head[:exc.start].decode(enc)
        try:
            text = head.decode("cp1252")
        except UnicodeDecodeError:
            return None
        return text


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t\f")
    return bad / len(text)


def _sniff_delimited(text: str) -> str | None:
    lines = [ln for ln in text.splitlines() if ln.strip()][:60]
    if len(lines) < 2:
        return None
    best: tuple[float, str] | None = None
    for delim, kind in ((",", "csv"), (";", "csv"), ("\t", "tsv"), ("|", "csv")):
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        except csv.Error:
            continue
        counts = [len(r) for r in rows if r]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        share = counts.count(mode) / len(counts)
        if mode >= 2 and share >= 0.8:
            score = share * mode
            if best is None or score > best[0]:
                best = (score, kind)
    return best[1] if best else None


_EML_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,40}:[ \t]")
_EML_KEYS = ("from:", "to:", "subject:", "date:", "message-id:", "mime-version:", "received:", "content-type:")


def _looks_eml(text: str) -> bool:
    header_lines: list[str] = []
    for ln in text.splitlines():
        if not ln.strip():
            break
        if ln[0] in " \t":
            continue
        if not _EML_HEADER.match(ln):
            return False
        header_lines.append(ln.lower())
    keys = sum(1 for ln in header_lines if ln.startswith(_EML_KEYS))
    return len(header_lines) >= 3 and keys >= 2


def _analyse_text(path: Path, size: int, text: str, out: dict[str, Any]) -> str:
    stripped = text.lstrip()
    low = stripped[:512].lower()
    if stripped.startswith("BEGIN:VCALENDAR"):
        return "ics"
    if stripped.startswith("{\\rtf"):
        return "rtf"
    if _looks_eml(stripped):
        return "eml"
    if low.startswith("<!doctype html") or low.startswith("<html") or "<html" in low[:200]:
        return "html"
    if stripped.startswith(("{", "[")):
        if size <= 5 * 1024 * 1024:
            try:
                json.loads(path.read_text(encoding="utf-8-sig"))
                out["evidence"].append("parsed as JSON")
                return "json"
            except (ValueError, OSError):
                pass
        else:
            out["warnings"].append("large file looks like JSON; syntax not verified")
            return "json"
    if stripped.startswith("<"):
        if size <= 5 * 1024 * 1024:
            try:
                ET.fromstring(path.read_bytes())
                out["evidence"].append("parsed as XML")
                return "xml"
            except (ET.ParseError, OSError):
                pass
        elif stripped.startswith("<?xml"):
            out["warnings"].append("large file looks like XML; well-formedness not verified")
            return "xml"
    delim = _sniff_delimited(text)
    if delim:
        out["evidence"].append("consistent column count across the first lines")
        return delim
    return "text"


# --------------------------------------------------------------------------------------------- public
def _blank(path: Path) -> dict[str, Any]:
    return {"path": str(path), "name": path.name, "extension": path.suffix.lower(), "exists": False, "is_file": False, "size_bytes": 0,
            "detected_type": None, "family": None, "label": None, "confidence": None, "extension_matches": None,
            "expected_extensions": [], "macro_enabled": False, "encrypted": False, "corrupt": False, "corrupt_reason": None,
            "oversized": False, "size_limit_bytes": DEFAULT_SIZE_LIMIT, "empty": False, "handler": {}, "warnings": [], "evidence": [],
            "status": "missing"}


def detect(path: str | Path, size_limit: int = DEFAULT_SIZE_LIMIT) -> dict[str, Any]:
    """Real format of `path`. `status` is one of ok, missing, directory, unreadable, empty, corrupt, encrypted, oversized, unknown."""
    p = Path(path)
    out = _blank(p)
    out["size_limit_bytes"] = size_limit
    try:
        if not p.exists():
            out["status"] = "missing"
            out["warnings"].append(f"{p} does not exist")
            return out
        out["exists"] = True
        if p.is_dir():
            out["status"] = "directory"
            out["detected_type"] = "directory"
            out["family"] = "folder"
            return out
        out["is_file"] = True
        size = p.stat().st_size
        out["size_bytes"] = size
        with p.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
            if size == 0:
                out["empty"] = True
                out["status"] = "empty"
                out["warnings"].append("file is empty (0 bytes)")
                return out
            kind, confidence = _sniff(p, fh, head, size, out)
    except PermissionError as exc:
        out["status"] = "unreadable"
        out["warnings"].append(f"no permission to read the file: {exc}")
        return out
    except OSError as exc:
        out["status"] = "unreadable"
        out["warnings"].append(f"cannot read the file: {type(exc).__name__}: {exc}")
        return out
    family, label, exts, macro = TYPES[kind]
    out.update(detected_type=kind, family=family, label=label, confidence=confidence, expected_extensions=list(exts),
               macro_enabled=macro or bool(out.pop("has_vba_project", False)))
    ext = out["extension"]
    if exts:
        out["extension_matches"] = ext in exts
        if ext and ext not in exts:
            out["warnings"].append(f"extension {ext} does not match the real format {kind}; the content decides, not the name")
    if kind == "ooxml_encrypted":
        out["encrypted"] = True
    if kind == "zip" and ext in {".docx", ".xlsx", ".pptx"}:
        out["corrupt"], out["corrupt_reason"] = True, f"named {ext} but it is a plain ZIP without an Office main part"
    if kind in ("text", "binary") and ext in {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls", ".ppt"}:
        out["corrupt"], out["corrupt_reason"] = True, f"named {ext} but the content is {kind}, not an Office/PDF file"
    if size > size_limit:
        out["oversized"] = True
        out["warnings"].append(f"{size} bytes exceeds the {size_limit} byte limit; will not be loaded into memory")
    if out["macro_enabled"]:
        out["warnings"].append("macro-enabled format: macros must never run automatically; opening it in the desktop app is a level 4 action")
    out["handler"] = route_for(kind)
    if out["corrupt"]:
        out["status"] = "corrupt"
    elif out["encrypted"]:
        out["status"] = "encrypted"
    elif out["oversized"]:
        out["status"] = "oversized"
    elif kind in ("binary", "ole2"):
        out["status"] = "unknown"
    else:
        out["status"] = "ok"
    return out


def _sniff(p: Path, fh: Any, head: bytes, size: int, out: dict[str, Any]) -> tuple[str, str]:
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            return _analyse_zip(p, out), "package"
        except zipfile.BadZipFile as exc:
            out["corrupt"], out["corrupt_reason"] = True, f"ZIP structure damaged: {exc}"
            return "zip", "magic"
        except (ET.ParseError, KeyError, RuntimeError, NotImplementedError, OSError) as exc:
            out["corrupt"], out["corrupt_reason"] = True, f"package unreadable: {type(exc).__name__}: {exc}"[:200]
            return "zip", "magic"
    if head.startswith(OLE_MAGIC):
        try:
            names = ole_entry_names(fh, size)
        except (ValueError, struct.error) as exc:
            out["corrupt"], out["corrupt_reason"] = True, f"OLE2 container damaged: {exc}"
            return "ole2", "magic"
        kind, conf = classify_ole(names)
        out["evidence"].append(f"OLE2 streams: {', '.join(names[:8])}")
        if kind == "ooxml_encrypted":
            out["encrypted"] = True
        return kind, conf
    lead = head[:1024]
    idx = lead.find(b"%PDF-")
    if idx >= 0:
        out["evidence"].append(f"%PDF- header at byte {idx}")
        if idx:
            out["warnings"].append(f"{idx} bytes of junk before the PDF header")
        tail_start = max(0, size - 8192)
        fh.seek(tail_start)
        tail = fh.read()
        if b"%%EOF" not in tail:
            out["corrupt"], out["corrupt_reason"] = True, "no %%EOF marker in the last 8 KB (truncated or damaged; a reader may still repair it)"
        if b"/Encrypt" in head or b"/Encrypt" in tail:
            out["encrypted"] = True
            out["evidence"].append("/Encrypt entry in the PDF trailer (password or owner restrictions)")
        m = re.match(rb"%PDF-(\d\.\d)", lead[idx:])
        if m:
            out["pdf_version"] = m.group(1).decode()
        return "pdf", "magic"
    if head[:4] == b"\x00\x01\x00\x00" and head[4:19] == b"Standard ACE DB":
        return "accdb", "magic"
    if head[:4] == b"\x00\x01\x00\x00" and head[4:19] == b"Standard Jet DB":
        return "mdb", "magic"
    if head.startswith(ONENOTE_MAGIC):
        return "onenote", "magic"
    if head.startswith(b"!BDN"):
        return "pst", "magic"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "sevenzip", "magic"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar", "magic"
    if head.startswith(b"\x1f\x8b"):
        return "gzip", "magic"
    text = _decode_head(head)
    if text is not None and _control_ratio(text) < 0.05:
        return _analyse_text(p, size, text, out), "content"
    return "binary", "content"


def route_for(kind: str) -> dict[str, Any]:
    r = ROUTES.get(kind)
    if r is None:
        return {"read": [], "edit": [], "library": None, "verify": None, "reason": f"no handler registered for {kind}"}
    out = dict(r)
    if not r.get("read"):
        out["reason"] = f"no read handler exists in this repository for {kind}: {r.get('library')}"
    return out


def inspect_zip_members(path: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    """Detect each Office-looking member of an archive WITHOUT extracting to the user's folders (reads into memory, capped)."""
    result: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist()[:limit]:
            if info.is_dir():
                continue
            entry: dict[str, Any] = {"member": info.filename, "size_bytes": info.file_size, "encrypted": bool(info.flag_bits & 1)}
            if info.file_size and info.file_size <= 32 * 1024 * 1024 and not entry["encrypted"]:
                data = z.read(info)
                entry["magic"] = ("pdf" if data[:5] == b"%PDF-" else "zip-or-ooxml" if data[:2] == b"PK" else "ole2" if data[:8] == OLE_MAGIC else "other")
            result.append(entry)
    return result
