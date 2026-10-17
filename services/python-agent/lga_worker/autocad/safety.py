"""Automation safety (spec section 14, 16, 17): operation records, dry-run vs apply, idempotency, backup, confirmation.

Pure Python (files and JSON only). The COM provider stays separate; this module decides *whether and how* an
operation may run and keeps the evidence:

* every operation gets an ID, its inputs, the entities it affects, a status and a log;
* DRY_RUN / PREVIEW never calls the executor; APPLY does;
* the idempotency key is a hash of (kind, canonical inputs). A retry of an APPLIED operation is answered from the
  journal (`SKIPPED_DUPLICATE`) and never runs twice, so it cannot create duplicate entities. `marker()` gives the
  string to store on created entities (XData / description) so a re-run can also recognise them in the drawing;
* an existing target file is backed up (with SHA-256) before it is overwritten, and restored if the write fails;
* bulk delete, purge, explode, unit/scale change, overwrite and layer-structure change need explicit confirmation.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SafetyError(RuntimeError):
    pass


class OpKind(str, enum.Enum):
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    PURGE = "PURGE"
    EXPLODE = "EXPLODE"
    CHANGE_UNITS = "CHANGE_UNITS"
    OVERWRITE = "OVERWRITE"
    LAYER_STRUCTURE = "LAYER_STRUCTURE"
    EXPORT = "EXPORT"
    SAVE = "SAVE"


class Mode(str, enum.Enum):
    DRY_RUN = "DRY_RUN"
    APPLY = "APPLY"


class OpStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    PREVIEWED = "PREVIEWED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"


DEFAULT_BULK_DELETE_THRESHOLD = 5
ALWAYS_CONFIRM = {OpKind.PURGE: "purge removes unused definitions without a dependency check",
                  OpKind.EXPLODE: "explode destroys block/xref structure",
                  OpKind.CHANGE_UNITS: "changing units or scale changes every distance in the drawing",
                  OpKind.LAYER_STRUCTURE: "changing the layer structure affects plotting and every reference to it"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def idempotency_key(kind: OpKind | str, inputs: Mapping[str, Any]) -> str:
    kind = OpKind(kind) if not isinstance(kind, OpKind) else kind
    return hashlib.sha256(f"{kind.value}|{canonical(inputs)}".encode("utf-8")).hexdigest()[:16]


def marker(key: str) -> str:
    """String to store on every entity an operation creates (XData / description) so a re-run can find them."""
    return f"LGA:{key}"


def confirmation_reasons(kind: OpKind, *, count: int = 1, target_exists: bool = False,
                         bulk_threshold: int = DEFAULT_BULK_DELETE_THRESHOLD) -> list[str]:
    """Why an operation needs an explicit yes (empty list = it may run on its own)."""
    if bulk_threshold < 1:
        raise ValueError("bulk_threshold must be at least 1")
    reasons: list[str] = []
    if kind in ALWAYS_CONFIRM:
        reasons.append(ALWAYS_CONFIRM[kind])
    if kind is OpKind.DELETE and count > bulk_threshold:
        reasons.append(f"deleting {count} entities exceeds the bulk threshold of {bulk_threshold}")
    if kind in (OpKind.OVERWRITE, OpKind.SAVE, OpKind.EXPORT) and target_exists:
        reasons.append("the target file already exists and would be overwritten (a backup is made first)")
    return reasons


# --------------------------------------------------------------------------- files

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BackupInfo:
    source: str
    backup: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "backup": self.backup, "sha256": self.sha256, "size": self.size}


def backup_file(path: Path, backup_dir: Path | None = None) -> BackupInfo | None:
    """Copy `path` to `<dir>/.lga_backups/<name>.<UTC time>.bak` and verify the copy by hash. None if no such file."""
    path = Path(path)
    if not path.is_file():
        return None
    directory = backup_dir or path.parent / ".lga_backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    original, copy = sha256_file(path), sha256_file(target)
    if original != copy:
        target.unlink(missing_ok=True)
        raise SafetyError(f"backup of {path} did not verify (hash mismatch); the write was not started")
    return BackupInfo(str(path), str(target), copy, target.stat().st_size)


def restore_backup(info: BackupInfo) -> None:
    backup, source = Path(info.backup), Path(info.source)
    if not backup.is_file() or sha256_file(backup) != info.sha256:
        raise SafetyError(f"backup {backup} is missing or no longer matches its hash; cannot restore")
    shutil.copy2(backup, source)


def make_working_copy(source: Path, work_dir: Path, name: str | None = None) -> dict[str, Any]:
    """Copy a source drawing into `work_dir` so edits never touch the original (spec 2). Returns hashes for later proof."""
    source, work_dir = Path(source), Path(work_dir)
    if not source.is_file():
        raise SafetyError(f"source file not found: {source}")
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / (name or f"{source.stem}_work{source.suffix}")
    if target.resolve() == source.resolve():
        raise SafetyError("the working copy would be the original file")
    if target.exists():
        raise SafetyError(f"working copy already exists: {target}")
    shutil.copy2(source, target)
    digest = sha256_file(source)
    if sha256_file(target) != digest:
        target.unlink(missing_ok=True)
        raise SafetyError("working copy does not match the source")
    return {"source": str(source), "copy": str(target), "sha256": digest}


def verify_source_unchanged(source: Path, expected_sha256: str) -> bool:
    return Path(source).is_file() and sha256_file(Path(source)) == expected_sha256


# --------------------------------------------------------------------------- journal

@dataclass
class OperationRecord:
    op_id: str
    kind: OpKind
    mode: Mode
    key: str
    inputs: dict[str, Any]
    status: OpStatus = OpStatus.PLANNED
    affected: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    error: str | None = None
    backup: BackupInfo | None = None
    confirmation_reasons: list[str] = field(default_factory=list)
    result: Any = None
    started: str = field(default_factory=_now)
    finished: str | None = None

    def note(self, text: str) -> None:
        self.log.append(f"{_now()} {text}")

    def to_dict(self) -> dict[str, Any]:
        return {"op_id": self.op_id, "kind": self.kind.value, "mode": self.mode.value, "key": self.key,
                "marker": marker(self.key), "inputs": self.inputs, "status": self.status.value,
                "affected": self.affected, "log": self.log, "error": self.error,
                "backup": self.backup.to_dict() if self.backup else None,
                "confirmation_reasons": self.confirmation_reasons, "started": self.started, "finished": self.finished}


class OperationJournal:
    """In-memory operation log, optionally mirrored to a JSON-lines file (write failures never break an operation)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[OperationRecord] = []

    def find_applied(self, key: str) -> OperationRecord | None:
        for record in reversed(self.records):
            if record.key == key and record.status is OpStatus.APPLIED:
                return record
        return None

    def add(self, record: OperationRecord) -> None:
        self.records.append(record)

    def flush(self, record: OperationRecord) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(canonical(record.to_dict()) + "\n")
        except OSError:
            record.note("journal file could not be written; record kept in memory only")


