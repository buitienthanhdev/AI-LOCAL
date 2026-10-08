"""Filesystem primitives with verifiable results (hashes, sizes) and atomic writes."""

from __future__ import annotations

import base64
import codecs
import hashlib
import os
import tempfile
from pathlib import Path

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.windows.provider import DirectoryEntry, FileContent, WriteResult

_BINARY_SNIFF_BYTES = 8192
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _os_error(exc: OSError, path: Path) -> ToolError:
    if isinstance(exc, FileNotFoundError):
        return ToolError(ErrorCode.NOT_FOUND, f"not found: {path}")
    if isinstance(exc, PermissionError):
        return ToolError(ErrorCode.POLICY_DENIED, f"the OS denied access to {path}: {exc.strerror}",
                         details={"winerror": getattr(exc, "winerror", None)})
    if isinstance(exc, IsADirectoryError | NotADirectoryError):
        return ToolError(ErrorCode.INVALID_ARGUMENT, f"{path}: {exc.strerror}")
    return ToolError(ErrorCode.EXECUTION_FAILED, f"{path}: {exc.strerror or exc}",
                     details={"errno": exc.errno, "winerror": getattr(exc, "winerror", None)})


def _decode(data: bytes, encoding: str | None) -> tuple[str, str]:
    if encoding:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as exc:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"cannot decode as {encoding}: {exc}") from exc
    for bom, name in _BOMS:
        if data.startswith(bom):
            return data.decode(name), name
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return base64.b64encode(data).decode("ascii"), "base64"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # Legacy ANSI text (e.g. files written by old Windows tools). cp1252 maps every byte except 5
        # undefined ones, which are replaced; the encoding name tells the caller what happened.
        return data.decode("cp1252", errors="replace"), "cp1252"


def read_file(path: Path, max_bytes: int, encoding: str | None) -> FileContent:
    try:
        size = path.stat().st_size
        if path.is_dir():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is a directory")
        with path.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        raise _os_error(exc, path) from exc
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    text, used_encoding = _decode(data, encoding)
    return FileContent(
        path=path,
        size=size,
        encoding=used_encoding,
        content=text,
        truncated=truncated,
        sha256=sha256_file(path) if not truncated else sha256_bytes(data),
    )


def write_file(
    path: Path, content: str, encoding: str, overwrite: bool, create_dirs: bool, max_bytes: int
) -> WriteResult:
    try:
        data = content.encode(encoding)
    except (UnicodeEncodeError, LookupError) as exc:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"cannot encode content as {encoding}: {exc}") from exc
    if len(data) > max_bytes:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"content is {len(data)} bytes; limit is {max_bytes}")
    existed = path.exists()
    if existed and not overwrite:
        raise ToolError(ErrorCode.CONFLICT, f"{path} already exists and overwrite is false")
    if existed and path.is_dir():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is a directory")
    parent = path.parent
    try:
        if not parent.exists():
            if not create_dirs:
                raise ToolError(ErrorCode.NOT_FOUND, f"parent directory {parent} does not exist")
            parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".lga-", suffix=".tmp", dir=parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        written_size = path.stat().st_size
        written_hash = sha256_file(path)
    except OSError as exc:
        raise _os_error(exc, path) from exc
    expected_hash = sha256_bytes(data)
    if written_size != len(data) or written_hash != expected_hash:
        raise ToolError(
            ErrorCode.VERIFICATION_FAILED,
            f"read-back of {path} does not match what was written",
            details={"expected_size": len(data), "actual_size": written_size},
        )
    return WriteResult(path=path, size=written_size, sha256=written_hash, created=not existed)


def list_directory(path: Path, max_entries: int) -> tuple[list[DirectoryEntry], bool]:
    try:
        if not path.is_dir():
            if path.exists():
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is not a directory")
            raise ToolError(ErrorCode.NOT_FOUND, f"not found: {path}")
        entries: list[DirectoryEntry] = []
        truncated = False
        with os.scandir(path) as it:
            for item in sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())):
                if len(entries) >= max_entries:
                    truncated = True
                    break
                try:
                    stat = item.stat(follow_symlinks=False)
                except OSError:
                    continue  # entry vanished or is inaccessible; listing the rest is still correct
                is_dir = item.is_dir(follow_symlinks=False)
                entries.append(DirectoryEntry(
                    name=item.name,
                    is_dir=is_dir,
                    size=None if is_dir else stat.st_size,
                    modified_unix=stat.st_mtime,
                ))
    except OSError as exc:
        raise _os_error(exc, path) from exc
    return entries, truncated
