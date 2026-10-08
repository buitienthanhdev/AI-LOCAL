"""Real-AutoCAD validation of the autocad.* tools through the tool registry (contract validation included).

Starts AutoCAD if it is not running (startup can take minutes), creates a drawing, draws, verifies, saves, exports
DXF and PDF and erases. Test drawings are closed without saving at the end; AutoCAD is left running.

Run: uv run pytest -m autocad --run-autocad -v tests/test_autocad_live.py
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from lga_common.config import Config

from lga_worker.autocad.provider import AutoCadProvider
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext, ToolOutcome, ToolRegistry
from lga_worker.server import build_registry
from lga_worker.services import Services

pytestmark = pytest.mark.autocad
CLEANUP_TIMEOUT_S = 30.0
LAYER = "LGA_TEST"
RED = 1


class Harness:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry: ToolRegistry = build_registry(config.path("paths.tools_dir"))
        self.services = Services(config)
        self.opened: list[str] = []

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> ToolOutcome:
        spec = self.registry.specs[tool]
        ctx = ToolContext(request_id=uuid.uuid4().hex, task_id="autocad-live", step_id=tool,
                          deadline=time.monotonic() + spec.timeout_ms / 1000.0, config=self.config,
                          services=self.services, cancel_event=threading.Event())
        _, outcome = await self.registry.invoke(ctx, tool, args or {})
        expected = "not_applicable" if spec.verification == "none" else "passed"
        assert outcome.verification_status(spec.verification) == expected, (tool, outcome.checks)
        print(f"{tool}: {outcome.observation}")
        return outcome

    async def error(self, tool: str, args: dict[str, Any]) -> ToolError:
        with pytest.raises(ToolError) as exc:
            await self.call(tool, args)
        print(f"{tool}: {exc.value.code} {exc.value.message}")
        return exc.value

    async def close(self) -> None:
        provider: AutoCadProvider | None = self.services.peek("autocad")
        try:
            if provider is not None:
                for name in self.opened:
                    closed = await provider.executor.run(CLEANUP_TIMEOUT_S, provider.close_document, name, False,
                                                         deadline=time.monotonic() + CLEANUP_TIMEOUT_S - 1)
                    print(f"closed {name}: {closed}")
        finally:
            await self.services.close()


async def test_autocad_end_to_end(config: Config, tmp_path: Path) -> None:
    h = Harness(config)
    try:
        status = (await h.call("autocad.status")).data
        assert status["installed"] and status["com_registered"] and status["ready"]

        launched = (await h.call("autocad.launch")).data
        assert launched["pid"] > 0 and launched["hwnd"] > 0 and launched["version"]
        again = (await h.call("autocad.launch")).data
        assert again["launched"] is False and again["pid"] == launched["pid"]

        status = (await h.call("autocad.status")).data
        assert status["running"] and status["com_responsive"] and status["pid"] == launched["pid"]

        doc = (await h.call("autocad.new_document")).data
        h.opened.append(doc["name"])
        documents = (await h.call("autocad.list_documents")).data["documents"]
        assert [d["name"] for d in documents if d["active"]] == [doc["name"]]

        # A layer must exist before it is used.
        missing = await h.error("autocad.create_line", {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1},
                                                        "layer": LAYER})
        assert missing.code == ErrorCode.NOT_FOUND
        layer = (await h.call("autocad.create_layer", {"name": LAYER, "color": RED})).data
        assert layer == {"name": LAYER, "color": RED, "created": True}
        assert (await h.call("autocad.create_layer", {"name": LAYER, "color": RED})).data["created"] is False

        line = (await h.call("autocad.create_line", {"start": {"x": -150, "y": -150}, "end": {"x": 150, "y": 150},
                                                     "layer": LAYER})).data
        assert line["object_name"] == "AcDbLine" and line["layer"] == LAYER
        circle = (await h.call("autocad.create_circle", {"center": {"x": 0, "y": 0}, "radius": 100,
                                                         "layer": LAYER})).data
        assert circle["radius"] == 100 and circle["center"] == {"x": 0.0, "y": 0.0, "z": 0.0}
        square = [{"x": -120, "y": -120}, {"x": 120, "y": -120}, {"x": 120, "y": 120}, {"x": -120, "y": 120}]
        pline = (await h.call("autocad.create_polyline", {"points": square, "closed": True})).data
        assert pline["vertex_count"] == 4 and pline["closed"] is True
        text = (await h.call("autocad.create_text", {"insertion": {"x": -100, "y": 130}, "text": "LGA live test",
                                                     "height": 10, "rotation_deg": 0, "layer": LAYER})).data
        assert text["text"] == "LGA live test"

        verified = (await h.call("autocad.verify_geometry", {
            "handle": circle["handle"],
            "expect": {"object_name": "circle", "center": {"x": 0, "y": 0}, "radius": 100, "layer": LAYER}})).data
        assert verified["matches"] is True and verified["mismatches"] == []
        wrong = (await h.call("autocad.verify_geometry", {"handle": circle["handle"], "expect": {"radius": 99}})).data
        assert wrong["matches"] is False and wrong["mismatches"][0]["field"] == "radius"

        circles = (await h.call("autocad.get_entities", {"object_name": "circle"})).data
        assert [e["handle"] for e in circles["entities"]] == [circle["handle"]]
        on_layer = (await h.call("autocad.get_entities", {"layer": LAYER.lower()})).data
        assert {e["handle"] for e in on_layer["entities"]} == {line["handle"], circle["handle"], text["handle"]}

        # DXF export needs a saved DWG first.
        dxf = tmp_path / "out" / "drawing.dxf"
        unsaved = await h.error("autocad.export_dxf", {"path": str(dxf)})
        assert unsaved.code == ErrorCode.CONFLICT

        await h.call("autocad.zoom_extents")
        dwg = tmp_path / "out" / "drawing.dwg"
        saved = (await h.call("autocad.save", {"path": str(dwg)})).data
        assert dwg.is_file() and saved["size"] == dwg.stat().st_size > 0
        h.opened = [dwg.name]

        exported = (await h.call("autocad.export_dxf", {"path": str(dxf)})).data
        assert dxf.is_file() and exported["size"] > 0
        assert Path(exported["document_path"]).name.lower() == dwg.name.lower()
        exists = await h.error("autocad.export_dxf", {"path": str(dxf)})
        assert exists.code == ErrorCode.CONFLICT
        documents = (await h.call("autocad.list_documents")).data["documents"]
        active = [d for d in documents if d["active"]]
        assert len(active) == 1 and active[0]["full_name"].lower().endswith(".dwg")

        pdf = tmp_path / "out" / "drawing.pdf"
        plotted = (await h.call("autocad.export_pdf", {"path": str(pdf)})).data
        assert pdf.read_bytes()[:4] == b"%PDF" and plotted["size"] > 0

        erased = (await h.call("autocad.erase", {"handle": line["handle"]})).data
        assert erased == {"handle": line["handle"], "object_name": "AcDbLine", "erased": True}
        gone = (await h.call("autocad.verify_geometry", {"handle": line["handle"],
                                                          "expect": {"object_name": "line"}})).data
        assert gone["matches"] is False and gone["actual"] is None
        again_erase = await h.error("autocad.erase", {"handle": line["handle"]})
        assert again_erase.code == ErrorCode.NOT_FOUND
    finally:
        await h.close()