def new_record(kind: OpKind, mode: Mode, inputs: Mapping[str, Any], affected: Sequence[str] = ()) -> OperationRecord:
    return OperationRecord(op_id=f"OP-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}", kind=kind,
                           mode=mode, key=idempotency_key(kind, inputs), inputs=dict(inputs), affected=list(affected))


def run_operation(journal: OperationJournal, kind: OpKind | str, inputs: Mapping[str, Any],
                  executor: Callable[[], Any], *, mode: Mode | str = Mode.DRY_RUN, confirmed: bool = False,
                  affected: Sequence[str] = (), target_path: Path | None = None, count: int | None = None,
                  bulk_threshold: int = DEFAULT_BULK_DELETE_THRESHOLD, backup_dir: Path | None = None,
                  reraise: bool = True) -> OperationRecord:
    """Plan, preview or apply one operation. Default mode is DRY_RUN: nothing runs unless APPLY is explicit."""
    kind = kind if isinstance(kind, OpKind) else OpKind(kind)
    mode = mode if isinstance(mode, Mode) else Mode(mode)
    key = idempotency_key(kind, inputs)
    record = new_record(kind, mode, inputs, affected)
    journal.add(record)
    try:
        target_exists = bool(target_path and Path(target_path).is_file())
        record.confirmation_reasons = confirmation_reasons(
            kind, count=len(record.affected) if count is None else count, target_exists=target_exists,
            bulk_threshold=bulk_threshold)
        if mode is Mode.DRY_RUN:
            record.status = OpStatus.PREVIEWED
            record.note(f"dry run: would affect {len(record.affected)} entities; nothing was changed")
            return record
        previous = journal.find_applied(key)
        if previous is not None and previous is not record:
            record.status = OpStatus.SKIPPED_DUPLICATE
            record.result = previous.result
            record.note(f"already applied as {previous.op_id}; not run again (idempotent retry)")
            return record
        if record.confirmation_reasons and not confirmed:
            record.status = OpStatus.NEEDS_CONFIRMATION
            record.note("confirmation required: " + "; ".join(record.confirmation_reasons))
            return record
        if target_exists:
            record.backup = backup_file(Path(target_path), backup_dir)  # type: ignore[arg-type]
            record.note(f"backup written to {record.backup.backup if record.backup else '?'}")
        try:
            record.result = executor()
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unless asked not to
            record.status = OpStatus.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.note(f"failed: {record.error}")
            if record.backup is not None:
                try:
                    restore_backup(record.backup)
                    record.status = OpStatus.ROLLED_BACK
                    record.note("target restored from backup")
                except SafetyError as restore_error:
                    record.note(f"restore failed: {restore_error}")
            if reraise:
                raise
            return record
        record.status = OpStatus.APPLIED
        record.note("applied")
        return record
    finally:
        record.finished = _now()
        journal.flush(record)


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file so a crash never leaves it half written (temp file in the same folder + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
