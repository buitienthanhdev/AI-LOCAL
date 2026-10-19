"""Unit tests (no AutoCAD) of the shared house model, the elevation/section/schedule views, the cross-view validation
rules and the tool handlers (with a fake provider).

The live check against the real AutoCAD is logs/dev/autocad_views_live.py.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from lga_common.config import Config

from lga_worker.autocad import floorplan, housemodel, houseviews, layers, validation
from lga_worker.autocad.provider import PrimsResult
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services

PARAMS = {"width": 8, "depth": 18, "bedrooms": 3}
NEW_RULES = ("COORD.ELEVATION_MATCHES_PLAN", "COORD.SCHEDULE_MATCHES_ELEVATION", "COORD.LEVELS_ELEVATION_SECTION",
             "COORD.SECTION_MARKER_VIEW", "COORD.ROOM_TABLE_AREA")


def model_for(**kw: Any) -> housemodel.HouseModel:
    return housemodel.build_house_model({**PARAMS, **kw})


def all_records(model: housemodel.HouseModel, cut: houseviews.Cut | None = None) -> list[dict[str, Any]]:
    out = [houseviews.elevation_view(model, s).record for s in housemodel.SIDES]
    out.append(houseviews.section_view(model, cut or houseviews.Cut()).record)
    out += [v.record for v in houseviews.schedule_views(model)]
    return copy.deepcopy(out)


def validate(model_dict: dict[str, Any], **kw: Any) -> validation.ValidationReport:
    return validation.run_validation(validation.DrawingModel.from_dict(model_dict), **kw)


def result_of(report: validation.ValidationReport, rule_id: str) -> validation.RuleResult:
    return next(r for r in report.results if r.rule_id == rule_id)


# --------------------------------------------------------------------------- the shared model

def test_model_uses_the_numbers_of_the_plan() -> None:
    model = model_for()
    plan = model.plan
    assert [o.kind for o in model.openings] == [o.kind for o in plan.openings]
    for mo, po in zip(model.openings, plan.openings):
        assert (mo.orient, mo.line, mo.a, mo.b) == (po.orient, po.line, po.a, po.b)
        assert mo.width == pytest.approx(po.b - po.a)
    listed = [r for r in plan.rooms if r.kind not in ("corridor", "annex")]
    assert [r.name for r in model.listed_rooms] == [r.name for r in listed]
    for mr, pr in zip(model.listed_rooms, listed):
        assert mr.area == pytest.approx(pr.total_area)
    assert {w.thickness for w in model.walls} == {floorplan.EXTERIOR_WALL, floorplan.INTERIOR_WALL}


def test_l_shaped_bedroom_boundary_area_equals_plan_area() -> None:
    plan = floorplan.plan_house(8, 24, bedrooms=2)
    assert any(r.kind == "annex" for r in plan.rooms)
    model = housemodel.build_house_model({"width": 8, "depth": 24, "bedrooms": 2})
    for room in model.rooms:
        if room.kind == "bedroom":
            expected = next(r.total_area for r in plan.rooms if r.name == room.name and r.kind == "bedroom")
            assert room.area == pytest.approx(expected)
            if len(room.boundary) == 6:
                return
    pytest.fail("no L-shaped bedroom found")


def test_every_opening_has_a_host_wall_and_unique_code() -> None:
    model = model_for()
    ids = {w.id for w in model.walls}
    assert all(o.wall_id in ids for o in model.openings)
    codes = [o.code for o in model.openings]
    assert len(codes) == len(set(codes))
    for o in model.openings:
        wall = model.wall(o.wall_id)
        assert wall.orient == o.orient and wall.a - 1e-6 <= o.a and o.b <= wall.b + 1e-6
        if o.facade:
            assert wall.exterior
    assert {o.code[0] for o in model.openings} == {"D", "W", "P"}


def test_defaults_are_flagged_assumed_and_given_values_are_provided() -> None:
    model = model_for()
    assert {a["field"] for a in model.assumptions} >= {"storey_height", "plinth_height", "door_height", "window_sill", "datum"}
    assert all(a["source_status"] == "ASSUMED" and a["source"] for a in model.assumptions)
    ent = {e.entity_id: e for e in model.entities()}
    assert ent["D2"].properties["size_status"] == "ASSUMED"
    given = model_for(storey_height=3.0, door_height=2.2)
    fields = {a["field"] for a in given.assumptions}
    assert "storey_height" not in fields and "door_height" not in fields
    assert given.elevation(housemodel.LEVEL_CEILING) == 3.0
    ent = {e.entity_id: e for e in given.entities()}
    assert ent["D2"].properties["size_status"] == "PROVIDED" and ent["D2"].properties["height"] == 2.2


def test_levels_and_datum() -> None:
    model = model_for()
    assert model.elevation(housemodel.LEVEL_FLOOR) == 0.0
    assert model.elevation(housemodel.LEVEL_GROUND) == pytest.approx(-0.45)
    assert model.elevation(housemodel.LEVEL_ROOF) == pytest.approx(3.4)
    assert model.elevation(housemodel.LEVEL_RIDGE) > model.elevation(housemodel.LEVEL_ROOF)
    flat = model_for(roof_type="flat")
    assert flat.has_level(housemodel.LEVEL_PARAPET) and not flat.has_level(housemodel.LEVEL_RIDGE)


def test_bad_vertical_numbers_are_refused() -> None:
    with pytest.raises(housemodel.HouseModelError):
        model_for(storey_height=2.0)
    with pytest.raises(housemodel.HouseModelError):
        model_for(window_sill=2.0, window_height=1.5)
    with pytest.raises(housemodel.HouseModelError):
        model_for(roof_type="dome")
    with pytest.raises(housemodel.HouseModelError):
        housemodel.build_house_model({"width": 2, "depth": 3})


# --------------------------------------------------------------------------- elevations

def test_elevation_openings_come_from_the_plan_exactly_once() -> None:
    model = model_for()
    seen: dict[str, dict[str, Any]] = {}
    for side in housemodel.SIDES:
        view = houseviews.elevation_view(model, side)
        for o in view.record["openings"]:
            assert o["code"] not in seen
            seen[o["code"]] = o
    exterior = [o for o in model.openings if o.facade and o.etype != "OPENING"]
    assert sorted(seen) == sorted(o.code for o in exterior)
    for o in exterior:
        got = seen[o.code]
        assert got["width"] == pytest.approx(o.width) and got["height"] == pytest.approx(o.height)
        assert got["sill_height"] == pytest.approx(o.sill) and got["facade"] == o.facade


def test_elevation_levels_labels_and_layers() -> None:
    model = model_for()
    view = houseviews.elevation_view(model, "front")
    marks = {m["level_id"]: m for m in view.record["levels"]}
    assert marks[housemodel.LEVEL_FLOOR]["label"] == "+0.000"
    assert marks[housemodel.LEVEL_ROOF]["label"] == "+3.400"
    assert marks[housemodel.LEVEL_GROUND]["label"] == "-0.450"
    for m in marks.values():
        assert m["label_value"] == pytest.approx(m["elevation"], abs=5e-4)
    std = set(layers.STANDARD_LAYERS)
    assert set(view.layers_used()) <= std
    assert {"A-WALL-EXT", "A-ROOF", "A-DOOR", "A-DIMS", "A-ANNO-LEVEL", "A-SITE-CONTEXT"} <= set(view.layers_used())
    assert view.expected_entities() == len(view.prims)  # no hatches in an elevation


def test_elevation_rejects_unknown_side_and_handles_flat_roof() -> None:
    with pytest.raises(ValueError):
        houseviews.elevation_view(model_for(), "top")
    flat = houseviews.elevation_view(model_for(roof_type="flat"), "left")
    assert {m["level_id"] for m in flat.record["levels"]} >= {housemodel.LEVEL_PARAPET}


def test_layer_map_renames_layers_for_the_file() -> None:
    view = houseviews.elevation_view(model_for(), "right")
    lm = layers.LayerMap({"A-DOOR": "OFFICE-DOOR"})
    mapped = layers.view_layers(view.layers_used(), lm)
    assert mapped["A-DOOR"] == ("OFFICE-DOOR", 1) and mapped["A-WINDOW"][0] == "A-WINDOW"
    with pytest.raises(layers.LayerMapError):
        layers.view_layers(["KT-TUONG"])


# --------------------------------------------------------------------------- sections

def test_section_default_cut_and_contents() -> None:
    model = model_for()
    cut = houseviews.check_cut(model, houseviews.default_cut(model))
    view = houseviews.section_view(model, cut)
    rec = view.record
    assert rec["cut"] == {"axis": "y", "position": cut.position, "direction": 1}
    assert rec["slabs"] == ["SLAB-FLOOR", "SLAB-ROOF"] and rec["cut_walls"]
    assert rec["clear_heights"] and all(c["height"] == pytest.approx(3.3) for c in rec["clear_heights"])
    assert {m["level_id"] for m in rec["levels"]} == {housemodel.LEVEL_GROUND, housemodel.LEVEL_FLOOR,
                                                      housemodel.LEVEL_CEILING, housemodel.LEVEL_ROOF}
    # the window the cut passes through splits the wall: more than one hatched piece for that wall
    window_walls = [o.wall_id for o in model.windows if o.a < cut.position < o.b]
    assert window_walls
    pieces = [p for p in view.prims if p.semantic == "cut_wall" and p.ref == window_walls[0]]
    assert len(pieces) == 2
    assert view.expected_entities() == len(view.prims) + sum(1 for p in view.prims if p.kind == "cut")
    assert rec["marker"]["target_view"] == cut.view_id and rec["marker"]["target_sheet"] == "A-301"


def test_section_cut_checks() -> None:
    model = model_for()
    with pytest.raises(ValueError, match="inside the wall"):
        houseviews.check_cut(model, houseviews.Cut("y", 6.7))
    with pytest.raises(ValueError, match="inside the house"):
        houseviews.check_cut(model, houseviews.Cut("y", 99.0))
    with pytest.raises(ValueError, match="axis"):
        houseviews.check_cut(model, houseviews.Cut("z", 4.0))
    other = houseviews.section_view(model, houseviews.Cut("x", 4.5, -1, "B"))
    assert other.record["cut"]["axis"] == "x" and other.record["id"] == "SEC-B-B"
    assert other.record["marker"]["axis"] == "x" and other.record["marker"]["direction"] == -1


def test_section_with_a_stair() -> None:
    model = model_for(storey_height=3.0)
    # a stair (single storey houses have none: the model accepts one) from the floor to the ceiling level
    housemodel.add_stair(model, housemodel.Stair("ST1", housemodel.LEVEL_FLOOR, housemodel.LEVEL_CEILING, 3.0, 17, 0.25,
                                                 1.0, (1.5, 2.5, 5.5, 3.5), "x"))
    view = houseviews.section_view(model, houseviews.Cut("y", 3.0))
    st = view.record["stairs"]
    assert len(st) == 1 and st[0]["riser_count"] == 17 and st[0]["total_height"] == pytest.approx(3.0)
    assert [p.semantic for p in view.prims].count("stair_cut") == 1
    ent = next(e for e in model.entities() if e.entity_id == "ST1")
    assert ent.properties["riser_height"] == pytest.approx(3.0 / 17, abs=1e-5)
    with pytest.raises(housemodel.HouseModelError):
        housemodel.add_stair(model, housemodel.Stair("ST2", "LV-NONE", housemodel.LEVEL_CEILING, 3.0, 17, 0.25, 1.0,
                                                     (0, 0, 1, 1), "x"))


# --------------------------------------------------------------------------- schedules

def test_drawn_tables_read_back_equal_the_model_rows() -> None:
    model = model_for()
    views = {v.record["schedule"]: v for v in houseviews.schedule_views(model)}
    door = views["door"].record["rows"]
    assert [r["code"] for r in door] == [o.code for o in model.doors]
    for row, o in zip(door, model.doors):
        assert row["width"] == pytest.approx(o.width) and row["height"] == pytest.approx(o.height)
    room = views["room"].record["rows"]
    assert room[-1]["total"] is True
    assert room[-1]["area"] == pytest.approx(sum(r.area for r in model.listed_rooms), abs=0.006)
    assert [r["code"] for r in room[:-1]] == [r.code for r in model.listed_rooms]
    assert houseviews.parse_number("1,35") == 1.35 and houseviews.fmt2(1.35) == "1,35"
    for kind in ("door", "window", "room"):
        assert any(p.semantic == "cell" for p in views[kind].prims)


# --------------------------------------------------------------------------- validation rules

def test_consistent_drawing_passes_all_new_rules() -> None:
    model = model_for()
    report = validate(model.validation_model(all_records(model)))
    for rid in NEW_RULES:
        r = result_of(report, rid)
        assert r.result.value == "PASS", (rid, r.evidence, r.issues)
        assert r.checked > 0
    assert report.overall in ("PASS", "WARN"), report.open_errors
    assert result_of(report, "COORD.SCHEDULE_MATCHES_SYMBOLS").result.value == "PASS"
    assert result_of(report, "COORD.AREA_RECOMPUTED").result.value == "PASS"
    assert result_of(report, "GEOM.OPENING_HOST_WALL").result.value == "PASS"


def test_rules_are_not_run_without_view_data() -> None:
    model = model_for()
    report = validate(model.validation_model())
    for rid in NEW_RULES[:4]:
        assert result_of(report, rid).result.value == "NOT_RUN", rid
    # the room table rule can still run on the schedule data of the model
    assert result_of(report, "COORD.ROOM_TABLE_AREA").result.value == "PASS"
    no_units = model.validation_model(all_records(model))
    no_units["meta"]["units"] = None
    for rid in NEW_RULES:
        assert result_of(validate(no_units), rid).result.value == "NOT_RUN", rid
    only_plan = model.validation_model([houseviews.schedule_views(model)[0].record])
    assert result_of(validate(only_plan), "COORD.ELEVATION_MATCHES_PLAN").result.value == "NOT_RUN"


def mutated(records: list[dict[str, Any]], fn: Any) -> dict[str, Any]:
    model = model_for()
    recs = copy.deepcopy(records)
    fn(recs)
    return model.validation_model(recs)


def test_elevation_width_and_missing_opening_are_found() -> None:
    model = model_for()
    base = all_records(model)

    def widen(recs: list[dict[str, Any]]) -> None:
        front = next(r for r in recs if r.get("side") == "front")
        front["openings"][0]["width"] += 0.05

    report = validate(mutated(base, widen))
    r = result_of(report, "COORD.ELEVATION_MATCHES_PLAN")
    assert r.result.value == "FAIL" and "width" in r.issues[0]["message"] and r.issues[0]["kind"] == "CONFLICT"
    # the elevation also disagrees with the schedule now
    assert result_of(report, "COORD.SCHEDULE_MATCHES_ELEVATION").result.value == "FAIL"

    def drop(recs: list[dict[str, Any]]) -> None:
        right = next(r for r in recs if r.get("side") == "right")
        right["openings"].pop()

    r = result_of(validate(mutated(base, drop)), "COORD.ELEVATION_MATCHES_PLAN")
    assert r.result.value == "FAIL" and "missing from" in r.issues[0]["message"]

    def wrong_code(recs: list[dict[str, Any]]) -> None:
        next(r for r in recs if r.get("side") == "rear")["openings"][0]["code"] = "D99"

    assert result_of(validate(mutated(base, wrong_code)), "COORD.ELEVATION_MATCHES_PLAN").result.value == "FAIL"


def test_schedule_width_and_code_mismatch_are_found() -> None:
    base = all_records(model_for())

    def edit(recs: list[dict[str, Any]]) -> None:
        door = next(r for r in recs if r.get("schedule") == "door")
        door["rows"][0]["width"] = 1.0
        door["rows"][1]["code"] = "D77"

    report = validate(mutated(base, edit))
    assert result_of(report, "COORD.SCHEDULE_MATCHES_ELEVATION").result.value == "FAIL"
    assert result_of(report, "COORD.SCHEDULE_MATCHES_SYMBOLS").result.value == "FAIL"  # plan vs schedule (existing rule)


def test_level_mismatches_are_found() -> None:
    base = all_records(model_for())

    def move(recs: list[dict[str, Any]]) -> None:
        sec = next(r for r in recs if r["kind"] == "section")
        next(m for m in sec["levels"] if m["level_id"] == housemodel.LEVEL_CEILING)["elevation"] = 3.2

    r = result_of(validate(mutated(base, move)), "COORD.LEVELS_ELEVATION_SECTION")
    assert r.result.value == "FAIL" and "3.2" in r.issues[0]["message"]

    def mislabel(recs: list[dict[str, Any]]) -> None:
        front = next(r for r in recs if r.get("side") == "front")
        next(m for m in front["levels"] if m["level_id"] == housemodel.LEVEL_ROOF)["label_value"] = 3.5

    assert result_of(validate(mutated(base, mislabel)), "COORD.LEVELS_ELEVATION_SECTION").result.value == "FAIL"

    def between(recs: list[dict[str, Any]]) -> None:
        for rec in recs:
            if rec.get("side") == "rear":
                next(m for m in rec["levels"] if m["level_id"] == housemodel.LEVEL_FLOOR)["elevation"] = 0.02

    r = result_of(validate(mutated(base, between)), "COORD.LEVELS_ELEVATION_SECTION")
    assert r.result.value == "FAIL" and any("0.02" in i["message"] for i in r.issues)

    def clear(recs: list[dict[str, Any]]) -> None:
        next(r for r in recs if r["kind"] == "section")["clear_heights"][0]["height"] = 2.9

    assert result_of(validate(mutated(base, clear)), "COORD.LEVELS_ELEVATION_SECTION").result.value == "FAIL"


def test_section_marker_must_reference_an_existing_view_cut_at_the_marker() -> None:
    base = all_records(model_for())

    def ghost(recs: list[dict[str, Any]]) -> None:
        next(r for r in recs if r["kind"] == "section")["marker"]["target_view"] = "SEC-Z-Z"

    r = result_of(validate(mutated(base, ghost)), "COORD.SECTION_MARKER_VIEW")
    assert r.result.value == "FAIL" and "SEC-Z-Z" in r.issues[0]["message"]

    def shifted(recs: list[dict[str, Any]]) -> None:
        next(r for r in recs if r["kind"] == "section")["marker"]["position"] += 0.5

    assert result_of(validate(mutated(base, shifted)), "COORD.SECTION_MARKER_VIEW").result.value == "FAIL"

    def no_marker(recs: list[dict[str, Any]]) -> None:
        next(r for r in recs if r["kind"] == "section").pop("marker")

    r = result_of(validate(mutated(base, no_marker)), "COORD.SECTION_MARKER_VIEW")
    assert r.result.value == "WARN" and "no marker" in r.issues[0]["message"]


def test_area_table_is_recomputed_from_the_boundary() -> None:
    base = all_records(model_for())

    def wrong_area(recs: list[dict[str, Any]]) -> None:
        room = next(r for r in recs if r.get("schedule") == "room")
        room["rows"][0]["area"] += 1.0

    r = result_of(validate(mutated(base, wrong_area)), "COORD.ROOM_TABLE_AREA")
    assert r.result.value == "FAIL" and r.issues[0]["detail"]["recomputed"] == pytest.approx(39.2)

    def wrong_total(recs: list[dict[str, Any]]) -> None:
        room = next(r for r in recs if r.get("schedule") == "room")
        room["rows"][-1]["area"] += 2.0

    r = result_of(validate(mutated(base, wrong_total)), "COORD.ROOM_TABLE_AREA")
    assert r.result.value == "FAIL" and "total" in r.issues[0]["message"]

    def missing(recs: list[dict[str, Any]]) -> None:
        room = next(r for r in recs if r.get("schedule") == "room")
        room["rows"] = [x for x in room["rows"] if x["code"] != "R03"]

    r = result_of(validate(mutated(base, missing)), "COORD.ROOM_TABLE_AREA")
    assert r.result.value == "FAIL" and any("missing from the area table" in i["message"] for i in r.issues)

    # a boundary changed in the model but the table not: the recomputed area differs
    model = model_for()
    md = model.validation_model(base)
    room = next(e for e in md["entities"] if e["entity_id"] == "R01")
    room["geometry"]["points"][2][0] += 1.0
    assert result_of(validate(md), "COORD.ROOM_TABLE_AREA").result.value == "FAIL"


def test_rule_catalog_lists_the_new_rules() -> None:
    ids = [r["rule_id"] for r in validation.rule_catalog()]
    assert len(ids) == len(set(ids))
    assert set(NEW_RULES) <= set(ids)
    for r in validation.rule_catalog():
        if r["rule_id"] in NEW_RULES:
            assert r["scope"] == "COORDINATION" and r["severity"] == "ERROR"


# --------------------------------------------------------------------------- tool handlers with a fake provider

class FakeExecutor:
    async def run(self, timeout: float, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)


class FakeProvider:
    """Records what the handlers ask it to draw; draw_prims answers the way a faithful AutoCAD would."""

    def __init__(self, drop: int = 0) -> None:
        self.executor = FakeExecutor()
        self.calls: list[dict[str, Any]] = []
        self.drop = drop
        self.total = 0

    def draw_prims(self, prims: Any, layers_: Any, origin: Any, text_height: float, new_document: bool, *,
                   deadline: float) -> PrimsResult:
        counts: dict[str, int] = {}
        by_layer: dict[str, int] = {}
        for p in prims:
            kinds = ["poly", "hatch"] if p.kind == "cut" else [p.kind]
            for k in kinds:
                counts[k] = counts.get(k, 0) + 1
            name = layers_[p.layer][0]
            by_layer[name] = by_layer.get(name, 0) + 1
            if p.kind == "cut":
                by_layer[layers_["A-HATCH"][0]] = by_layer.get(layers_["A-HATCH"][0], 0) + 1
        n = sum(counts.values())
        self.calls.append({"n": n, "origin": origin, "layers": layers_, "new_document": new_document,
                           "text_height": text_height})
        before = self.total
        self.total += n - self.drop
        return PrimsResult("Drawing9.dwg", counts, by_layer, before, self.total, list(by_layer), [])


@dataclass
class TmpContext(ToolContext):
    tmp: Path | None = None

    def artifact_dir(self, kind: str) -> Path:
        assert self.tmp is not None
        directory = self.tmp / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class Harness:
    def __init__(self, config: Config, tmp: Path, provider: FakeProvider | None = None) -> None:
        self.config = config
        self.tmp = tmp
        self.registry = build_registry(config.path("paths.tools_dir"))
        self.services = Services(config)
        self.provider = provider or FakeProvider()
        self.services._extra["autocad"] = self.provider  # noqa: SLF001

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        spec = self.registry.specs[tool]
        ctx = TmpContext(request_id=uuid.uuid4().hex, task_id="views-unit", step_id=tool,
                         deadline=time.monotonic() + spec.timeout_ms / 1000.0, config=self.config,
                         services=self.services, cancel_event=threading.Event(), tmp=self.tmp)
        _, outcome = await self.registry.invoke(ctx, tool, args)
        return outcome


def all_checks_pass(outcome: Any) -> bool:
    return all(c.passed for c in outcome.checks)


async def test_tools_are_in_the_contract(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    for name in ("autocad.house_model", "autocad.draw_elevation", "autocad.draw_section", "autocad.draw_schedules"):
        assert name in h.registry.specs
    assert not [t for t in h.registry.unbound_tools() if t.startswith("autocad.")]


async def test_draw_elevation_handler(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    out = await h.call("autocad.draw_elevation", {**PARAMS, "sides": ["front", "right"], "new_document": True})
    assert all_checks_pass(out)
    assert [v["id"] for v in out.data["views"]] == ["ELV-FRONT", "ELV-RIGHT"]
    assert [c["new_document"] for c in h.provider.calls] == [True, False]
    assert h.provider.calls[0]["origin"] == houseviews.default_origin(model_for(), "front")
    assert out.data["operation_id"].startswith("OP-")
    lines = (tmp_path / "autocad" / "operations.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["kind"] == "CREATE" and record["status"] == "APPLIED"
    # every layer sent to the provider is a standard A-* layer, and the layer map renames
    out = await h.call("autocad.draw_elevation", {**PARAMS, "layer_map": {"A-DOOR": "MY-DOOR"}})
    assert h.provider.calls[-1]["layers"]["A-DOOR"][0] == "MY-DOOR"
    assert all(k.startswith("A-") for k in h.provider.calls[-1]["layers"])
    assert out.data["levels"][1]["elevation"] == 0.0


async def test_draw_elevation_fails_the_check_when_autocad_adds_fewer_entities(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path, FakeProvider(drop=2))
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.draw_elevation", dict(PARAMS))
    assert exc.value.code == ErrorCode.VERIFICATION_FAILED and "entities_drawn" in exc.value.message


async def test_draw_section_handler(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    out = await h.call("autocad.draw_section", {**PARAMS, "cut": {"axis": "y", "letter": "A"}})
    assert all_checks_pass(out)
    assert out.data["view"]["section_code"] == "A-A" and out.data["marker"]["entities"] > 0
    assert len(h.provider.calls) == 2  # the section, then the marker on the plan
    assert h.provider.calls[1]["origin"] == (0.0, 0.0)
    out = await h.call("autocad.draw_section", {**PARAMS, "draw_marker": False})
    assert out.data["marker"] is None
    for bad in ({"axis": "y", "position": 6.7}, {"axis": "y", "position": 50}, {"axis": "q"}):
        with pytest.raises(ToolError) as exc:
            await h.call("autocad.draw_section", {**PARAMS, "cut": bad})
        assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_draw_schedules_handler_and_export_with_backup(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    target = tmp_path / "out" / "schedules.json"
    out = await h.call("autocad.draw_schedules", {**PARAMS, "export_path": str(target)})
    assert all_checks_pass(out)
    assert [v["schedule"] for v in out.data["views"]] == ["door", "window", "room"]
    assert out.data["schedules"]["door"][0]["code"] == "D1"
    assert out.data["room_total_area_m2"] == pytest.approx(sum(r["area"] for r in out.data["schedules"]["room"]))
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["schedules"]["window"][0]["code"] == "W1" and "backup" not in out.data
    with pytest.raises(ToolError) as exc:  # refuses to replace silently
        await h.call("autocad.draw_schedules", {**PARAMS, "export_path": str(target)})
    assert exc.value.code == ErrorCode.CONFLICT
    again = await h.call("autocad.draw_schedules", {**PARAMS, "export_path": str(target), "overwrite": True})
    backup = again.data["backup"]
    assert Path(backup["backup"]).is_file() and len(backup["sha256"]) == 64
    assert any(c.name == "backup_before_overwrite" and c.passed for c in again.checks)


async def test_house_model_handler_feeds_validation(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    elev = await h.call("autocad.draw_elevation", {**PARAMS, "sides": list(housemodel.SIDES)})
    sec = await h.call("autocad.draw_section", dict(PARAMS))
    sch = await h.call("autocad.draw_schedules", dict(PARAMS))
    views = elev.data["views"] + [sec.data["view"]] + sch.data["views"]
    out = await h.call("autocad.house_model", {**PARAMS, "views": views, "export_path": str(tmp_path / "model.json")})
    assert all_checks_pass(out) and (tmp_path / "model.json").is_file()
    verdict = await h.call("autocad.validate_model", {"model": out.data["model"]})
    by_id = {r["rule_id"]: r for r in verdict.data["results"]}
    for rid in NEW_RULES:
        assert by_id[rid]["result"] == "PASS", (rid, by_id[rid]["evidence"], by_id[rid]["issues"])
    assert verdict.data["overall"] in ("PASS", "WARN")
    report = await h.call("autocad.report", {"project": "unit", "model": out.data["model"],
                                              "evidence": {"dwg_opened_in_autocad": False}})
    assert report.data["final_status"] not in ("CODE_REVIEWED", "COORDINATED")  # nothing was opened or reviewed
    # without the drawn views the cross-view rules are NOT_RUN, never a pass
    bare = await h.call("autocad.house_model", dict(PARAMS))
    verdict = await h.call("autocad.validate_model", {"model": bare.data["model"]})
    assert {r["rule_id"]: r["result"] for r in verdict.data["results"]}["COORD.SECTION_MARKER_VIEW"] == "NOT_RUN"


async def test_handlers_reject_bad_arguments(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    for tool, args in (("autocad.draw_elevation", {"width": 2, "depth": 3}),
                       ("autocad.house_model", {**PARAMS, "storey_height": 2.0}),
                       ("autocad.draw_schedules", {**PARAMS, "layer_map": {"KT-TUONG": "X"}})):
        with pytest.raises(ToolError) as exc:
            await h.call(tool, args)
        assert exc.value.code == ErrorCode.INVALID_ARGUMENT, tool
    assert h.provider.calls == []
