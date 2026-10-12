"""AutoCadProvider: every AutoCAD COM operation of the worker, executed on one dedicated STA thread.

Adapted from AutoCAD-MCP (https://github.com/beiming183-cloud/AutoCAD-MCP, commit 11f7c47, MIT,
(c) 2024 AutoCAD MCP Server Contributors): entity creation through ModelSpace.AddLine / AddCircle /
AddLightWeightPolyline (+ .Closed) / AddText (`_create_entity_via_com`); read-back by HandleToObject with erase on
mismatch (`verify_created_entity`); SaveAs followed by a FullName check (`_save_via_com`); DXF export through
Document.Export with a temporary selection set, keeping the drawing's own path (`_export_dxf_via_com`); and PDF
plotting that saves and restores the layout's plot settings around Plot.PlotToFile with BACKGROUNDPLOT=0
(`_plot_preview_via_com`).
Its LISP / SendCommand / WM_CHAR fallbacks are deliberately not ported: when COM fails, the call fails closed.

Design:
* Threading. All public methods are synchronous and must run on `self.executor` (a `StaExecutor("autocad")`);
  tool handlers call them through `await provider.executor.run(timeout, method, ..., deadline=...)`. The COM
  application proxy is cached together with the thread that created it; if the executor abandoned a stuck thread,
  the proxy is dropped and re-acquired on the new thread.
* Deadlines. Every public method takes `deadline` (a `time.monotonic()` value that ends before the executor's
  timeout) so busy retries and launch/plot waits fail with their own deterministic error instead of a thread abort.
* Busy handling. Idempotent calls (reads, setting a property to a fixed value, SaveAs/Export/PlotToFile to the same
  file, deleting our own temporary objects) go through `_idempotent` (bounded busy retry, `autocad.busy_retry_ms`).
  Calls that create or erase drawings or entities are never retried; before any change, `_ensure_idle` refuses
  with BUSY while a command is active (CMDACTIVE != 0) or AutoCAD is not quiescent.
* Identity. The provider binds to one acad.exe (PID of the COM application's HWND). If COM later hands back a
  different, still-running instance the call fails with CONFLICT instead of drawing into the wrong AutoCAD.
* Error mapping (see `com.map_com_error`): busy -> BUSY, server gone -> BACKEND_UNAVAILABLE ("call autocad.launch
  first"), no drawing -> NOT_FOUND ("call autocad.new_document"), anything else -> EXECUTION_FAILED with AutoCAD's
  own message. Nothing is swallowed and no call reports success after an exception.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from lga_common.config import Config

from lga_worker import inventory
from lga_worker.autocad import com, floorplan, geometry
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.sta import StaExecutor

logger = logging.getLogger(__name__)

ACAD_IMAGE_NAME = "acad.exe"
LAUNCH_ARGUMENTS = ("/nologo",)
LAUNCH_POLL_INTERVAL_S = 0.5
ACTIVATION_POLL_INTERVAL_S = 0.1
FILE_POLL_INTERVAL_S = 0.1
CLOSE_TIMEOUT_S = 5.0
ERROR_ACCESS_DENIED = 5  # CreateProcess result when the parent's job object forbids CREATE_BREAKAWAY_FROM_JOB

# AutoCAD ActiveX enumerations.
AC_PLOT_TYPE_EXTENTS = 1  # AcPlotType.acExtents
AC_SCALE_TO_FIT = 0  # AcPlotScale.acScaleToFit
# Layout plot settings a PDF plot changes (directly or through the device), compared after restoring them.
PLOT_VERIFY_SETTINGS = ("ConfigName", "CanonicalMediaName", "PlotType", "StandardScale", "UseStandardScale",
                        "CenterPlot", "PlotRotation", "PaperUnits", "StyleSheet")
PLOT_BACKUP_PREFIX = "LGA_PLOT_BACKUP_"
# Preferred paper for "DWG To PDF.pc3" when the layout's current media is not available on the device.
PDF_MEDIA_PREFERENCE = (
    "ISO_full_bleed_A4_(297.00_x_210.00_MM)", "ISO_A4_(297.00_x_210.00_MM)", "ANSI_A_(11.00_x_8.50_Inches)",
)

NO_DOCUMENT_HINT = "AutoCAD has no active drawing; call autocad.new_document first"
UNKNOWN_HANDLE = "(unknown)"
# Layers of a drawn floor plan: role -> (layer name, ACI colour).
PLAN_LAYERS = {"wall": ("KT-TUONG", 7), "door": ("KT-CUA", 1), "window": ("KT-CUASO", 4), "furniture": ("KT-NOITHAT", 8),
               "porch": ("KT-HIEN", 2), "text": ("KT-CHU", 2), "dimension": ("KT-KICHTHUOC", 3)}
PLAN_TEXT_STYLE = "LGA_VN"  # Arial: renders Vietnamese marks, unlike AutoCAD's default txt.shx
DXF_EXPORT_FORMAT = "dxf"  # lower case: AutoCAD appends it verbatim as the file extension
DXF_SELECTION_PREFIX = "LGA_DXF_"
DXF_HEAD_BYTES = 512
# AcSaveAsType DXF codes -> the $ACADVER header value of that DXF release.
DXF_SAVE_TYPE_VERSIONS = {13: "AC1015", 25: "AC1018", 37: "AC1021", 49: "AC1024", 61: "AC1027", 65: "AC1032"}
PDF_MAGIC = b"%PDF"


def same_file_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Case-insensitive comparison of two Windows paths after resolving 8.3 names and relative parts."""
    if not str(a) or not str(b):
        return False
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def dxf_version(head: bytes) -> str | None:
    """The $ACADVER value (e.g. AC1032) from the start of an ASCII DXF header, or None."""
    lines = [line.strip() for line in head.replace(b"\r\n", b"\n").split(b"\n")]
    for index, line in enumerate(lines[:-2]):
        if line == b"$ACADVER" and lines[index + 1] == b"1":
            return lines[index + 2].decode("ascii", "replace")
    return None


def looks_like_dxf(head: bytes) -> bool:
    """ASCII DXF starts with group code 0 + SECTION (optionally after 999 comments); binary DXF has a sentinel."""
    if head.startswith(b"AutoCAD Binary DXF"):
        return True
    lines = [line.strip() for line in head.replace(b"\r\n", b"\n").split(b"\n")]
    while len(lines) >= 2 and lines[0] == b"999":
        lines = lines[2:]
    return len(lines) >= 2 and lines[0] == b"0" and lines[1] == b"SECTION"


