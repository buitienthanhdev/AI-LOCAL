"""file.info (contract: tools/documents.json): where a file is, what it is, who made it, when it first appeared.

Definitions the person gave (and the tool reports under these names):
* authors   = the file's own "Authors" property (Office core.xml dc:creator, PDF /Author, image Artist), which is what
              Explorer's details pane shows; `last_saved_by` is a different person.
* first_seen = the day the file first appeared: the earliest of the file-system creation time and the first time the
              company catalog / file-event log saw it. This is the "ngày tạo" the person means.
"""

from __future__ import annotations

import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paths import absolute_path
from lga_worker.registry import ToolContext, ToolOutcome, ToolRegistry

BACKEND = "fileinfo"
NS = {"dc": "http://purl.org/dc/elements/1.1/", "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties/",
      "dcterms": "http://purl.org/dc/terms/", "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"}
OFFICE = {".docx", ".xlsx", ".pptx", ".dotx", ".xlsm", ".docm", ".pptm"}


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _office(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with zipfile.ZipFile(path) as z:
        if "docProps/core.xml" in z.namelist():
            root = ET.fromstring(z.read("docProps/core.xml"))
            get = lambda tag: (root.findtext(tag, namespaces=NS) or "").strip() or None  # noqa: E731
            out.update(authors=get("dc:creator"), last_saved_by=get("cp:lastModifiedBy"), title=get("dc:title"),
                       content_created=get("dcterms:created"), date_last_saved=get("dcterms:modified"))
        if "docProps/app.xml" in z.namelist():
            root = ET.fromstring(z.read("docProps/app.xml"))
            for key, tag in (("pages", "ep:Pages"), ("words", "ep:Words"), ("total_editing_minutes", "ep:TotalTime"),
                             ("application", "ep:Application"), ("company", "ep:Company")):
                value = root.findtext(tag, namespaces=NS)
                if value:
                    out[key] = value.strip()
    return out


def _pdf(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        meta = reader.metadata or {}
        out.update(authors=(str(meta.get("/Author") or "").strip() or None), title=(str(meta.get("/Title") or "").strip() or None),
                   content_created=str(meta.get("/CreationDate") or "") or None, pages=len(reader.pages),
                   application=str(meta.get("/Producer") or "") or None)
    except Exception as exc:  # noqa: BLE001 - encrypted or damaged
        out["metadata_error"] = f"{type(exc).__name__}: {exc}"[:120]
    return out


def _image(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        from PIL import Image

        with Image.open(path) as im:
            out["dimensions"] = [im.width, im.height]
            exif = im.getexif()
            if exif:
                out["authors"] = (str(exif.get(0x013B) or "").strip() or None)
                out["content_created"] = str(exif.get(0x9003) or exif.get(0x0132) or "") or None
    except Exception as exc:  # noqa: BLE001
        out["metadata_error"] = f"{type(exc).__name__}: {exc}"[:120]
    return out


def _owner(path: Path) -> str | None:
    try:
        import win32security

        sd = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
        name, domain, _ = win32security.LookupAccountSid(None, sd.GetSecurityDescriptorOwner())
        return f"{domain}\\{name}"
    except Exception:  # noqa: BLE001 - network shares, protected files
        return None


def _catalog(ctx: ToolContext, path: Path) -> dict[str, Any]:
    db = ctx.config.root / "services" / "company-brain" / "data" / "catalog.db"
    if not db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM files WHERE lower(path) = lower(?) ORDER BY status = 'active' DESC LIMIT 1",
                           (str(path),)).fetchone()
        if row is None:
            return {}
        out = {k: row[k] for k in ("doc_author", "doc_last_editor", "doc_created_at", "fs_created_at", "fs_modified_at", "fs_owner",
                                   "first_seen_at", "last_seen_at", "department", "project", "status", "title", "version_no")}
        ev = conn.execute("SELECT MIN(at) a FROM file_events WHERE file_id = ?", (row["file_id"],)).fetchone()
        out["first_event_at"] = ev["a"] if ev else None
        conn.close()
        return out
    except sqlite3.Error:
        return {}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", v)
    try:
        if m:
            return datetime(*(int(x) for x in m.groups(default="0")[:3]), *(int(x) for x in m.groups(default="0")[3:6])).astimezone()
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except (ValueError, TypeError):
        return None


def register(registry: ToolRegistry) -> None:
    @registry.handler("file.info")
    async def info(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = absolute_path(args["path"])
        if not path.exists():
            raise ToolError(ErrorCode.NOT_FOUND, f"không có tệp {path}")
        st = path.stat()
        data: dict[str, Any] = {"path": str(path), "name": path.name, "folder": str(path.parent), "is_dir": path.is_dir(),
                                "size_bytes": st.st_size, "fs_created": _iso(st.st_ctime), "fs_modified": _iso(st.st_mtime),
                                "fs_accessed": _iso(st.st_atime), "owner": _owner(path)}
        ext = path.suffix.lower()
        try:
            if ext in OFFICE:
                data.update(_office(path))
            elif ext == ".pdf":
                data.update(_pdf(path))
            elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}:
                data.update(_image(path))
        except (zipfile.BadZipFile, OSError, ET.ParseError) as exc:
            data["metadata_error"] = f"{type(exc).__name__}: {exc}"[:120]
        cat = _catalog(ctx, path)
        if cat:
            data["catalog"] = cat
            data.setdefault("authors", cat.get("doc_author"))
            if not data.get("authors"):
                data["authors"] = cat.get("doc_author")
        candidates = [("file system", _parse(data["fs_created"])), ("catalog first seen", _parse(cat.get("first_seen_at"))),
                      ("catalog first event", _parse(cat.get("first_event_at")))]
        known = [(s, d) for s, d in candidates if d is not None]
        if known:
            source, first = min(known, key=lambda x: x[1])
            data["first_seen"] = first.isoformat(timespec="seconds")
            data["first_seen_source"] = source
        parts = [f"{path.name} nằm ở {path.parent}"]
        if data.get("authors"):
            parts.append(f"Authors (tác giả): {data['authors']}")
        if data.get("last_saved_by"):
            parts.append(f"người lưu cuối: {data['last_saved_by']}")
        if data.get("first_seen"):
            parts.append(f"ngày tạo (xuất hiện lần đầu): {data['first_seen'][:10]} (theo {data['first_seen_source']})")
        if data.get("content_created"):
            parts.append(f"ngày tạo nội dung ghi trong tệp: {str(data['content_created'])[:10]}")
        parts.append(f"sửa lần cuối: {str(data['fs_modified'])[:16].replace('T', ' ')}")
        return ToolOutcome(data=data, observation="; ".join(parts) + ".", backend=BACKEND)
