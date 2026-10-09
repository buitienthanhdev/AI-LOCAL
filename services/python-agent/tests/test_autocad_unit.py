"""Pure unit tests of the AutoCAD subsystem (no AutoCAD needed): geometry, COM helpers with fakes, contract."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pywintypes
from lga_common.config import Config

from lga_worker.autocad import com, geometry
from lga_worker.autocad.provider import (
    DXF_SAVE_TYPE_VERSIONS,
    AutoCadSettings,
    dxf_version,
    looks_like_dxf,
    same_file_path,
)
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.server import build_registry

TOL = 1e-6


def com_error(hresult: int, scode: int = 0, description: str = "") -> pywintypes.com_error:
    excepinfo = (0, "AutoCAD", description, None, 0, scode) if scode or description else None
    return pywintypes.com_error(hresult, "COM failure", excepinfo, None)


# --------------------------------------------------------------------------- geometry

def circle(**overrides: Any) -> dict[str, Any]:
    entity = {"handle": "2F3", "object_name": "AcDbCircle", "layer": "Walls",
              "center": {"x": 0.0, "y": 0.0, "z": 0.0}, "radius": 100.0}
    entity.update(overrides)
    return entity


def test_compare_match_within_tolerance() -> None:
    expect = {"object_name": "circle", "center": {"x": 0, "y": 5e-7}, "radius": 100.00000001, "layer": "WALLS"}
    assert geometry.compare(expect, circle(), TOL) == []


def test_compare_reports_each_mismatch() -> None:
    expect = {"object_name": "AcDbLine", "center": {"x": 1, "y": 0}, "radius": 99, "layer": "0"}
    fields = [m["field"] for m in geometry.compare(expect, circle(), TOL)]
    assert fields == ["object_name", "center", "radius", "layer"]


def test_compare_missing_field_is_mismatch() -> None:
    mismatches = geometry.compare({"start": {"x": 0, "y": 0}, "text": "A"}, circle(), TOL)
    assert [(m["field"], m["actual"]) for m in mismatches] == [("start", None), ("text", None)]


def test_compare_scales_tolerance_for_large_magnitudes() -> None:
    big = circle(center={"x": 1e9, "y": 0.0, "z": 0.0})
    assert geometry.compare({"center": {"x": 1e9 + 1e-4, "y": 0}}, big, TOL) == []
    assert geometry.compare({"center": {"x": 1e9 + 1e4, "y": 0}}, big, TOL)


def test_compare_polyline_and_angles() -> None:
    actual = {"object_name": "AcDbPolyline",
              "points": [{"x": 0.0, "y": 0.0, "z": 2.0}, {"x": 10.0, "y": 0.0, "z": 2.0}],
              "closed": True, "vertex_count": 2, "rotation_deg": 270.0}
    expect = {"points": [{"x": 0, "y": 0, "z": 2}, {"x": 10, "y": 0, "z": 2}], "closed": True, "vertex_count": 2,
              "rotation_deg": -90.0}
    assert geometry.compare(expect, actual, TOL) == []
    assert geometry.compare({"points": [{"x": 0, "y": 0, "z": 2}]}, actual, TOL)[0]["field"] == "points"
    assert geometry.compare({"closed": False}, actual, TOL)[0]["field"] == "closed"
    assert geometry.angle_close(359.9999999, 0.0, TOL)


def test_compare_unknown_field_is_invalid_argument() -> None:
    with pytest.raises(ToolError) as exc:
        geometry.compare({"colour": 1}, circle(), TOL)
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


def test_object_name_matching() -> None:
    assert geometry.object_name_matches("circle", "AcDbCircle")
    assert geometry.object_name_matches("AcDbPolyline", "acdbpolyline")
    assert not geometry.object_name_matches("line", "AcDbPolyline")


def test_polyline_elevation_requires_single_z() -> None:
    assert geometry.polyline_elevation([{"x": 0, "y": 0}, {"x": 1, "y": 1, "z": 0}]) == 0.0
    assert geometry.polyline_elevation([{"x": 0, "y": 0, "z": 5}, {"x": 1, "y": 1, "z": 5}]) == 5.0
    with pytest.raises(ToolError) as exc:
        geometry.polyline_elevation([{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 1, "z": 1}])
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


def test_entity_to_dict_from_fakes() -> None:
    line = SimpleNamespace(ObjectName="AcDbLine", Handle="1A", Layer="0", StartPoint=(0.0, 0.0, 0.0),
                           EndPoint=(3.0, 4.0, 0.0), Length=5.0)
    assert geometry.entity_to_dict(line) == {"handle": "1A", "object_name": "AcDbLine", "layer": "0",
                                             "start": {"x": 0.0, "y": 0.0, "z": 0.0},
                                             "end": {"x": 3.0, "y": 4.0, "z": 0.0}, "length": 5.0}
    pline = SimpleNamespace(ObjectName="AcDbPolyline", Handle="1B", Layer="A", Coordinates=(0.0, 0.0, 1.0, 2.0),
                            Elevation=7.0, Closed=True)
    data = geometry.entity_to_dict(pline)
    assert data["points"] == [{"x": 0.0, "y": 0.0, "z": 7.0}, {"x": 1.0, "y": 2.0, "z": 7.0}]
    assert data["closed"] is True and data["vertex_count"] == 2
    text = SimpleNamespace(ObjectName="AcDbText", Handle="1C", Layer="T", InsertionPoint=(1.0, 2.0, 0.0),
                           TextString="Hi", Height=2.5, Rotation=math.pi / 2)
    data = geometry.entity_to_dict(text)
    assert data["text"] == "Hi" and math.isclose(data["rotation_deg"], 90.0)
    other = SimpleNamespace(ObjectName="AcDbHatch", Handle="1D", Layer="H")
    assert geometry.entity_to_dict(other) == {"handle": "1D", "object_name": "AcDbHatch", "layer": "H"}


def test_entity_to_dict_does_not_swallow_property_errors() -> None:
    class Broken:
        ObjectName, Handle, Layer = "AcDbCircle", "1", "0"

        @property
        def Center(self) -> Any:
            raise com_error(com.DISP_E_EXCEPTION, scode=-2147467259)

    with pytest.raises(pywintypes.com_error):
        geometry.entity_to_dict(Broken())


# --------------------------------------------------------------------------- COM helpers

def test_hresult_classification() -> None:
    assert com.to_signed_hresult(0x80010001) == com.RPC_E_CALL_REJECTED
    assert com.is_busy(com_error(com.RPC_E_CALL_REJECTED))
    assert com.is_busy(com_error(com.DISP_E_EXCEPTION, scode=com.RPC_E_SERVERCALL_RETRYLATER))
    assert com.is_disconnected(com_error(com.RPC_S_SERVER_UNAVAILABLE))
    assert not com.is_busy(com_error(com.DISP_E_EXCEPTION, scode=-2145386484))
    assert "Unknown handle" in com.describe(com_error(com.DISP_E_EXCEPTION, -2145386484, "Unknown handle"))


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (com_error(com.RPC_E_CALL_REJECTED), ErrorCode.BUSY),
        (com_error(com.RPC_E_DISCONNECTED), ErrorCode.BACKEND_UNAVAILABLE),
        (com_error(com.MK_E_UNAVAILABLE), ErrorCode.BACKEND_UNAVAILABLE),
        (com_error(com.DISP_E_EXCEPTION, -2145386476, "Key not found"), ErrorCode.EXECUTION_FAILED),
    ],
)
def test_map_com_error(error: pywintypes.com_error, code: ErrorCode) -> None:
    mapped = com.map_com_error(error, "op")
    assert mapped.code == code
    if code == ErrorCode.BACKEND_UNAVAILABLE:
        assert "autocad.launch" in mapped.message


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def flaky(failures: int, error: BaseException) -> Any:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] <= failures:
            raise error
        return "ok"

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_retry_busy_recovers_with_exponential_backoff() -> None:
    clock = FakeClock()
    fn = flaky(4, com_error(com.RPC_E_CALL_REJECTED))
    result = com.retry_busy(fn, operation="read", idempotent=True, deadline=10.0, pump=lambda: None,
                            sleep=clock.sleep, clock=clock.clock)
    assert result == "ok" and fn.calls["n"] == 5
    assert math.isclose(clock.now, 0.05 + 0.1 + 0.2 + 0.4)


def test_retry_busy_backoff_is_capped() -> None:
    delays = com.backoff_delays()
    assert [next(delays) for _ in range(7)] == [0.05, 0.1, 0.2, 0.4, 0.8, 0.8, 0.8]


def test_retry_busy_gives_up_at_deadline_with_busy() -> None:
    clock = FakeClock()
    fn = flaky(1000, com_error(com.RPC_E_SERVERCALL_RETRYLATER))
    with pytest.raises(ToolError) as exc:
        com.retry_busy(fn, operation="read", idempotent=True, deadline=2.0, pump=lambda: None,
                       sleep=clock.sleep, clock=clock.clock)
    assert exc.value.code == ErrorCode.BUSY and exc.value.retryable
    assert clock.now <= 2.0


def test_retry_busy_never_retries_non_idempotent_calls() -> None:
    fn = flaky(1, com_error(com.RPC_E_CALL_REJECTED))
    with pytest.raises(ToolError) as exc:
        com.retry_busy(fn, operation="add", idempotent=False, deadline=100.0, pump=lambda: None,
                       sleep=lambda s: None)
    assert exc.value.code == ErrorCode.BUSY and fn.calls["n"] == 1


def test_retry_busy_propagates_other_errors_unchanged() -> None:
    error = com_error(com.DISP_E_EXCEPTION, -2145386476, "Key not found")
    with pytest.raises(pywintypes.com_error):
        com.retry_busy(flaky(1, error), operation="read", idempotent=True, deadline=100.0, pump=lambda: None)
    with pytest.raises(ValueError):
        com.retry_busy(flaky(1, ValueError("bug")), operation="read", idempotent=True, deadline=100.0)


def test_variant_helpers_with_fakes() -> None:
    fake_pythoncom = SimpleNamespace(VT_ARRAY=0x2000, VT_R8=5)
    fake_client = SimpleNamespace(VARIANT=lambda vt, value: (vt, value))
    assert com.point_variant({"x": 1, "y": 2}, fake_pythoncom, fake_client) == (0x2005, [1.0, 2.0, 0.0])
    assert com.double_array([0, 1, 2, 3], fake_pythoncom, fake_client) == (0x2005, [0.0, 1.0, 2.0, 3.0])
    assert com.point_xyz({"x": 1, "y": 2, "z": 3}) == (1.0, 2.0, 3.0)


def test_real_variant_type() -> None:
    variant = com.point_variant({"x": 1, "y": 2, "z": 3})
    import pythoncom

    assert variant.varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8
    assert list(variant.value) == [1.0, 2.0, 3.0]


class FakeDispatch:
    """Minimal PyIDispatch: GetIDsOfNames / Invoke over a dict of properties and methods."""

    def __init__(self, members: dict[str, Any], reject_names: int = 0) -> None:
        self.members = members
        self.names = list(members)
        self.reject_names = reject_names
        self.invocations: list[tuple[str, int, tuple[Any, ...]]] = []

    def GetIDsOfNames(self, lcid: int, name: str) -> int:
        if self.reject_names:
            self.reject_names -= 1
            raise com_error(com.RPC_E_CALL_REJECTED)
        if name not in self.members:
            raise com_error(-2147352570)  # DISP_E_UNKNOWNNAME
        return self.names.index(name)

    def Invoke(self, dispid: int, lcid: int, flags: int, result_wanted: bool, *args: Any) -> Any:
        name = self.names[dispid]
        self.invocations.append((name, flags, args))
        member = self.members[name]
        if flags == com.DISPATCH_PROPERTYPUT:
            self.members[name] = args[0]
            return None
        return member(*args) if callable(member) else member


def test_com_object_properties_methods_and_wrapping() -> None:
    child = FakeDispatch({"Name": "inner"})
    raw = FakeDispatch({"Name": "outer", "Child": child, "Add": lambda a, b: a + b})
    obj = com.ComObject(raw)
    assert obj.Name == "outer"
    assert obj.call("Add", 2, 3) == 5
    assert isinstance(obj.Child, com.ComObject) and obj.Child.Name == "inner"
    obj.Name = "renamed"
    assert raw.members["Name"] == "renamed"
    assert raw.invocations[-1][1] == com.DISPATCH_PROPERTYPUT


def test_com_object_surfaces_busy_name_lookup_as_com_error() -> None:
    """win32com's CDispatch would raise AttributeError here and hide that AutoCAD is busy."""
    obj = com.ComObject(FakeDispatch({"Version": "24.2"}, reject_names=2))
    value = com.retry_busy(lambda: obj.Version, operation="read", idempotent=True, deadline=1e9,
                           pump=lambda: None, sleep=lambda s: None)
    assert value == "24.2"
    with pytest.raises(pywintypes.com_error):
        _ = com.ComObject(FakeDispatch({})).Missing


