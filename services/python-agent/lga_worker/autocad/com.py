"""AutoCAD COM plumbing: HRESULT classification, busy retry, VARIANT helpers and instance identity.

Adapted from AutoCAD-MCP (https://github.com/beiming183-cloud/AutoCAD-MCP, commit 11f7c47, MIT,
(c) 2024 AutoCAD MCP Server Contributors): the RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER retry with
exponential backoff while pumping messages, only for idempotent calls (`src/autocad_mcp/com_sta.py`); signed
HRESULT extraction (`com_hresult`); VARIANT(VT_ARRAY|VT_R8) points (`backends/file_ipc.py` `_solid_point`); and
the check that the COM application's HWND belongs to the expected acad.exe process (`_get_autocad_application`).

Design notes:
* Everything here that touches COM runs on the AutoCAD `StaExecutor` thread. `pythoncom` / `win32com` are
  imported lazily inside the functions that need them so that they are first used on that thread, and so the
  pure helpers can be unit-tested with fakes.
* AutoCAD's message filter rejects incoming calls while it is busy (regen, a modal dialog, a running command).
  Idempotent calls are retried with a bounded exponential backoff (50 ms doubling to 0.8 s) until a deadline;
  non-idempotent calls are never retried and fail with BUSY so the planner decides.
* Every COM failure maps to one deterministic `ToolError` code (`map_com_error`).
"""

from __future__ import annotations

import logging
import time
import winreg
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

# --- HRESULTs (signed 32-bit, as pywin32 reports them) -------------------------------------------------------
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001: the server's message filter rejected the call (busy)
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A: the server asked to retry later (busy)
RPC_E_SERVER_DIED = -2147418110  # 0x80010007
RPC_E_SERVER_DIED_DNE = -2147418094  # 0x80010012
RPC_E_DISCONNECTED = -2147417848  # 0x80010108: the object's server is gone
RPC_S_SERVER_UNAVAILABLE = -2147023174  # 0x800706BA
RPC_S_CALL_FAILED = -2147023170  # 0x800706BE
CO_E_OBJNOTCONNECTED = -2147220995  # 0x800401FD
MK_E_UNAVAILABLE = -2147221021  # 0x800401E3: GetActiveObject found nothing in the Running Object Table
DISP_E_EXCEPTION = -2147352567  # 0x80020009: the real error is the scode inside excepinfo

BUSY_HRESULTS = frozenset({RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER})
DISCONNECTED_HRESULTS = frozenset({
    RPC_E_SERVER_DIED, RPC_E_SERVER_DIED_DNE, RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE, RPC_S_CALL_FAILED,
    CO_E_OBJNOTCONNECTED, MK_E_UNAVAILABLE,
})

_HRESULT_MODULUS = 1 << 32
_HRESULT_SIGN_LIMIT = 0x7FFFFFFF
# pywintypes.com_error args: (hresult, strerror, excepinfo, argerror);
# excepinfo: (wcode, source, description, helpfile, helpcontext, scode).
_ARG_STRERROR = 1
_ARG_EXCEPINFO = 2
_EXCEPINFO_DESCRIPTION = 2
_EXCEPINFO_SCODE = 5

# --- Busy retry ---------------------------------------------------------------------------------------------
BACKOFF_INITIAL_S = 0.05
BACKOFF_MAX_S = 0.8
PUMP_SLICE_S = 0.01

BUSY_HINT = ("AutoCAD is busy: a dialog may be open or a command may be running in AutoCAD. "
             "Finish or cancel it in AutoCAD (Esc), then retry")
NOT_RUNNING_HINT = "AutoCAD is not running or stopped answering COM; call autocad.launch first"

# --- VARIANT -----------------------------------------------------------------------------------------------
_VT_R8 = 5
_VT_ARRAY = 0x2000


def to_signed_hresult(value: int) -> int:
    value = int(value) % _HRESULT_MODULUS
    return value - _HRESULT_MODULUS if value > _HRESULT_SIGN_LIMIT else value


def _com_error_type() -> type[BaseException]:
    import pywintypes

    return pywintypes.com_error


def is_com_error(exc: BaseException) -> bool:
    return isinstance(exc, _com_error_type())