@dataclass(frozen=True)
class AutoCadSettings:
    prog_id: str
    launch_timeout_s: float
    com_call_timeout_s: float
    busy_retry_s: float
    template: str
    dwg_save_type: int
    dxf_save_type: int
    pdf_plot_device: str
    tolerance: float

    @classmethod
    def from_config(cls, config: Config) -> AutoCadSettings:
        return cls(
            prog_id=str(config.get("autocad.prog_id")),
            launch_timeout_s=config.seconds("autocad.launch_timeout_ms"),
            com_call_timeout_s=config.seconds("autocad.com_call_timeout_ms"),
            busy_retry_s=config.seconds("autocad.busy_retry_ms"),
            template=str(config.get("autocad.template") or ""),
            dwg_save_type=int(config.get("autocad.dwg_save_type")),
            dxf_save_type=int(config.get("autocad.dxf_save_type")),
            pdf_plot_device=str(config.get("autocad.pdf_plot_device")),
            tolerance=float(config.get("autocad.geometry_tolerance")),
        )


@dataclass
class LaunchInfo:
    version: str
    pid: int
    hwnd: int
    launched: bool
    documents: int
    active_document: str | None
    outlives_worker: bool


@dataclass
class NewDocumentInfo:
    name: str
    documents_before: int
    documents_after: int
    active_document: str | None
    template: str | None


@dataclass
class CreateResult:
    entity: dict[str, Any]
    expect: dict[str, Any]
    mismatches: list[dict[str, Any]]
    tolerance: float


@dataclass
class LayerInfo:
    name: str | None
    color: int | None
    created: bool
    exists: bool


@dataclass
class VerifyResult:
    handle: str
    actual: dict[str, Any] | None
    mismatches: list[dict[str, Any]]
    tolerance: float

    @property
    def matches(self) -> bool:
        return self.actual is not None and not self.mismatches


@dataclass
class EraseResult:
    handle: str
    object_name: str
    gone: bool


@dataclass
class PlanResult:
    document: str
    counts: dict[str, int]


@dataclass
class StrokesResult:
    document: str
    drawn: int
    layers: int
    true_color: bool
    entities_before: int
    entities_after: int
    background: str  # "white" | "kept" | "unchanged (could not set)"
    previous_background: int | None


@dataclass
class SaveResult:
    path: Path
    full_name: str


@dataclass
class DxfResult:
    path: Path
    document_path: str
    acadver: str | None
    expected_acadver: str | None
    cleanup_error: str | None


@dataclass
class PdfResult:
    path: Path
    device: str
    media: str | None
    layout: str
    plotted: bool
    restore_errors: list[str] = field(default_factory=list)


