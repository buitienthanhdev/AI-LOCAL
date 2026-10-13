"""cad.build / cad.inspect: contract, argument checks and (when the CAD runtime is installed) real builds."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services

BRACKET = ("WIDTH = 60\nDEPTH = 30\nBASE_T = 6\nWALL_H = 30\nHOLE_D = 8\n"
           "base = bd.Box(WIDTH, DEPTH, BASE_T)\n"
           "wall = bd.Pos(0, -(DEPTH/2 - 3), BASE_T/2 + WALL_H/2) * bd.Box(WIDTH, 6, WALL_H)\n"
           "result = base + wall\n"
           "for x in (-20, 20):\n    result = result - bd.Pos(x, 0, 0) * bd.Cylinder(HOLE_D/2, BASE_T + 4)\n")


def _runtime(config: Config) -> bool:
    return (config.root / "runtime" / "cad" / "Scripts" / "python.exe").is_file()


async def _call(config: Config, tool: str, args: dict[str, Any]) -> Any:
    registry = build_registry(config.path("paths.tools_dir"))
    ctx = ToolContext("r1", "cad-unit", "s1", time.monotonic() + 280, config, Services(config), threading.Event())
    return await registry.invoke(ctx, tool, args)


def test_contract_has_handlers(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    contract = json.loads((config.path("paths.tools_dir") / "cad.json").read_text("utf-8"))
    names = {t["name"] for t in contract["tools"]}
    assert names == {"cad.build", "cad.inspect"}
    assert names <= set(registry.available_tools())
    assert registry.specs["cad.build"].input_schema.get("additionalProperties") is False
    assert registry.specs["cad.build"].verification == "self"


async def test_code_without_result_is_rejected(config: Config) -> None:
    with pytest.raises(ToolError) as exc:
        await _call(config, "cad.build", {"name": "x", "code": "shape = bd.Box(1, 1, 1)\n"})
    assert exc.value.code in (ErrorCode.INVALID_ARGUMENT, ErrorCode.BACKEND_UNAVAILABLE)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[3] / "runtime/cad/Scripts/python.exe").is_file(),
                    reason="CAD runtime not installed (scripts/setup-cad.ps1)")
class TestRealBuild:
    async def test_bracket_is_measured_and_checked(self, config: Config) -> None:
        _, out = await _call(config, "cad.build", {"name": "unit_bracket", "code": BRACKET, "formats": ["stl"],
                                                   "expected_size_mm": [60, 30, 36]})
        m = out.data["measured"]
        assert Path(out.data["files"]["step"]).is_file()
        assert Path(out.data["files"]["stl"]).is_file()
        assert abs(m["volume_mm3"] - 20997) < 20
        assert m["solids"] == 1 and m["valid"] is True
        assert sorted(round(v) for v in m["size_mm"]) == [30, 36, 60]

    async def test_a_python_error_comes_back_as_a_traceback(self, config: Config) -> None:
        with pytest.raises(ToolError) as exc:
            await _call(config, "cad.build", {"name": "unit_bad", "code": "result = bd.Box(10, 10, 10) - bd.Box(5, 5, nope)\n"})
        assert exc.value.code == ErrorCode.EXECUTION_FAILED
        assert "nope" in exc.value.message

    async def test_wrong_size_fails_verification_with_the_measured_numbers(self, config: Config) -> None:
        with pytest.raises(ToolError) as exc:
            await _call(config, "cad.build", {"name": "unit_cube", "code": "result = bd.Box(10, 10, 10)\n",
                                              "formats": [], "expected_size_mm": [20, 20, 20]})
        assert exc.value.code == ErrorCode.VERIFICATION_FAILED
        assert "differs from the expected" in exc.value.message and "10.0" in exc.value.message

    async def test_imports_are_dropped_and_name_is_optional(self, config: Config) -> None:
        code = "from build123d import *\nimport build123d\nresult = Cylinder(10, 40) - Cylinder(7, 42)\n"
        _, out = await _call(config, "cad.build", {"code": code, "formats": [], "expected_size_mm": [20, 20, 40]})
        assert out.data["name"] == "part" and abs(out.data["measured"]["volume_mm3"] - 6409) < 5