def hresults(exc: BaseException) -> tuple[int, ...]:
    """The HRESULTs carried by a pywin32 `com_error`: the outer one and, for DISP_E_EXCEPTION, the inner scode."""
    args = getattr(exc, "args", ()) or ()
    found: list[int] = []
    outer = getattr(exc, "hresult", None)
    if outer is None and args:
        outer = args[0]
    if isinstance(outer, int):
        found.append(to_signed_hresult(outer))
    excepinfo = getattr(exc, "excepinfo", None)
    if excepinfo is None and len(args) > _ARG_EXCEPINFO:
        excepinfo = args[_ARG_EXCEPINFO]
    scode = excepinfo[_EXCEPINFO_SCODE] if isinstance(excepinfo, tuple) and len(excepinfo) > _EXCEPINFO_SCODE else None
    if isinstance(scode, int) and scode:
        found.append(to_signed_hresult(scode))
    return tuple(found)


def is_busy(exc: BaseException) -> bool:
    return any(h in BUSY_HRESULTS for h in hresults(exc))


def is_disconnected(exc: BaseException) -> bool:
    return any(h in DISCONNECTED_HRESULTS for h in hresults(exc))


def describe(exc: BaseException) -> str:
    """Human-readable text of a COM error: AutoCAD's own description when present, plus the HRESULT."""
    args = getattr(exc, "args", ()) or ()
    text = ""
    excepinfo = args[_ARG_EXCEPINFO] if len(args) > _ARG_EXCEPINFO else None
    if isinstance(excepinfo, tuple) and len(excepinfo) > _EXCEPINFO_DESCRIPTION and excepinfo[_EXCEPINFO_DESCRIPTION]:
        text = str(excepinfo[_EXCEPINFO_DESCRIPTION]).strip()
    if not text and len(args) > _ARG_STRERROR and args[_ARG_STRERROR]:
        text = str(args[_ARG_STRERROR]).strip()
    codes = ", ".join(f"0x{h % _HRESULT_MODULUS:08X}" for h in hresults(exc))
    return f"{text or type(exc).__name__} ({codes})" if codes else (text or repr(exc))


def map_com_error(exc: BaseException, operation: str) -> ToolError:
    """Deterministic mapping of a COM failure to the tool error model."""
    details = {"operation": operation, "hresults": [f"0x{h % _HRESULT_MODULUS:08X}" for h in hresults(exc)]}
    if is_busy(exc):
        return ToolError(ErrorCode.BUSY, f"{operation}: {BUSY_HINT}", details=details)
    if is_disconnected(exc):
        return ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"{operation}: {NOT_RUNNING_HINT}", details=details)
    return ToolError(ErrorCode.EXECUTION_FAILED, f"{operation} failed: {describe(exc)}", details=details)


def pump_messages() -> None:
    import pythoncom

    pythoncom.PumpWaitingMessages()