class AutoCadProvider:
    def __init__(self, config: Config) -> None:
        self.settings = AutoCadSettings.from_config(config)
        self.executor = StaExecutor("autocad")
        self.pid: int | None = None
        self.hwnd: int | None = None
        self._app: Any = None
        self._app_thread: int | None = None
        self._launched_process: subprocess.Popen[bytes] | None = None

    async def close(self) -> None:
        """Release the COM proxy on its own thread and stop the STA thread. AutoCAD itself keeps running."""
        try:
            await self.executor.run(CLOSE_TIMEOUT_S, self._forget)
        finally:
            self.executor.shutdown()

    # ------------------------------------------------------------------ infrastructure (STA thread only)

    def _forget(self) -> None:
        self._app = None
        self._app_thread = None

    def _busy_deadline(self, deadline: float) -> float:
        return min(deadline, time.monotonic() + self.settings.busy_retry_s)

    def _idempotent(self, operation: str, fn: Callable[[], Any], deadline: float) -> Any:
        """An idempotent COM call, retried with backoff while AutoCAD rejects it as busy."""
        return com.retry_busy(fn, operation=operation, idempotent=True, deadline=self._busy_deadline(deadline))

    def _guard(self, operation: str, fn: Callable[[], Any]) -> Any:
        """Run one public operation; translate COM failures into deterministic ToolErrors."""
        try:
            return fn()
        except ToolError as err:
            if err.code == ErrorCode.BACKEND_UNAVAILABLE:
                self._forget()
            raise
        except Exception as exc:
            if not com.is_com_error(exc):
                raise
            error = com.map_com_error(exc, operation)
            if error.code == ErrorCode.BACKEND_UNAVAILABLE:
                self._forget()
            raise error from exc

    @staticmethod
    def _acad_pids() -> list[int]:
        return sorted(p.pid for p in psutil.process_iter(["name"])
                      if (p.info.get("name") or "").lower() == ACAD_IMAGE_NAME)

    @staticmethod
    def _is_acad_process(pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            return psutil.Process(pid).name().lower() == ACAD_IMAGE_NAME
        except psutil.NoSuchProcess:
            return False

    def _bind(self, app: Any, deadline: float, expected_pid: int | None = None) -> None:
        hwnd = int(self._idempotent("read AutoCAD main window", lambda: app.HWND, deadline))
        pid = com.window_pid(hwnd)
        expected = expected_pid if expected_pid is not None else (self.pid if self._is_acad_process(self.pid) else None)
        if expected is not None and pid != expected:
            raise ToolError(
                ErrorCode.CONFLICT,
                f"COM returned the AutoCAD instance with pid {pid}, but the agent works with pid {expected}; "
                "close the other AutoCAD instance so only one is running",
                details={"com_pid": pid, "expected_pid": expected, "acad_pids": self._acad_pids()},
            )
        self._app, self._app_thread, self.pid, self.hwnd = app, threading.get_ident(), pid, hwnd

    def _application(self, deadline: float) -> Any:
        if self._app is not None and (self._app_thread != threading.get_ident() or not self._is_acad_process(self.pid)):
            self._forget()
        if self._app is None:
            app = self._idempotent("attach to AutoCAD",
                                   lambda: com.get_active_application(self.settings.prog_id), deadline)
            if app is None:
                raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, com.NOT_RUNNING_HINT,
                                details={"acad_pids": self._acad_pids()})
            self._bind(app, deadline)
        return self._app

    def _active_document(self, app: Any, deadline: float) -> Any:
        count = int(self._idempotent("count drawings", lambda: app.Documents.Count, deadline))
        if count == 0:
            raise ToolError(ErrorCode.NOT_FOUND, NO_DOCUMENT_HINT)

        def active() -> Any:
            try:
                return app.ActiveDocument
            except Exception as exc:
                if com.is_com_error(exc) and not com.is_busy(exc) and not com.is_disconnected(exc):
                    raise ToolError(ErrorCode.NOT_FOUND, f"{NO_DOCUMENT_HINT} ({com.describe(exc)})") from exc
                raise

        return self._idempotent("get the active drawing", active, deadline)

    def _active_name(self, app: Any, deadline: float) -> str | None:
        try:
            doc = self._active_document(app, deadline)
        except ToolError as err:
            if err.code == ErrorCode.NOT_FOUND:
                return None
            raise
        return str(self._idempotent("read drawing name", lambda: doc.Name, deadline))

    def _ensure_idle(self, app: Any, doc: Any | None, deadline: float) -> None:
        if doc is not None:
            active = int(self._idempotent("read CMDACTIVE", lambda: doc.call("GetVariable", "CMDACTIVE"), deadline))
            if active:
                raise ToolError(ErrorCode.BUSY, f"a command is in progress in AutoCAD (CMDACTIVE={active}). "
                                f"{com.BUSY_HINT}", details={"cmdactive": active})
        quiescent = bool(self._idempotent("read AutoCAD state", lambda: app.call("GetAcadState").IsQuiescent, deadline))
        if not quiescent:
            raise ToolError(ErrorCode.BUSY, f"AutoCAD is not idle. {com.BUSY_HINT}")

    def _writable_document(self, deadline: float) -> tuple[Any, Any]:
        app = self._application(deadline)
        doc = self._active_document(app, deadline)
        self._ensure_idle(app, doc, deadline)
        return app, doc

    def _find_layer(self, doc: Any, name: str, deadline: float) -> Any | None:
        wanted = name.casefold()

        def find() -> Any | None:
            layers = doc.Layers
            for index in range(int(layers.Count)):
                layer = layers.call("Item", index)
                if str(layer.Name).casefold() == wanted:
                    return layer
            return None

        return self._idempotent(f"look up layer {name!r}", find, deadline)

    def _entity_by_handle(self, doc: Any, handle: str, deadline: float) -> Any | None:
        """The live entity with this handle, or None when the handle is unknown or the entity was erased."""

        def lookup() -> Any | None:
            try:
                entity = doc.call("HandleToObject", handle)
            except Exception as exc:
                if com.is_com_error(exc) and not com.is_busy(exc) and not com.is_disconnected(exc):
                    logger.debug("HandleToObject(%s): %s", handle, com.describe(exc))
                    return None
                raise
            return entity

        return self._idempotent(f"look up handle {handle}", lookup, deadline)

    def _try_erase(self, entity: Any) -> tuple[bool, str | None]:
        """Erase as a rollback step. A failure is returned (and logged) so the caller can report it."""
        try:
            entity.call("Erase")
        except Exception as exc:
            if not com.is_com_error(exc):
                raise
            logger.error("rollback erase failed: %s", com.describe(exc))
            return False, com.describe(exc)
        return True, None

    # ------------------------------------------------------------------ status and launch

    def probe(self, *, deadline: float) -> dict[str, Any]:
        """Attach to a running AutoCAD (never starts it) and report version and drawings."""

        def run() -> dict[str, Any]:
            app = self._application(deadline)
            version = str(self._idempotent("read AutoCAD version", lambda: app.Version, deadline))
            documents = int(self._idempotent("count drawings", lambda: app.Documents.Count, deadline))
            return {"version": version, "pid": self.pid, "documents": documents,
                    "active_document": self._active_name(app, deadline)}

        return self._guard("probe AutoCAD", run)

    def launch(self, *, deadline: float) -> LaunchInfo:
        return self._guard("launch AutoCAD",
                           lambda: self._launch(min(deadline, time.monotonic() + self.settings.launch_timeout_s)))

    def _try_attach(self) -> Any | None:
        try:
            return com.get_active_application(self.settings.prog_id)
        except Exception as exc:
            if com.is_com_error(exc) and com.is_busy(exc):
                return None  # registered but still initialising; the caller keeps polling
            raise

    def _select_executable(self) -> Path:
        installations = inventory.autocad_installations()
        if not installations:
            raise ToolError(ErrorCode.NOT_FOUND, "AutoCAD is not installed (no AcadLocation under "
                            r"HKLM\SOFTWARE\Autodesk\AutoCAD)")
        if not inventory.com_registered(self.settings.prog_id):
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"{self.settings.prog_id} is not registered for COM; "
                            "repair the AutoCAD installation", retryable=False)
        server = com.com_server_executable(self.settings.prog_id)
        if server is not None:
            for install in installations:
                if same_file_path(install["executable"], server):
                    return Path(install["executable"])
        newest = max(installations, key=lambda i: i["release"])
        return Path(newest["executable"])

    def _spawn(self, executable: Path) -> tuple[subprocess.Popen[bytes], bool]:
        """Start acad.exe detached, without inheriting any handle, outside the worker's job object if allowed."""
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        command = [str(executable), *LAUNCH_ARGUMENTS]
        try:
            # No std handles + close_fds=True -> CreateProcess(bInheritHandles=FALSE).
            process = subprocess.Popen(command, cwd=str(executable.parent), close_fds=True,
                                       creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            return process, True
        except PermissionError as exc:
            if getattr(exc, "winerror", None) != ERROR_ACCESS_DENIED:
                raise
            logger.warning("the worker's job object forbids breakaway; AutoCAD will close when the worker exits")
        process = subprocess.Popen(command, cwd=str(executable.parent), close_fds=True, creationflags=flags)
        return process, False

    def _launch(self, deadline: float) -> LaunchInfo:
        launched = False
        outlives_worker = True
        process: subprocess.Popen[bytes] | None = None
        app = self._try_attach()
        if app is None:
            if not self._acad_pids():
                executable = self._select_executable()
                process, outlives_worker = self._spawn(executable)
                self._launched_process = process
                launched = True
                logger.info("started %s (pid %d)", executable, process.pid)
            app = self._wait_for_com(process, deadline)
        self._wait_quiescent(app, deadline)  # first: a starting AutoCAD rejects calls for a long time
        self._bind(app, deadline, expected_pid=process.pid if process is not None else None)
        version = str(self._idempotent("read AutoCAD version", lambda: app.Version, deadline))
        documents = int(self._idempotent("count drawings", lambda: app.Documents.Count, deadline))
        return LaunchInfo(version=version, pid=int(self.pid or 0), hwnd=int(self.hwnd or 0), launched=launched,
                          documents=documents, active_document=self._active_name(app, deadline),
                          outlives_worker=outlives_worker)

    def _wait_for_com(self, process: subprocess.Popen[bytes] | None, deadline: float) -> Any:
        started = time.monotonic()
        while True:
            app = self._try_attach()
            if app is not None:
                return app
            if process is not None and process.poll() is not None and not self._acad_pids():
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                f"acad.exe exited with code {process.returncode} before answering COM")
            if time.monotonic() + LAUNCH_POLL_INTERVAL_S > deadline:
                raise ToolError(ErrorCode.TIMEOUT,
                                f"AutoCAD did not register for COM within {time.monotonic() - started:.0f}s; a startup "
                                "dialog (sign-in, license, crash recovery) may be waiting for the user",
                                details={"acad_pids": self._acad_pids()})
            com.pump_sleep(LAUNCH_POLL_INTERVAL_S)

    def _wait_quiescent(self, app: Any, deadline: float) -> None:
        started = time.monotonic()
        while True:
            try:
                if bool(app.call("GetAcadState").IsQuiescent):
                    return
            except Exception as exc:
                if not (com.is_com_error(exc) and com.is_busy(exc)):
                    raise
            if time.monotonic() + LAUNCH_POLL_INTERVAL_S > deadline:
                raise ToolError(ErrorCode.TIMEOUT,
                                "AutoCAD answers COM but did not become idle within "
                                f"{time.monotonic() - started:.0f}s; "
                                "a dialog may be waiting for the user in AutoCAD")
            com.pump_sleep(LAUNCH_POLL_INTERVAL_S)

    # ------------------------------------------------------------------ documents

    def new_document(self, template: str | None, *, deadline: float) -> NewDocumentInfo:
        return self._guard("create a drawing", lambda: self._new_document(template, deadline))

    def _new_document(self, template: str | None, deadline: float) -> NewDocumentInfo:
        app = self._application(deadline)
        before = int(self._idempotent("count drawings", lambda: app.Documents.Count, deadline))
        self._ensure_idle(app, self._active_document(app, deadline) if before else None, deadline)
        chosen = template or self.settings.template or None
        if chosen and Path(chosen).is_absolute() and not Path(chosen).is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"template {chosen} does not exist")
        documents = app.Documents
        doc = documents.call("Add", chosen) if chosen else documents.call("Add")
        name = str(self._idempotent("read the new drawing's name", lambda: doc.Name, deadline))
        self._idempotent("activate the new drawing", lambda: doc.call("Activate"), deadline)
        active = self._wait_active(app, name, deadline)
        after = int(self._idempotent("count drawings", lambda: app.Documents.Count, deadline))
        return NewDocumentInfo(name=name, documents_before=before, documents_after=after, active_document=active,
                               template=chosen)

    def _wait_active(self, app: Any, name: str, deadline: float) -> str | None:
        settle = min(deadline, time.monotonic() + self.settings.com_call_timeout_s)
        while True:
            active = self._active_name(app, deadline)
            if active == name or time.monotonic() + ACTIVATION_POLL_INTERVAL_S > settle:
                return active
            com.pump_sleep(ACTIVATION_POLL_INTERVAL_S)

    def list_documents(self, *, deadline: float) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            app = self._application(deadline)
            active_doc = None
            try:
                active_doc = self._active_document(app, deadline)
            except ToolError as err:
                if err.code != ErrorCode.NOT_FOUND:
                    raise

            def collect() -> list[dict[str, Any]]:
                documents = app.Documents
                active_key = (str(active_doc.Name), str(active_doc.FullName)) if active_doc is not None else None
                result = []
                for index in range(int(documents.Count)):
                    doc = documents.call("Item", index)
                    name, full_name = str(doc.Name), str(doc.FullName)
                    result.append({"name": name, "full_name": full_name, "saved": bool(doc.Saved),
                                   "active": (name, full_name) == active_key})
                return result

            return self._idempotent("list drawings", collect, deadline)

        return self._guard("list drawings", run)

    def close_document(self, name: str, save: bool = False, *, deadline: float) -> bool:
        """Close an open drawing by name (used by tests and cleanup; not a planner tool)."""

        def run() -> bool:
            app = self._application(deadline)

            def find() -> Any | None:
                documents = app.Documents
                for index in range(int(documents.Count)):
                    doc = documents.call("Item", index)
                    if str(doc.Name).casefold() == name.casefold():
                        return doc
                return None

            doc = self._idempotent(f"find drawing {name!r}", find, deadline)
            if doc is None:
                return False
            doc.call("Close", bool(save))
            return True

        return self._guard(f"close drawing {name!r}", run)

    # ------------------------------------------------------------------ entities

    def get_entities(self, object_name: str | None, layer: str | None, limit: int, *,
                     deadline: float) -> tuple[list[dict[str, Any]], int, bool]:
        def run() -> tuple[list[dict[str, Any]], int, bool]:
            app = self._application(deadline)
            doc = self._active_document(app, deadline)

            def scan() -> tuple[list[dict[str, Any]], int, bool]:
                model_space = doc.ModelSpace
                total = int(model_space.Count)
                found: list[dict[str, Any]] = []
                for index in range(total):
                    entity = model_space.call("Item", index)
                    if object_name and not geometry.object_name_matches(object_name, str(entity.ObjectName)):
                        continue
                    if layer and str(entity.Layer).casefold() != layer.casefold():
                        continue
                    found.append(geometry.entity_to_dict(entity))
                    if len(found) >= limit:
                        return found, total, index + 1 < total
                return found, total, False

            return self._idempotent("read model space", scan, deadline)

        return self._guard("read entities", run)

    def create_line(self, start: Mapping[str, Any], end: Mapping[str, Any], layer: str | None, *,
                    deadline: float) -> CreateResult:
        expect = {"object_name": geometry.LINE, "start": start, "end": end}
        return self._guard("create line", lambda: self._create(
            "create line", expect, layer,
            lambda ms: ms.call("AddLine", com.point_variant(start), com.point_variant(end)), None, deadline))

    def create_circle(self, center: Mapping[str, Any], radius: float, layer: str | None, *,
                      deadline: float) -> CreateResult:
        expect = {"object_name": geometry.CIRCLE, "center": center, "radius": float(radius)}
        return self._guard("create circle", lambda: self._create(
            "create circle", expect, layer,
            lambda ms: ms.call("AddCircle", com.point_variant(center), float(radius)), None, deadline))

    def create_polyline(self, points: Sequence[Mapping[str, Any]], closed: bool, layer: str | None, *,
                        deadline: float) -> CreateResult:
        elevation = geometry.polyline_elevation(points)
        coordinates = [value for p in points for value in (float(p["x"]), float(p["y"]))]
        expect = {"object_name": geometry.LWPOLYLINE, "points": [{**p, "z": elevation} for p in points],
                  "closed": bool(closed), "vertex_count": len(points)}

        def configure(entity: Any) -> None:
            entity.Closed = bool(closed)
            if elevation:
                entity.Elevation = elevation

        return self._guard("create polyline", lambda: self._create(
            "create polyline", expect, layer,
            lambda ms: ms.call("AddLightWeightPolyline", com.double_array(coordinates)), configure, deadline))

    def create_text(self, insertion: Mapping[str, Any], text: str, height: float, rotation_deg: float,
                    layer: str | None, *, deadline: float) -> CreateResult:
        expect = {"object_name": geometry.TEXT, "insertion": insertion, "text": text, "height": float(height),
                  "rotation_deg": float(rotation_deg)}

        def configure(entity: Any) -> None:
            if rotation_deg:
                entity.Rotation = math.radians(float(rotation_deg))

        return self._guard("create text", lambda: self._create(
            "create text", expect, layer,
            lambda ms: ms.call("AddText", text, com.point_variant(insertion), float(height)), configure, deadline))

    def _require_layer(self, doc: Any, name: str, deadline: float) -> str:
        layer = self._find_layer(doc, name, deadline)
        if layer is None:
            raise ToolError(ErrorCode.NOT_FOUND,
                            f"layer {name!r} does not exist in the active drawing; create it with autocad.create_layer")
        if bool(self._idempotent(f"read lock state of layer {name!r}", lambda: layer.Lock, deadline)):
            raise ToolError(ErrorCode.CONFLICT, f"layer {name!r} is locked; unlock it in AutoCAD or use another layer")
        return str(self._idempotent("read layer name", lambda: layer.Name, deadline))

    def _create(self, operation: str, expect: dict[str, Any], layer: str | None, add: Callable[[Any], Any],
                configure: Callable[[Any], None] | None, deadline: float) -> CreateResult:
        _, doc = self._writable_document(deadline)
        layer_name = self._require_layer(doc, layer, deadline) if layer is not None else None
        if layer is not None:
            expect = {**expect, "layer": layer}
        model_space = self._idempotent("open model space", lambda: doc.ModelSpace, deadline)
        entity = add(model_space)  # creates an object: never retried
        handle = UNKNOWN_HANDLE
        try:
            handle = str(self._idempotent("read the new handle", lambda: entity.Handle, deadline))
            # Setting properties to fixed values is idempotent, so these steps are retried while AutoCAD is busy.
            if layer_name is not None:
                self._idempotent(f"set layer of {handle}", lambda: setattr(entity, "Layer", layer_name), deadline)
            if configure is not None:
                self._idempotent(f"configure {handle}", lambda: configure(entity), deadline)
            self._idempotent(f"update {handle}", lambda: entity.call("Update"), deadline)
            actual = self._idempotent(f"read back {handle}",
                                      lambda: geometry.entity_to_dict(doc.call("HandleToObject", handle)), deadline)
        except BaseException as exc:
            error = self._rollback_failed_create(entity, handle, operation, exc)
            if error is exc:
                raise
            raise error from exc
        mismatches = geometry.compare(expect, actual, self.settings.tolerance)
        if mismatches:
            erased, erase_error = self._try_erase(entity)
            raise ToolError(
                ErrorCode.VERIFICATION_FAILED,
                f"{operation}: AutoCAD read back {handle} differently ({geometry.describe_mismatches(mismatches)}); "
                + ("the entity was erased" if erased else f"erasing it failed: {erase_error}"),
                details={"handle": handle, "actual": actual, "mismatches": mismatches, "erased": erased,
                         "tolerance": self.settings.tolerance},
            )
        return CreateResult(entity=actual, expect=expect, mismatches=[], tolerance=self.settings.tolerance)

    def _rollback_failed_create(self, entity: Any, handle: str, operation: str, exc: BaseException) -> BaseException:
        erased, erase_error = self._try_erase(entity)
        if isinstance(exc, ToolError):
            error = exc
        elif com.is_com_error(exc):
            error = com.map_com_error(exc, operation)
        else:
            logger.error("%s: unexpected %s after creating %s (erased=%s)", operation, type(exc).__name__,
                         handle, erased)
            return exc
        error.details.update(handle=handle, rolled_back=erased, rollback_error=erase_error)
        return error

    def create_layer(self, name: str, color: int | None, *, deadline: float) -> LayerInfo:
        def run() -> LayerInfo:
            _, doc = self._writable_document(deadline)
            existing = self._find_layer(doc, name, deadline)
            # Layers.Add returns the existing layer for a known name, so it is idempotent like the color write.
            layer = existing if existing is not None else self._idempotent(
                f"add layer {name!r}", lambda: doc.Layers.call("Add", name), deadline)
            if color is not None and int(self._idempotent("read layer color", lambda: layer.color, deadline)) != color:
                self._idempotent("set layer color", lambda: setattr(layer, "color", int(color)), deadline)
            again = self._find_layer(doc, name, deadline)
            if again is None:
                return LayerInfo(name=None, color=None, created=existing is None, exists=False)
            return LayerInfo(name=str(self._idempotent("read layer name", lambda: again.Name, deadline)),
                             color=int(self._idempotent("read layer color", lambda: again.color, deadline)),
                             created=existing is None, exists=True)

        return self._guard(f"create layer {name!r}", run)

    def verify_geometry(self, handle: str, expect: Mapping[str, Any], *, deadline: float) -> VerifyResult:
        def run() -> VerifyResult:
            app = self._application(deadline)
            doc = self._active_document(app, deadline)
            entity = self._entity_by_handle(doc, handle, deadline)
            if entity is None:
                return VerifyResult(handle=handle, actual=None, tolerance=self.settings.tolerance,
                                    mismatches=[{"field": "handle", "expected": "an existing entity",
                                                 "actual": None}])
            actual = self._idempotent(f"read {handle}", lambda: geometry.entity_to_dict(entity), deadline)
            return VerifyResult(handle=handle, actual=actual, tolerance=self.settings.tolerance,
                                mismatches=geometry.compare(expect, actual, self.settings.tolerance))

        return self._guard(f"verify {handle}", run)

    def erase(self, handle: str, *, deadline: float) -> EraseResult:
        def run() -> EraseResult:
            _, doc = self._writable_document(deadline)
            entity = self._entity_by_handle(doc, handle, deadline)
            if entity is None:
                raise ToolError(ErrorCode.NOT_FOUND, f"no entity with handle {handle} in the active drawing")
            object_name = str(self._idempotent(f"read {handle}", lambda: entity.ObjectName, deadline))
            entity.call("Erase")
            gone = self._entity_by_handle(doc, handle, deadline) is None
            return EraseResult(handle=handle, object_name=object_name, gone=gone)

        return self._guard(f"erase {handle}", run)

    def zoom_extents(self, *, deadline: float) -> str:
        def run() -> str:
            app, doc = self._writable_document(deadline)
            self._idempotent("zoom extents", lambda: app.call("ZoomExtents"), deadline)
            return str(self._idempotent("read drawing name", lambda: doc.Name, deadline))

        return self._guard("zoom extents", run)

    # ------------------------------------------------------------------ whole drawings

    def draw_plan(self, plan: floorplan.Plan, new_document: bool, origin: tuple[float, float], *,
                  deadline: float) -> PlanResult:
        """Draw a computed floor plan (walls filled, doors with swings, windows, furniture, labels, dimensions,
        title, area table) as one undo step, then zoom to it."""

        def run() -> PlanResult:
            if new_document:
                self._new_document(None, deadline)
            app, doc = self._writable_document(deadline)
            name = str(self._idempotent("read drawing name", lambda: doc.Name, deadline))
            for layer_name, color in PLAN_LAYERS.values():
                layer = self._idempotent(f"add layer {layer_name}", lambda n=layer_name: doc.Layers.call("Add", n), deadline)
                self._idempotent("set layer color", lambda lay=layer, c=color: setattr(lay, "color", c), deadline)
            style = self._idempotent("add text style", lambda: doc.TextStyles.call("Add", PLAN_TEXT_STYLE), deadline)
            try:
                style.call("SetFont", "Arial", False, False, 0, 34)
            except Exception as exc:  # keeps AutoCAD's default font: still readable, without Vietnamese marks
                logger.warning("draw_plan: could not set the Arial font: %s", com.describe(exc) if com.is_com_error(exc) else exc)
            ms = self._idempotent("open model space", lambda: doc.ModelSpace, deadline)
            ox, oy = origin
            counts: dict[str, int] = {}

            def pt(x: float, y: float) -> Any:
                return com.double_array((x + ox, y + oy, 0.0))

            def put(entity: Any, role: str) -> Any:
                entity.Layer = PLAN_LAYERS[role][0]
                counts[role] = counts.get(role, 0) + 1
                return entity

            def shape(s: floorplan.Shape, role: str) -> None:
                v = s.values
                if s.kind == "rect":
                    pl = ms.call("AddLightWeightPolyline", com.double_array(
                        (v[0] + ox, v[1] + oy, v[2] + ox, v[1] + oy, v[2] + ox, v[3] + oy, v[0] + ox, v[3] + oy)))
                    pl.Closed = True
                    put(pl, role)
                elif s.kind == "line":
                    put(ms.call("AddLine", pt(v[0], v[1]), pt(v[2], v[3])), role)
                elif s.kind == "circle":
                    put(ms.call("AddCircle", pt(v[0], v[1]), float(v[2])), role)
                elif s.kind == "arc":
                    put(ms.call("AddArc", pt(v[0], v[1]), float(v[2]), float(v[3]), float(v[4])), role)

            def text(label: floorplan.Label, attach: int, role: str = "text") -> None:
                mt = ms.call("AddMText", pt(label.x, label.y), 0.0, label.text)
                mt.StyleName = PLAN_TEXT_STYLE
                mt.Height = float(label.height)
                mt.AttachmentPoint = attach
                mt.InsertionPoint = pt(label.x, label.y)
                put(mt, role)

            doc.call("StartUndoMark")
            try:
                for x0, y0, x1, y1 in plan.walls:
                    pl = ms.call("AddLightWeightPolyline", com.double_array(
                        (x0 + ox, y0 + oy, x1 + ox, y0 + oy, x1 + ox, y1 + oy, x0 + ox, y1 + oy)))
                    pl.Closed = True
                    put(pl, "wall")
                    hatch = ms.call("AddHatch", 1, "SOLID", True)  # acHatchPatternTypePreDefined
                    put(hatch, "wall")
                    hatch.call("AppendOuterLoop", com.dispatch_array([pl]))
                    hatch.call("Evaluate")
                for op in plan.openings:
                    t = plan.wall_thickness.get((op.orient, round(op.line, 4)), floorplan.INTERIOR_WALL)
                    for s in floorplan.opening_shapes(op, t):
                        shape(s, "window" if op.kind == "window" else "door")
                for s in plan.furniture:
                    shape(s, "furniture")
                for s in plan.porch_shapes:
                    shape(s, "porch")
                for label in plan.labels:
                    text(label, 5)  # acAttachmentPointMiddleCenter
                text(plan.title, 2)  # top centre
                text(plan.schedule, 1)  # top left
                for dim in plan.dimensions:
                    d = put(ms.call("AddDimAligned", pt(*dim.p1), pt(*dim.p2), pt(*dim.text_at)), "dimension")
                    for prop, value in (("TextStyle", PLAN_TEXT_STYLE), ("TextHeight", plan.text_height),
                                        ("Arrowhead1Type", 4), ("Arrowhead2Type", 4), ("ArrowheadSize", 0.12),
                                        ("ExtensionLineExtension", 0.1), ("ExtensionLineOffset", 0.1),
                                        ("TextGap", 0.05), ("PrimaryUnitsPrecision", 2), ("SuppressTrailingZeros", False),
                                        ("DecimalSeparator", ",")):
                        try:
                            setattr(d, prop, value)
                        except Exception:  # a property this AutoCAD version lacks: keep its default
                            pass
            finally:
                doc.call("EndUndoMark")
            self._idempotent("zoom extents", lambda: app.call("ZoomExtents"), deadline)
            return PlanResult(document=name, counts=counts)

        return self._guard("draw floor plan", run)

    def draw_strokes(self, strokes: Sequence[tuple[Sequence[tuple[float, float]], tuple[int, int, int], bool]],
                     new_document: bool, paper: str, aci_for: Callable[[tuple[int, int, int]], int], *,
                     deadline: float) -> StrokesResult:
        """Draw many coloured strokes (open or closed polylines) as one undo step, each on a layer named after
        its colour (the layer carries the colour, entities stay ByLayer), then zoom to the drawing. Used for
        sketches: AutoCAD is driven as a line-only application, every stroke in the colour of what it describes.
        `paper="white"` sets the model-space background to white (dark pencil lines vanish on black)."""

        def run() -> StrokesResult:
            if new_document:
                self._new_document(None, deadline)
            app, doc = self._writable_document(deadline)
            name = str(self._idempotent("read drawing name", lambda: doc.Name, deadline))
            ms = self._idempotent("open model space", lambda: doc.ModelSpace, deadline)
            before = int(self._idempotent("count entities", lambda: ms.Count, deadline))

            background, previous = "kept", None
            try:
                display = app.Preferences.Display
                previous = int(display.GraphicsWinModelBackgrndColor)
                if paper == "white":
                    display.GraphicsWinModelBackgrndColor = 0xFFFFFF
                    background = "white"
            except Exception as exc:  # a build without the preference: the drawing is still made
                logger.warning("draw_strokes: model-space background not set: %s", exc)
                background = "unchanged (could not set)"

            color_factory: Callable[[], Any] | None = None
            for progid in ("AutoCAD.AcCmColor.24", "AutoCAD.AcCmColor.23", "AutoCAD.AcCmColor.22", "AutoCAD.AcCmColor.21",
                           "AutoCAD.AcCmColor.20", "AutoCAD.AcCmColor"):
                try:
                    app.call("GetInterfaceObject", progid)
                except Exception:  # noqa: BLE001 - try the next version
                    continue
                color_factory = lambda pid=progid: app.call("GetInterfaceObject", pid)  # noqa: E731
                break

            layer_of: dict[tuple[int, int, int], str] = {}
            true_color = color_factory is not None
            for _pts, rgb, _closed in strokes:
                if rgb in layer_of:
                    continue
                layer_name = "SKETCH-%02X%02X%02X" % rgb
                layer = self._idempotent(f"add layer {layer_name}", lambda n=layer_name: doc.Layers.call("Add", n), deadline)
                applied = False
                if color_factory is not None:
                    try:
                        c = color_factory()
                        c.call("SetRGB", int(rgb[0]), int(rgb[1]), int(rgb[2]))
                        layer.TrueColor = c
                        applied = True
                    except Exception as exc:  # noqa: BLE001 - fall back to the nearest index colour
                        logger.warning("draw_strokes: true colour failed (%s); using ACI", exc)
                        true_color = False
                if not applied:
                    layer.color = int(aci_for(rgb))
                layer_of[rgb] = layer_name

            drawn = 0
            doc.call("StartUndoMark")
            try:
                for pts, rgb, closed in strokes:
                    if len(pts) == 2 and not closed:
                        (x0, y0), (x1, y1) = pts
                        entity = ms.call("AddLine", com.double_array((x0, y0, 0.0)), com.double_array((x1, y1, 0.0)))
                    else:
                        flat = [v for point in pts for v in point]
                        entity = ms.call("AddLightWeightPolyline", com.double_array(flat))
                        entity.Closed = bool(closed)
                    entity.Layer = layer_of[rgb]
                    drawn += 1
            finally:
                doc.call("EndUndoMark")
            self._idempotent("zoom extents", lambda: app.call("ZoomExtents"), deadline)
            after = int(self._idempotent("count entities", lambda: ms.Count, deadline))
            return StrokesResult(document=name, drawn=drawn, layers=len(layer_of), true_color=true_color,
                                 entities_before=before, entities_after=after, background=background,
                                 previous_background=previous)

        return self._guard("draw strokes", run)

    # ------------------------------------------------------------------ files

    def save(self, path: Path, overwrite: bool, *, deadline: float) -> SaveResult:
        def run() -> SaveResult:
            _, doc = self._writable_document(deadline)
            current = str(self._idempotent("read drawing path", lambda: doc.FullName, deadline))
            self._prepare_output(path, overwrite, allow_same_as=current)
            self._idempotent("save drawing", lambda: doc.call("SaveAs", str(path), self.settings.dwg_save_type),
                             deadline)
            full_name = str(self._idempotent("read drawing path", lambda: doc.FullName, deadline))
            return SaveResult(path=path, full_name=full_name)

        return self._guard(f"save {path}", run)

    @staticmethod
    def _prepare_output(path: Path, overwrite: bool, allow_same_as: str = "") -> None:
        if path.is_dir():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{path} is a directory")
        if path.exists() and not overwrite and not same_file_path(allow_same_as, path):
            raise ToolError(ErrorCode.CONFLICT, f"{path} already exists; pass overwrite=true to replace it")
        path.parent.mkdir(parents=True, exist_ok=True)

    def export_dxf(self, path: Path, overwrite: bool, *, deadline: float) -> DxfResult:
        def run() -> DxfResult:
            _, doc = self._writable_document(deadline)
            dwg = str(self._idempotent("read drawing path", lambda: doc.FullName, deadline))
            if not dwg or Path(dwg).suffix.lower() != ".dwg":
                raise ToolError(ErrorCode.CONFLICT, "the drawing has not been saved as a .dwg yet; call autocad.save "
                                "with a .dwg path first, then export the DXF", details={"document_path": dwg})
            self._prepare_output(path, overwrite)
            path.unlink(missing_ok=True)  # the checks must see this call's output, not an old file
            # Document.Export writes a copy and leaves the drawing associated with its DWG. (SaveAs .dxf would
            # re-associate the drawing with the DXF, and saving it back fails: AutoCAD keeps the original DWG
            # locked while the drawing is open.) An empty selection set exports the whole drawing.
            selection = doc.SelectionSets.call("Add", f"{DXF_SELECTION_PREFIX}{uuid.uuid4().hex[:12]}")
            try:
                target = str(path.with_suffix(""))
                self._idempotent("export DXF", lambda: doc.call("Export", target, DXF_EXPORT_FORMAT, selection),
                                 deadline)
            finally:
                cleanup_error = self._delete_temporary(selection, "selection set", deadline)
            self._wait_for_file(path, deadline)
            head = b""
            if path.is_file():  # a missing file is reported by the tool's file_exists check
                with path.open("rb") as fh:
                    head = fh.read(DXF_HEAD_BYTES)
            document_path = str(self._idempotent("read drawing path", lambda: doc.FullName, deadline))
            return DxfResult(path=path, document_path=document_path, acadver=dxf_version(head),
                             expected_acadver=DXF_SAVE_TYPE_VERSIONS.get(self.settings.dxf_save_type),
                             cleanup_error=cleanup_error)

        return self._guard(f"export DXF {path}", run)

    def _delete_temporary(self, obj: Any, what: str, deadline: float) -> str | None:
        """Delete a temporary AutoCAD object (idempotent by state); a failure is logged and returned for reporting."""
        try:
            self._idempotent(f"delete the temporary {what}", lambda: obj.call("Delete"), deadline)
        except ToolError as err:
            logger.error("deleting the temporary %s failed: %s", what, err.message)
            return err.message
        except Exception as exc:
            if not com.is_com_error(exc):
                raise
            logger.error("deleting the temporary %s failed: %s", what, com.describe(exc))
            return com.describe(exc)
        return None

    def export_pdf(self, path: Path, overwrite: bool, *, deadline: float) -> PdfResult:
        return self._guard(f"export PDF {path}", lambda: self._export_pdf(path, overwrite, deadline))

    def _export_pdf(self, path: Path, overwrite: bool, deadline: float) -> PdfResult:
        _, doc = self._writable_document(deadline)
        self._prepare_output(path, overwrite)
        path.unlink(missing_ok=True)
        device = self.settings.pdf_plot_device
        layout = self._idempotent("open the active layout", lambda: doc.ActiveLayout, deadline)
        layout_name = str(self._idempotent("read layout name", lambda: layout.Name, deadline))
        is_model = bool(self._idempotent("read layout type", lambda: layout.ModelType, deadline))
        plot = self._idempotent("open the plot object", lambda: doc.Plot, deadline)
        saved = {name: self._idempotent(f"read layout {name}", lambda n=name: getattr(layout, n), deadline)
                 for name in PLOT_VERIFY_SETTINGS}
        background_plot = self._idempotent("read BACKGROUNDPLOT", lambda: doc.call("GetVariable", "BACKGROUNDPLOT"),
                                           deadline)
        quiet_errors = self._idempotent("read plot QuietErrorMode", lambda: plot.QuietErrorMode, deadline)
        # A complete copy of the layout's plot settings. Writing the properties back one by one is not enough:
        # a layout set to a printer with no media ('') cannot be given '' again.
        configurations = self._idempotent("open page setups", lambda: doc.PlotConfigurations, deadline)
        backup = configurations.call("Add", f"{PLOT_BACKUP_PREFIX}{uuid.uuid4().hex[:12]}", is_model)
        try:
            self._idempotent("back up plot settings", lambda: backup.call("CopyFrom", layout), deadline)
        except BaseException:
            self._delete_temporary(backup, "page setup", deadline)
            raise

        def step(operation: str, fn: Callable[[], Any]) -> Any:
            # Every plot step sets a fixed value (or writes the same file again), so it is idempotent.
            return self._idempotent(operation, fn, deadline)

        media: str | None = None
        try:
            # Foreground plot: PlotToFile returns once the file is written. QuietErrorMode: plot errors come back
            # as COM errors instead of modal dialogs.
            step("set BACKGROUNDPLOT=0", lambda: doc.call("SetVariable", "BACKGROUNDPLOT", 0))
            step("set plot QuietErrorMode", lambda: setattr(plot, "QuietErrorMode", True))
            step(f"select plot device {device}", lambda: setattr(layout, "ConfigName", device))
            step("refresh plot device", lambda: layout.call("RefreshPlotDeviceInfo"))
            media = step("choose paper", lambda: self._choose_media(layout, str(saved["CanonicalMediaName"])))
            if media is not None:
                step(f"select paper {media}", lambda: setattr(layout, "CanonicalMediaName", media))
            step("plot extents", lambda: setattr(layout, "PlotType", AC_PLOT_TYPE_EXTENTS))
            step("use a standard scale", lambda: setattr(layout, "UseStandardScale", True))
            step("scale to fit", lambda: setattr(layout, "StandardScale", AC_SCALE_TO_FIT))
            step("center the plot", lambda: setattr(layout, "CenterPlot", True))
            plotted = bool(step("plot to file", lambda: plot.call("PlotToFile", str(path), device)))
        except BaseException as exc:
            restore_errors = self._restore_plot_settings(doc, layout, plot, backup, saved, background_plot,
                                                         quiet_errors, deadline)
            if not restore_errors:
                raise
            error = exc if isinstance(exc, ToolError) else (
                com.map_com_error(exc, "plot PDF") if com.is_com_error(exc) else None)
            if error is None:
                raise
            error.message += "; restoring the layout's plot settings also failed: " + "; ".join(restore_errors)
            error.details["restore_errors"] = restore_errors
            if error is exc:
                raise
            raise error from exc
        restore_errors = self._restore_plot_settings(doc, layout, plot, backup, saved, background_plot, quiet_errors,
                                                     deadline)
        self._wait_for_file(path, deadline)
        return PdfResult(path=path, device=device, media=media, layout=layout_name, plotted=plotted,
                         restore_errors=restore_errors)

    @staticmethod
    def _choose_media(layout: Any, current: str) -> str | None:
        names = [str(n) for n in (layout.call("GetCanonicalMediaNames") or ())]
        if current in names:
            return current
        for preferred in PDF_MEDIA_PREFERENCE:
            if preferred in names:
                return preferred
        return names[0] if names else None

    def _restore_plot_settings(self, doc: Any, layout: Any, plot: Any, backup: Any, saved: Mapping[str, Any],
                               background_plot: Any, quiet_errors: Any, deadline: float) -> list[str]:
        """Put every changed setting back; each failure is collected (and logged) and reported to the caller."""
        errors: list[str] = []

        def attempt(label: str, fn: Callable[[], Any]) -> None:
            try:
                self._idempotent(f"restore {label}", fn, deadline)
            except ToolError as err:
                logger.error("restoring %s after the PDF plot failed: %s", label, err.message)
                errors.append(f"{label}: {err.code}")
            except Exception as exc:
                if not com.is_com_error(exc):
                    raise
                logger.error("restoring %s after the PDF plot failed: %s", label, com.describe(exc))
                errors.append(f"{label}: {com.describe(exc)}")

        attempt("layout plot settings", lambda: layout.call("CopyFrom", backup))
        cleanup_error = self._delete_temporary(backup, "page setup", deadline)
        if cleanup_error is not None:
            errors.append(f"temporary page setup: {cleanup_error}")
        attempt("plot QuietErrorMode", lambda: setattr(plot, "QuietErrorMode", quiet_errors))
        attempt("BACKGROUNDPLOT", lambda: doc.call("SetVariable", "BACKGROUNDPLOT", background_plot))
        for name in PLOT_VERIFY_SETTINGS:
            def check(n: str = name) -> None:
                actual = getattr(layout, n)
                if actual != saved[n]:
                    errors.append(f"layout {n} is {actual!r} after restoring, expected {saved[n]!r}")

            attempt(f"verify layout {name}", check)
        return errors

    def _wait_for_file(self, path: Path, deadline: float) -> None:
        settle = min(deadline, time.monotonic() + self.settings.com_call_timeout_s)
        while not (path.is_file() and path.stat().st_size > 0) and time.monotonic() + FILE_POLL_INTERVAL_S < settle:
            com.pump_sleep(FILE_POLL_INTERVAL_S)