# --------------------------------------------------------------------------- provider helpers

def test_looks_like_dxf() -> None:
    assert looks_like_dxf(b"  0\r\nSECTION\r\n  2\r\nHEADER\r\n")
    assert looks_like_dxf(b"999\r\ncomment\r\n  0\r\nSECTION\r\n")
    assert looks_like_dxf(b"AutoCAD Binary DXF\r\n\x1a\x00")
    assert not looks_like_dxf(b"%PDF-1.5")
    assert not looks_like_dxf(b"AC1032")


def test_dxf_version() -> None:
    head = b"  0\r\nSECTION\r\n  2\r\nHEADER\r\n  9\r\n$ACADVER\r\n  1\r\nAC1032\r\n  9\r\n$ACADMAINTVER\r\n"
    assert dxf_version(head) == "AC1032"
    assert dxf_version(b"  0\r\nSECTION\r\n") is None
    assert DXF_SAVE_TYPE_VERSIONS[65] == "AC1032"


def test_same_file_path(tmp_path: Path) -> None:
    target = tmp_path / "A.dwg"
    assert same_file_path(str(target), str(target).upper())
    assert not same_file_path("", str(target))
    assert not same_file_path(str(target), str(tmp_path / "B.dwg"))


def test_settings_from_config(config: Config) -> None:
    settings = AutoCadSettings.from_config(config)
    assert settings.launch_timeout_s > settings.com_call_timeout_s > settings.busy_retry_s > 0
    assert settings.tolerance > 0 and settings.pdf_plot_device


# --------------------------------------------------------------------------- contract

def test_every_autocad_tool_has_a_handler(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    contract = json.loads((config.path("paths.tools_dir") / "autocad.json").read_text("utf-8"))
    names = {t["name"] for t in contract["tools"]}
    assert len(names) == 17
    assert not [t for t in registry.unbound_tools() if t.startswith("autocad.")]
    assert names <= set(registry.available_tools())


def test_contract_conventions(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    specs = {n: s for n, s in registry.specs.items() if n.startswith("autocad.")}
    for name, spec in specs.items():
        assert spec.input_schema.get("additionalProperties") is False, name
        if spec.policy_category == "autocad.read":
            assert spec.verification == "none", name
        else:
            assert spec.verification == "self", name
    assert specs["autocad.erase"].policy_category == "autocad.destructive"
    assert specs["autocad.launch"].timeout_ms >= 180_000
    assert "matches" in specs["autocad.verify_geometry"].output_schema["required"]