def pump_sleep(seconds: float, pump: Callable[[], None] | None = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
    """Wait while pumping the STA's message queue (a blocked STA would stall AutoCAD's callbacks)."""
    pump = pump or pump_messages
    end = clock() + max(0.0, seconds)
    while True:
        pump()
        left = end - clock()
        if left <= 0:
            return
        sleep(min(PUMP_SLICE_S, left))


def backoff_delays(initial: float = BACKOFF_INITIAL_S, maximum: float = BACKOFF_MAX_S) -> Iterable[float]:
    delay = initial
    while True:
        yield delay
        delay = min(delay * 2, maximum)


def retry_busy[T](fn: Callable[[], T], *, operation: str, idempotent: bool, deadline: float,
                  pump: Callable[[], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> T:
    """Call `fn`; while AutoCAD rejects it as busy, retry (idempotent calls only) until `deadline`.

    Raises ToolError(BUSY) when retries are exhausted or the call is not idempotent. Every other exception
    propagates unchanged.
    """
    attempts = 0
    delays = iter(backoff_delays())
    while True:
        attempts += 1
        try:
            return fn()
        except Exception as exc:
            if not is_com_error(exc) or not is_busy(exc):
                raise
            delay = next(delays)
            if not idempotent or clock() + delay > deadline:
                why = "not retried (not idempotent)" if not idempotent else f"still busy after {attempts} attempts"
                raise ToolError(ErrorCode.BUSY, f"{operation}: {BUSY_HINT} ({why})",
                                details={"operation": operation, "attempts": attempts}) from exc
            logger.debug("%s rejected by AutoCAD (busy); retry %d in %.2fs", operation, attempts, delay)
            pump_sleep(delay, pump=pump, sleep=sleep, clock=clock)


# --- Points and VARIANTs ------------------------------------------------------------------------------------

def point_xyz(point: Mapping[str, Any]) -> tuple[float, float, float]:
    """A contract point {"x", "y", "z"?} as a 3-tuple of floats (z defaults to 0)."""
    return float(point["x"]), float(point["y"]), float(point.get("z", 0.0) or 0.0)


def double_array(values: Iterable[float], pythoncom: Any = None, client: Any = None) -> Any:
    """VARIANT(VT_ARRAY | VT_R8) of doubles, the form AutoCAD's ActiveX API requires for points and vertex lists."""
    if pythoncom is None:
        import pythoncom
    if client is None:
        from win32com import client
    vt = getattr(pythoncom, "VT_ARRAY", _VT_ARRAY) | getattr(pythoncom, "VT_R8", _VT_R8)
    return client.VARIANT(vt, [float(v) for v in values])


def point_variant(point: Mapping[str, Any], pythoncom: Any = None, client: Any = None) -> Any:
    return double_array(point_xyz(point), pythoncom, client)


# --- Late-bound accessor -------------------------------------------------------------------------------------

DISPATCH_METHOD = 1
DISPATCH_PROPERTYGET = 2
DISPATCH_PROPERTYPUT = 4
LOCALE_NEUTRAL = 0


class ComObject:
    """Late-bound IDispatch wrapper whose failures are always the real `com_error`.

    win32com's dynamic `CDispatch` resolves names with GetIDsOfNames and converts *any* failure of that call into
    `AttributeError` - including AutoCAD rejecting it because it is busy - which would hide RPC_E_CALL_REJECTED from
    the busy retry. This wrapper calls GetIDsOfNames / Invoke directly so every failure keeps its HRESULT.

    Properties are read and written as attributes (`doc.Name`, `entity.Layer = "0"`); methods are invoked with
    `call("Name", *args)`. Returned IDispatch pointers are wrapped again.
    """

    __slots__ = ("_dispids", "_ole")

    def __init__(self, ole: Any) -> None:
        object.__setattr__(self, "_ole", ole)
        object.__setattr__(self, "_dispids", {})

    def _dispid(self, name: str) -> int:
        dispid = self._dispids.get(name)
        if dispid is None:
            dispid = self._ole.GetIDsOfNames(LOCALE_NEUTRAL, name)
            self._dispids[name] = dispid
        return dispid

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return wrap(self._ole.Invoke(self._dispid(name), LOCALE_NEUTRAL, DISPATCH_PROPERTYGET, True))

    def __setattr__(self, name: str, value: Any) -> None:
        self._ole.Invoke(self._dispid(name), LOCALE_NEUTRAL, DISPATCH_PROPERTYPUT, False, unwrap(value))

    def call(self, name: str, *args: Any) -> Any:
        flags = DISPATCH_METHOD | DISPATCH_PROPERTYGET
        return wrap(self._ole.Invoke(self._dispid(name), LOCALE_NEUTRAL, flags, True, *(unwrap(a) for a in args)))


def unwrap(value: Any) -> Any:
    """The raw IDispatch of a wrapped object (for passing COM objects as arguments); plain values pass through."""
    return object.__getattribute__(value, "_ole") if isinstance(value, ComObject) else value


def wrap(value: Any) -> Any:
    """Wrap a raw IDispatch pointer returned by Invoke; plain values pass through."""
    if isinstance(value, ComObject) or value is None or isinstance(value, str | int | float | bool | tuple):
        return value
    if callable(getattr(value, "GetIDsOfNames", None)) and callable(getattr(value, "Invoke", None)):
        return ComObject(value)
    return value


# --- Instance identity ---------------------------------------------------------------------------------------

def window_pid(hwnd: int) -> int:
    import win32process

    _, pid = win32process.GetWindowThreadProcessId(int(hwnd))
    return int(pid)


def get_active_application(prog_id: str) -> ComObject | None:
    """The running AutoCAD registered in the Running Object Table, or None. Never starts AutoCAD."""
    import pythoncom
    import pywintypes

    try:
        unknown = pythoncom.GetActiveObject(pywintypes.IID(prog_id))
    except Exception as exc:
        if is_com_error(exc) and MK_E_UNAVAILABLE in hresults(exc):
            return None
        raise
    return ComObject(unknown.QueryInterface(pythoncom.IID_IDispatch))


def com_server_executable(prog_id: str) -> Path | None:
    """The executable registered as the ProgID's COM LocalServer32 (the AutoCAD that COM talks to)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\CLSID") as key:
            clsid = str(winreg.QueryValueEx(key, "")[0])
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
            command = str(winreg.QueryValueEx(key, "")[0]).strip()
    except OSError:
        return None
    if command.startswith('"'):
        executable = command[1:].split('"', 1)[0]
    else:
        lowered = command.lower()
        end = lowered.find(".exe")
        executable = command[: end + len(".exe")] if end >= 0 else command.split(" ", 1)[0]
    return Path(executable)
