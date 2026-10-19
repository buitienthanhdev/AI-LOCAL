"""Unit tests (no AutoCAD) of multi-storey houses, stairs, slab openings, layouts/title blocks/viewports, the new validation
rules, idempotency markers, the re-open evidence, PDF checks and the new tool handlers (with a fake provider).

The live check against the real AutoCAD is logs/dev/autocad_multistorey_live.py.
"""

from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from lga_common.config import Config

from lga_worker.autocad import floorplan, housemodel, houseviews, layouts, pdfcheck, report, validation
from lga_worker.autocad.provider import PlanResult, PrimsResult
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services
from lga_worker.tools import autocad as autocad_tools

PARAMS = {"width": 8, "depth": 18, "bedrooms": 3}


def model_for(floors: int = 3, **kw: Any) -> housemodel.HouseModel:
    return housemodel.build_house_model({**PARAMS, "floors": floors, **kw})


def validate(model_dict: dict[str, Any], **kw: Any) -> validation.ValidationReport:
    return validation.run_validation(validation.DrawingModel.from_dict(model_dict), **kw)


def result_of(report_: validation.ValidationReport, rule_id: str) -> validation.RuleResult:
    return next(r for r in report_.results if r.rule_id == rule_id)


def drawn_records(model: housemodel.HouseModel) -> list[dict[str, Any]]:
    """The records the draw tools would return for a full drawing of the model."""
    out: list[dict[str, Any]] = []
    for n in range(1, model.floors + 1):
        view = houseviews.stair_plan_view(model, n)
        assert view is not None
        out.append({**view.record, "drawn": True})
    out.append(houseviews.roof_plan_view(model).record)
    out += [houseviews.elevation_view(model, s).record for s in housemodel.SIDES]
    cut = houseviews.check_cut(model, houseviews.default_cut(model))
    out.append(houseviews.section_view(model, cut).record)
    out += [v.record for v in houseviews.schedule_views(model)]
    return copy.deepcopy(out)


# --------------------------------------------------------------------------- levels and floors

def test_single_storey_is_unchanged() -> None:
    one = model_for(1)
    assert one.floors == 1 and one.stairs == []
    assert [lv.id for lv in one.levels] == ["LV-GROUND", "LV-F1", "LV-CEIL", "LV-ROOF", "LV-RIDGE"]
    default = housemodel.build_house_model(PARAMS)
    assert [o.code for o in default.openings] == [o.code for o in one.openings]
    assert [r.code for r in default.rooms] == [r.code for r in one.rooms]
    assert housemodel.floor_plan(one, 1) is one.plan


@pytest.mark.parametrize("floors", [2, 3, 4])
def test_levels_follow_floor_heights_and_slabs(floors: int) -> None:
    model = model_for(floors, floor_heights=[3.3, 3.0, 3.6, 3.2][:floors], slab_thickness=0.12)
    elevations = {lv.id: lv.elevation for lv in model.levels}
    z = 0.0
    for n in range(1, floors + 1):
        h = [3.3, 3.0, 3.6, 3.2][n - 1]
        assert elevations[housemodel.floor_level_id(n)] == pytest.approx(z)
        assert elevations[housemodel.ceiling_level_id(n)] == pytest.approx(z + h)
        z += h + 0.12
    assert elevations["LV-ROOF"] == pytest.approx(z)
    assert model.storey_clear(2) == 3.0
    assert len(model.stairs) == floors - 1


def test_floor_arguments_are_checked() -> None:
    for bad in ({"floors": 0}, {"floors": 5}, {"floors": 2, "floor_heights": [3.3]},
                {"floors": 2, "floor_heights": [3.3, 9.0]}, {"floors": 2, "storey_height": 3.0, "floor_heights": [3.3, 3.3]}):
        with pytest.raises(housemodel.HouseModelError):
            housemodel.build_house_model({**PARAMS, **bad})


def test_upper_floors_have_codes_levels_and_no_entrance_door() -> None:
    model = model_for(2)
    codes = [o.code for o in model.openings]
    assert len(codes) == len(set(codes))
    assert {"D1", "D1-T2"} <= set(codes) and all(c.endswith("-T2") for c in codes if o_level(model, c) == "LV-F2")
    front_door = next(o for o in model.openings if o.facade == "front" and o.level_id == "LV-F1" and o.kind == "double")
    upper = [o for o in model.floor_openings(2) if o.wall_id.startswith(front_door.wall_id)]
    assert upper and all(o.kind == "window" for o in upper)  # exterior doors become windows upstairs
    assert {r.level_id for r in model.rooms} == {"LV-F1", "LV-F2"}
    assert all(w.id.endswith("-F2") for w in model.walls if w.level_id == "LV-F2")
    assert any(a["id"] == "ASM-upper-floors" for a in model.assumptions)


def o_level(model: housemodel.HouseModel, code: str) -> str:
    return next(o.level_id for o in model.openings if o.code == code)


# --------------------------------------------------------------------------- stairs

@pytest.mark.parametrize("height", [2.6, 2.8, 3.0, 3.3, 3.6, 4.2, 5.0])
def test_stair_riser_count_and_height_are_recomputed_from_the_height(height: float) -> None:
    model = model_for(2, floor_heights=[height, 3.0])
    st = model.stairs[0]
    total = model.elevation("LV-F2") - model.elevation("LV-F1")
    assert st.total_height == pytest.approx(total)
    assert st.riser_count == -(-round(total / 0.17, 9) // 1)  # ceil(total / target)
    riser = st.total_height / st.riser_count
    assert riser <= 0.17 + 1e-9 and riser * st.riser_count == pytest.approx(total)
    assert sum(f["risers"] for f in st.flights) == st.riser_count
    assert all(f["treads"] == f["risers"] - 1 for f in st.flights)
    assert st.status["tread"] == "ASSUMED" and st.status["riser_target"] == "ASSUMED"


def test_stair_stays_inside_its_room_and_clear_of_doors() -> None:
    model = model_for(3)
    living = next(r for r in model.rooms if r.kind == "living" and r.level_id == "LV-F1")
    x0, y0, x1, y1 = model.stairs[0].footprint
    lx0, ly0, lx1, ly1 = living.bbox
    assert lx0 < x0 and ly0 < y0 and x1 < lx1 and y1 < ly1
    for o in model.floor_openings(1):
        if o.kind == "window":
            continue
        zone = ((o.a - 0.1, o.line - 0.9, o.b + 0.1, o.line + 0.9) if o.orient == "h"
                else (o.line - 0.9, o.a - 0.1, o.line + 0.9, o.b + 0.1))
        assert not (x0 < zone[2] and zone[0] < x1 and y0 < zone[3] and zone[1] < y1), o.code
    assert all(s.footprint == model.stairs[0].footprint for s in model.stairs)  # stacked in one footprint


def test_slab_opening_is_inside_the_footprint_and_leaves_the_arrival_free() -> None:
    model = model_for(3)
    st = model.stairs[0]
    fx0, fy0, fx1, fy1 = st.footprint
    assert st.opening and all(fx0 - 1e-9 <= r[0] and r[2] <= fx1 + 1e-9 and fy0 - 1e-9 <= r[1] and r[3] <= fy1 + 1e-9
                              for r in st.opening)
    area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in st.opening)
    assert 0.4 < area / ((fx1 - fx0) * (fy1 - fy0)) < 1.0  # not the whole footprint: the first metres keep a slab (headroom)
    slabs = {e.entity_id: e for e in model.entities() if e.entity_type.value == "SLAB"}
    assert slabs["SLAB-F2"].properties["stair_opening"] and slabs["SLAB-F3"].properties["stair_opening"]


def test_straight_stair_is_for_two_floors_only() -> None:
    model = model_for(2, stair={"flights": 1, "width": 0.9, "riser_target": 0.19})
    assert model.stairs[0].shape == "straight" and len(model.stairs[0].flights) == 1
    assert model.stairs[0].flights[0]["risers"] == model.stairs[0].riser_count
    with pytest.raises(housemodel.HouseModelError, match="3 or more floors"):
        model_for(3, stair={"flights": 1})


def test_stair_that_does_not_fit_is_refused_with_a_reason() -> None:
    with pytest.raises(housemodel.HouseModelError, match="needs"):
        model_for(2, stair={"width": 2.9, "tread": 0.3})
    with pytest.raises(housemodel.HouseModelError, match="unknown stair"):
        model_for(2, stair={"colour": "red"})


def test_profile_limits_apply_only_when_verified() -> None:
    unverified = model_for(2, profile={"max_riser_height": {"value": 0.15, "source": "somebody said so"}})
    st = unverified.stairs[0]
    assert st.total_height / st.riser_count > 0.15 and st.limits["applied"] == {}
    assert "max_riser_height" in st.limits["unverified"]
    assert any(a["id"] == "ASM-profile-max_riser_height" for a in unverified.assumptions)
    assert any(a["id"] == "ASM-stair-limits" for a in unverified.assumptions)
    verified = model_for(2, profile={"max_riser_height": {"value": 0.15, "verified_by": "chu tri thiet ke (test)"},
                                     "min_tread_depth": 0.22})
    v = verified.stairs[0]
    assert v.total_height / v.riser_count <= 0.15 + 1e-9 and v.riser_count > st.riser_count
    assert v.limits["applied"]["max_riser_height"]["verified_by"]
    assert "min_tread_depth" not in v.limits["applied"]  # a bare number carries no verification
    with pytest.raises(housemodel.HouseModelError, match="verified profile limit"):
        model_for(2, stair={"tread": 0.2}, profile={"min_tread_depth": {"value": 0.25, "verified_by": "x"}})
    with pytest.raises(housemodel.HouseModelError, match="verified profile limit"):
        model_for(2, stair={"riser_target": 0.2}, profile={"max_riser_height": {"value": 0.18, "verified_by": "x"}})


def test_stair_entity_carries_the_recomputed_numbers_and_status() -> None:
    model = model_for(2)
    ent = next(e for e in model.entities() if e.entity_id == "ST1")
    assert ent.properties["riser_count"] == model.stairs[0].riser_count
    assert ent.properties["riser_height"] == pytest.approx(model.stairs[0].total_height / model.stairs[0].riser_count, abs=1e-5)
    assert ent.properties["setting_status"]["tread"] == "ASSUMED"
    assert ent.properties["slab_opening"] and ent.properties["flights"]


# --------------------------------------------------------------------------- views of several floors

def test_elevation_marks_every_floor_and_puts_openings_at_their_floor() -> None:
    model = model_for(3)
    view = houseviews.elevation_view(model, "front")
    marked = {m["level_id"] for m in view.record["levels"]}
    assert {"LV-F1", "LV-F2", "LV-F3", "LV-ROOF", "LV-GROUND"} <= marked
    upper = [o for o in view.record["openings"] if o["level_id"] == "LV-F2"]
    plan = [o for o in model.floor_openings(2) if o.facade == "front"]
    assert upper and {o["code"] for o in upper} == {o.code for o in plan}
    for o in upper:  # the record is measured against the floor, so it equals the plan's sill
        assert o["sill_height"] == pytest.approx(next(p.sill for p in plan if p.code == o["code"]), abs=1e-6)
    drawn_z = {p.ref: p.pts[1] for p in view.prims if p.semantic == "opening"}
    assert drawn_z[upper[0]["code"]] == pytest.approx(model.elevation("LV-F2") + upper[0]["sill_height"])


def test_section_shows_all_floors_slabs_with_an_opening_and_the_stair() -> None:
    model = model_for(3)
    cut = houseviews.check_cut(model, houseviews.default_cut(model))
    assert cut.axis == "x" and model.stairs[0].footprint[0] < cut.position < model.stairs[0].footprint[2]
    view = houseviews.section_view(model, cut)
    rec = view.record
    assert rec["slabs"] == ["SLAB-FLOOR", "SLAB-F2", "SLAB-F3", "SLAB-ROOF"] and rec["floors"] == 3
    for ref in ("SLAB-F2", "SLAB-F3"):  # the slab is cut in two pieces around the stair opening
        assert len([p for p in rec["slab_pieces"] if p["ref"] == ref]) == 2
    assert {c["from_level"] for c in rec["clear_heights"]} == {"LV-F1", "LV-F2", "LV-F3"}
    assert all(c["height"] == pytest.approx(3.3) for c in rec["clear_heights"])
    levels = {m["level_id"] for m in rec["levels"]}
    assert {"LV-F1", "LV-F2", "LV-F3", "LV-CEIL3", "LV-ROOF"} <= levels
    assert [s["id"] for s in rec["stairs"]] == ["ST1", "ST2"]
    for s, st in zip(rec["stairs"], model.stairs):
        assert s["riser_count"] == st.riser_count and s["total_height"] == pytest.approx(st.total_height)
        assert sum(s["risers_per_flight"]) == st.riser_count
    cut_walls = rec["cut_walls"]
    assert any(w.endswith("-F2") for w in cut_walls) and any(w.endswith("-F3") for w in cut_walls)


def test_section_through_the_gap_or_the_other_flight_still_counts_every_riser() -> None:
    model = model_for(2)
    x0, _, x1, _ = model.stairs[0].footprint
    for pos in (x0 + 0.3, (x0 + x1) / 2, x1 - 0.3):
        view = houseviews.section_view(model, houseviews.Cut("x", pos))
        assert view.record["stairs"][0]["riser_count"] == model.stairs[0].riser_count, pos


def test_stair_plan_symbol_counts_back_to_the_model() -> None:
    model = model_for(3)
    for n in (1, 2, 3):
        view = houseviews.stair_plan_view(model, n)
        assert view is not None
        rec = view.record
        assert rec["floor"] == n and rec["level_id"] == f"LV-F{n}"
        texts = [t for s in rec["stairs"] for t in s.get("texts", [])]
        assert ("LÊN" in texts) == (n < 3) and ("XUỐNG" in texts) == (n > 1)
        for s in rec["stairs"]:
            if "riser_lines" in s:
                assert s["riser_lines"] == model.stairs[0].riser_count
        if n > 1:
            assert rec["slab_openings"][0]["stair_id"] == f"ST{n - 1}"
            assert rec["slab_openings"][0]["rects"] == [list(r) for r in model.stairs[n - 2].opening] or \
                sorted(rec["slab_openings"][0]["rects"]) == sorted([round(v, 6) for v in r] for r in model.stairs[n - 2].opening)
        else:
            assert rec["slab_openings"] == []
    assert houseviews.stair_plan_view(model_for(1), 1) is None


def test_roof_plan_and_floor_plan_origins() -> None:
    model = model_for(3)
    roof = houseviews.roof_plan_view(model)
    assert roof.record["kind"] == "roof_plan" and roof.record["code"] == "A-104"
    assert roof.record["ridge_x"] == pytest.approx(model.width / 2) and roof.record["slope_deg"] == 30.0
    flat = houseviews.roof_plan_view(model_for(2, roof_type="flat"))
    assert flat.record["slope_deg"] is None and flat.record["roof_type"] == "flat"
    origins = [houseviews.floor_plan_origin(model, n) for n in (1, 2, 3, 4)]
    assert origins[0] == (0.0, 0.0) and all(a[0] > b[0] for a, b in zip(origins, origins[1:]))
    elev_y = houseviews.default_origin(model, "front")[1]
    ridge_top = elev_y + model.elevation("LV-RIDGE")
    assert ridge_top < -5.0  # the tall elevations no longer reach up into the plan
    assert houseviews.default_origin(model_for(1), "front") == (4.0, -16.0)  # single storey: as before


def test_floor_plan_geometry_per_floor() -> None:
    model = model_for(2)
    p1, p2 = housemodel.floor_plan(model, 1), housemodel.floor_plan(model, 2)
    assert p2.porch_shapes == [] and p1.porch_shapes
    assert "TẦNG 2" in p2.title.text and all("(T2)" in lb.text for lb in p2.labels)
    assert not [o for o in p2.openings if o.kind in ("door", "double") and o.line in (model.porch, model.depth)]
    st = model.stairs[0].footprint
    for plan in (p1, p2):
        for sh in plan.furniture:
            x0, y0, x1, y1 = housemodel._shape_box(sh)  # noqa: SLF001
            assert not (x0 < st[2] + 0.1 and x1 > st[0] - 0.1 and y0 < st[3] + 0.1 and y1 > st[1] - 0.1)
    assert floorplan.expected_entity_count(p1) == 2 * len(p1.walls) + sum(
        len(floorplan.opening_shapes(o, p1.wall_thickness.get((o.orient, round(o.line, 4)), floorplan.INTERIOR_WALL)))
        for o in p1.openings) + len(p1.furniture) + len(p1.porch_shapes) + len(p1.labels) + 2 + len(p1.dimensions)


def test_schedules_are_per_floor_and_totals_per_floor() -> None:
    model = model_for(3)
    views = houseviews.schedule_views(model)
    assert [v.id for v in views] == ["SCH-DOOR-F1", "SCH-DOOR-F2", "SCH-DOOR-F3", "SCH-WINDOW-F1", "SCH-WINDOW-F2",
                                     "SCH-WINDOW-F3", "SCH-ROOM-F1", "SCH-ROOM-F2", "SCH-ROOM-F3"]
    for v in views:
        n = int(v.id[-1])
        assert all(r["level_id"] == f"LV-F{n}" for r in v.record["rows"])
    room2 = views[7].record["rows"]
    assert room2[-1]["total"] and room2[-1]["area"] == pytest.approx(sum(r.area for r in model.floor_rooms(2) if r.listed), abs=0.006)
    first_x = [min(p.pts[0] for p in v.prims if p.kind == "line") for v in views[:3]]
    assert first_x[0] < first_x[1] < first_x[2]  # the floors side by side
    assert [v.id for v in houseviews.schedule_views(model_for(1))] == ["SCH-DOOR", "SCH-WINDOW", "SCH-ROOM"]


# --------------------------------------------------------------------------- validation of the new data

NEW_COORD = ("COORD.STAIR_RISERS", "COORD.STAIR_OPENING_IN_SLAB", "COORD.STAIR_PLAN_MATCHES_MODEL",
             "COORD.FLOOR_LEVELS_ALL_FLOORS", "COORD.ELEVATION_MATCHES_PLAN", "COORD.SCHEDULE_MATCHES_ELEVATION",
             "COORD.LEVELS_ELEVATION_SECTION", "COORD.SECTION_MARKER_VIEW", "COORD.ROOM_TABLE_AREA")


def test_consistent_three_storey_model_passes_the_coordination_rules() -> None:
    model = model_for(3)
    rep = validate(model.validation_model(drawn_records(model)))
    for rid in NEW_COORD:
        r = result_of(rep, rid)
        assert r.result.value == "PASS", (rid, r.evidence, [i["message"] for i in r.issues if i["severity"] != "INFO"])
        assert r.checked > 0
    assert not rep.open_errors and not rep.open_blockers
    assert result_of(rep, "GEOM.DUPLICATE_ENTITIES").result.value == "PASS"  # one storey's wall is not another's duplicate
    info = [i for i in result_of(rep, "COORD.STAIR_RISERS").issues if i["severity"] == "INFO"]
    assert info and "ASSUMED" in info[0]["message"] and "no verified limit" in info[0]["message"]


def test_rules_are_not_run_without_data() -> None:
    bare = validate(model_for(3).validation_model())
    states = {r.rule_id: r.result.value for r in bare.results}
    assert states["COORD.STAIR_PLAN_MATCHES_MODEL"] == "NOT_RUN"
    assert states["PLOT.SHEET_LAYOUT_EXISTS"] == "NOT_RUN" and states["PLOT.VIEWPORT_SCALE"] == "NOT_RUN"
    assert states["PLOT.VIEWPORT_LOCKED"] == "NOT_RUN" and states["PLOT.TITLE_BLOCK_FIELDS"] == "NOT_RUN"
    assert states["PLOT.PAPER_ORIENTATION"] == "NOT_RUN" and states["PLOT.PDF_PAGES_MATCH_SHEETS"] == "NOT_RUN"
    assert states["FILE.REOPEN_COUNTS"] == "NOT_RUN"
    no_units = model_for(3).validation_model()
    no_units["meta"].pop("units")
    assert result_of(validate(no_units), "COORD.FLOOR_LEVELS_ALL_FLOORS").result.value == "NOT_RUN"
    no_floors = model_for(3).validation_model()
    no_floors["meta"].pop("floors")
    assert result_of(validate(no_floors), "COORD.FLOOR_LEVELS_ALL_FLOORS").result.value == "NOT_RUN"
    legacy = {"meta": {"units": "m"}, "entities": [{"entity_id": "S1", "entity_type": "STAIR",
                                                    "properties": {"total_height": 3.0, "riser_count": 18, "to_level": "L2"}}]}
    assert result_of(validate(legacy), "COORD.STAIR_OPENING_IN_SLAB").result.value == "NOT_RUN"  # no slabs modelled


def test_stair_opening_rule_finds_a_missing_or_wrong_opening() -> None:
    model = model_for(2)
    data = model.validation_model(drawn_records(model))
    slab = next(e for e in data["entities"] if e["entity_id"] == "SLAB-F2")
    slab["properties"]["stair_opening"] = []
    r = result_of(validate(data), "COORD.STAIR_OPENING_IN_SLAB")
    assert r.result.value == "FAIL" and "no stair opening" in r.issues[0]["message"]
    slab["properties"]["stair_opening"] = [[0.0, 0.0, 1.0, 1.0]]  # outside the footprint
    r = result_of(validate(data), "COORD.STAIR_OPENING_IN_SLAB")
    assert r.result.value == "FAIL" and any("outside the stair footprint" in i["message"] for i in r.issues)
    data = model.validation_model(drawn_records(model))  # the drawn plan lost its opening
    for rec in data["meta"]["views"]:
        if rec.get("id") == "PLAN-F2":
            rec["slab_openings"] = []
    r = result_of(validate(data), "COORD.STAIR_OPENING_IN_SLAB")
    assert r.result.value == "FAIL" and "shows no slab opening" in r.issues[0]["message"]
    data = model.validation_model(drawn_records(model))  # no slab at all on the upper level while others exist
    data["entities"] = [e for e in data["entities"] if e["entity_id"] != "SLAB-F2"]
    assert result_of(validate(data), "COORD.STAIR_OPENING_IN_SLAB").result.value == "FAIL"


def test_stair_riser_rule_checks_flights_levels_and_verified_limits() -> None:
    model = model_for(2)
    data = model.validation_model()
    st = next(e for e in data["entities"] if e["entity_id"] == "ST1")
    st["properties"]["flights"][0]["risers"] += 1
    r = result_of(validate(data), "COORD.STAIR_RISERS")
    assert r.result.value == "FAIL" and any("flights have" in i["message"] for i in r.issues)
    data = model.validation_model()
    st = next(e for e in data["entities"] if e["entity_id"] == "ST1")
    st["properties"]["riser_height"] = 0.2  # stated riser disagrees with the level difference over the risers
    assert result_of(validate(data), "COORD.STAIR_RISERS").result.value == "FAIL"
    data = model_for(2, profile={"max_riser_height": {"value": 0.17, "verified_by": "x"}}).validation_model()
    st = next(e for e in data["entities"] if e["entity_id"] == "ST1")
    assert result_of(validate(data), "COORD.STAIR_RISERS").result.value == "PASS"
    st["properties"]["limits"]["applied"]["max_riser_height"]["value"] = 0.1  # a verified limit the stair violates
    r = result_of(validate(data), "COORD.STAIR_RISERS")
    assert r.result.value == "FAIL" and any("verified-profile limit" in i["message"] for i in r.issues)


def test_stair_plan_rule_finds_wrong_riser_lines_and_a_missing_stair() -> None:
    model = model_for(3)
    data = model.validation_model(drawn_records(model))
    plan = next(v for v in data["meta"]["views"] if v.get("id") == "PLAN-F1")
    plan["stairs"][0]["riser_lines"] -= 1
    r = result_of(validate(data), "COORD.STAIR_PLAN_MATCHES_MODEL")
    assert r.result.value == "FAIL" and "riser lines" in r.issues[0]["message"]
    data = model.validation_model(drawn_records(model))
    plan = next(v for v in data["meta"]["views"] if v.get("id") == "PLAN-F2")
    plan["stairs"] = [s for s in plan["stairs"] if s["id"] != "ST2"]
    r = result_of(validate(data), "COORD.STAIR_PLAN_MATCHES_MODEL")
    assert r.result.value == "FAIL" and "not drawn on its plan" in r.issues[0]["message"]


def test_floor_level_rule_finds_inconsistent_floors() -> None:
    model = model_for(3)
    base = model.validation_model(drawn_records(model))
    data = copy.deepcopy(base)
    next(e for e in data["entities"] if e["entity_id"] == "LV-F3")["elevation"] += 0.3  # floor 3 no longer sits on slab 2
    r = result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS")
    assert r.result.value == "FAIL" and any("slab is" in i["message"] for i in r.issues)
    data = copy.deepcopy(base)
    data["meta"]["floors"][1]["clear_height"] = 2.9
    assert result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS").result.value == "FAIL"
    data = copy.deepcopy(base)  # a view that does not mark every floor
    elev = next(v for v in data["meta"]["views"] if v.get("kind") == "elevation")
    elev["levels"] = [m for m in elev["levels"] if m["level_id"] != "LV-F3"]
    r = result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS")
    assert r.result.value == "FAIL" and "LV-F3" in r.issues[0]["message"]
    data = copy.deepcopy(base)  # no entity of floor 2
    data["entities"] = [e for e in data["entities"] if e.get("level_id") != "LV-F2"]
    assert result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS").result.value == "FAIL"
    data = copy.deepcopy(base)
    data["entities"] = [e for e in data["entities"] if e["entity_type"] != "STAIR"]
    r = result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS")
    assert r.result.value == "FAIL" and any("no stair connects" in i["message"] for i in r.issues)
    data = copy.deepcopy(base)  # plans drawn for two floors only: a warning, not an error
    data["meta"]["views"] = [v for v in data["meta"]["views"] if v.get("id") != "PLAN-F3"]
    r = result_of(validate(data), "COORD.FLOOR_LEVELS_ALL_FLOORS")
    assert r.result.value == "WARN" and "no plan was drawn for floor LV-F3" in r.issues[0]["message"]


def test_per_floor_room_tables_are_checked_floor_by_floor() -> None:
    model = model_for(2)
    data = model.validation_model(drawn_records(model))
    assert result_of(validate(data), "COORD.ROOM_TABLE_AREA").result.value == "PASS"
    table = next(v for v in data["meta"]["views"] if v.get("id") == "SCH-ROOM-F2")
    next(r for r in table["rows"] if r.get("total"))["area"] += 1.0
    r = result_of(validate(data), "COORD.ROOM_TABLE_AREA")
    assert r.result.value == "FAIL" and "LV-F2" in r.issues[0]["message"]


# --------------------------------------------------------------------------- sheets, layouts, title blocks

def specs_for(model: housemodel.HouseModel, paper: str = "A3", orientation: str = "landscape", **kw: Any
              ) -> tuple[list[layouts.SheetSpec], layouts.Frame]:
    frame = layouts.frame_for(paper, orientation)
    return layouts.fit_all(layouts.sheet_specs(model, **kw), frame), frame


def measured_layouts(specs: list[layouts.SheetSpec], frame: layouts.Frame, title: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
    """What autocad.create_layout reads back from a faithful AutoCAD for these specs."""
    out = []
    for s in specs:
        _prims, block = layouts.frame_prims(s, frame, title)
        out.append({"name": s.code, "media": "ISO_full_bleed_A3_(420.00_x_297.00_MM)", "paper_mm": [frame.width, frame.height],
                    "orientation": frame.orientation, "entities": 32, "marked_entities": 32,
                    "viewports": [{"marked": True, "role": "viewport", "scale_custom": 1000.0 / s.scale, "locked": True,
                                   "on": True, "scale_denominator": float(s.scale)}],
                    "title_block": {k: {"label": v["label"], "value": v["value"]} for k, v in block.items()}})
    return out


def test_sheet_codes_follow_the_spec_and_the_floor_count() -> None:
    specs, _ = specs_for(model_for(3))
    assert [s.code for s in specs] == ["A-101", "A-102", "A-103", "A-104", "A-201", "A-202", "A-203", "A-204", "A-301",
                                      "A-501", "A-503"]
    assert [s.kind for s in specs][:4] == ["plan", "plan", "plan", "roof_plan"]
    one, _ = specs_for(model_for(1))
    assert [s.code for s in one][:2] == ["A-101", "A-201"]
    renamed, _ = specs_for(model_for(2), sheet_codes={"A-301": "KT-05"}, include=["KT-05", "A-101"])
    assert [s.code for s in renamed] == ["A-101", "KT-05"]
    with pytest.raises(layouts.LayoutError, match="unknown sheet code"):
        layouts.sheet_specs(model_for(2), include=["Z-999"])


def test_scales_are_exact_and_adjusted_only_when_the_content_does_not_fit() -> None:
    specs, frame = specs_for(model_for(2))
    plan = specs[0]
    assert plan.scale == 100 and not plan.scale_adjusted and plan.scale_label == "1:100"
    assert layouts.mm_per_model_unit(100) == pytest.approx(10.0)  # metres: 10 mm of paper per drawing unit
    vx0, vy0, vx1, vy1 = layouts.viewport_rect_for(plan, frame)
    win = layouts.viewport_window_for(plan, (vx0, vy0, vx1, vy1))
    assert (vx1 - vx0) * plan.scale / 1000.0 == pytest.approx(win[2] - win[0])
    assert (vy1 - vy0) * plan.scale / 1000.0 == pytest.approx(win[3] - win[1])
    fx0, fy0, fx1, fy1 = frame.viewport
    assert fx0 <= vx0 and vx1 <= fx1 and fy0 <= vy0 and vy1 <= fy1
    small, _ = specs_for(model_for(2), paper="A4", scales={"plan": "1:50"})
    assert small[0].scale > 100 and small[0].scale_adjusted and small[0].requested_scale == 50  # 1:50 cannot fit A4
    one, _ = specs_for(model_for(2), scales={"A-201": "1:200"})
    assert next(s for s in one if s.code == "A-201").scale == 200
    for bad in ("1/100", "2:100", "1:0", "1:1.5"):
        with pytest.raises(layouts.LayoutError):
            layouts.parse_scale(bad)
    assert layouts.parse_scale("1:100") == 100
    with pytest.raises(layouts.LayoutError, match="does not fit"):
        layouts.fit_scale((0, 0, 4000, 4000), frame, 100)


def test_paper_sizes_and_orientation() -> None:
    assert layouts.paper_mm("A3", "landscape") == (420.0, 297.0) and layouts.paper_mm("a3", "portrait") == (297.0, 420.0)
    assert layouts.paper_mm("A1", "landscape") == (841.0, 594.0)
    for bad in (("A5", "landscape"), ("A3", "diagonal")):
        with pytest.raises(layouts.LayoutError):
            layouts.paper_mm(*bad)
    land, port = layouts.frame_for("A3", "landscape"), layouts.frame_for("A3", "portrait")
    assert land.title[2] - land.title[0] == layouts.STRIP_MM and port.title[3] - port.title[1] == layouts.BAND_MM
    assert land.viewport[2] < land.title[0] and port.viewport[1] > port.title[3]  # the viewport never overlaps the title block


def test_title_block_never_fills_people_and_leaves_blank_fields() -> None:
    specs, frame = specs_for(model_for(2))
    prims, block = layouts.frame_prims(specs[0], frame, {"project": "Nha thu nghiem", "revision": "A"})
    assert block["project"]["value"] == "Nha thu nghiem" and block["revision"]["value"] == "A"
    assert block["status"]["value"] == "DRAFT" and block["sheet_code"]["value"] == "A-101" and block["scale"]["value"] == "1:100"
    for key in ("prepared_by", "checked_by", "approved_by", "client", "designer", "date"):
        assert block[key]["blank"] and block[key]["value"] == ""
    texts = {p.semantic: p.text for p in prims if p.kind == "text"}
    assert all(f"tb_label:{k}" in texts for k, _l, _w in layouts.TITLE_FIELDS)
    assert not [s for s in texts if s.startswith("tb_value:") and s.split(":")[1] in layouts.NEVER_FIELDS]
    for key in layouts.NEVER_FIELDS:
        with pytest.raises(layouts.LayoutError, match="never filled"):
            layouts.title_values(specs[0], {key: "Nguyen Van A"})
    with pytest.raises(layouts.LayoutError, match="unknown title block"):
        layouts.title_values(specs[0], {"logo": "x"})
    assert layouts.title_values(specs[0], {"date": "today"})["date"].count("/") == 2
    portrait, pframe = specs_for(model_for(2), orientation="portrait")
    _prims, pblock = layouts.frame_prims(portrait[0], pframe, None)
    assert pblock["project"]["blank"] and set(pblock) >= set(layouts.title_keys())


def sheet_data(model: housemodel.HouseModel, **title: Any) -> tuple[dict[str, Any], list[layouts.SheetSpec], layouts.Frame]:
    specs, frame = specs_for(model)
    data = model.validation_model(drawn_records(model), {
        "sheet_index": layouts.expected_sheet_index(specs, frame),
        "layouts": layouts.layouts_meta(measured_layouts(specs, frame, {"project": "P", **title}))})
    return data, specs, frame


PLOT_RULES = ("PLOT.SHEET_LAYOUT_EXISTS", "PLOT.VIEWPORT_SCALE", "PLOT.VIEWPORT_LOCKED", "PLOT.TITLE_BLOCK_FIELDS",
              "PLOT.PAPER_ORIENTATION", "PLOT.LAYOUT_SETUP")


def test_consistent_sheet_set_passes_the_plot_rules() -> None:
    data, specs, _ = sheet_data(model_for(3))
    rep = validate(data)
    for rid in PLOT_RULES:
        r = result_of(rep, rid)
        assert r.result.value == "PASS", (rid, r.evidence, [i["message"] for i in r.issues if i["severity"] != "INFO"])
    assert result_of(rep, "PLOT.SHEET_LAYOUT_EXISTS").checked == len(specs)
    metas = layouts.layouts_meta(measured_layouts(specs, layouts.frame_for("A3", "landscape")))
    assert metas[0]["paper"] == "A3" and metas[0]["scale"] == "1:100" and metas[0]["viewport_locked"] is True


def test_sheet_without_a_layout_is_found_and_extra_layouts_are_info() -> None:
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"] = [l for l in data["meta"]["layouts"] if l["name"] != "A-201"]
    r = result_of(validate(data), "PLOT.SHEET_LAYOUT_EXISTS")
    assert r.result.value == "FAIL" and "A-201" in r.issues[0]["message"]
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"].append({"name": "Layout1", "viewports": []})
    r = result_of(validate(data), "PLOT.SHEET_LAYOUT_EXISTS")
    assert r.result.value == "PASS" and r.issues[0]["severity"] == "INFO"
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["sheet_index"].append(dict(data["meta"]["sheet_index"][0]))
    assert result_of(validate(data), "PLOT.SHEET_LAYOUT_EXISTS").result.value == "FAIL"  # duplicate sheet code


def test_viewport_scale_and_lock_are_compared_with_the_declaration() -> None:
    data, _, _ = sheet_data(model_for(2))
    vp = data["meta"]["layouts"][0]["viewports"][0]
    vp["scale_denominator"] = 101.0
    r = result_of(validate(data), "PLOT.VIEWPORT_SCALE")
    assert r.result.value == "FAIL" and "1:101" in r.issues[0]["message"]
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][1]["viewports"][0]["locked"] = False
    r = result_of(validate(data), "PLOT.VIEWPORT_LOCKED")
    assert r.result.value == "FAIL" and "A-102" in r.issues[0]["message"]
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["viewports"] = []
    assert result_of(validate(data), "PLOT.VIEWPORT_SCALE").result.value == "FAIL"  # no viewport at all
    assert result_of(validate(data), "PLOT.VIEWPORT_LOCKED").result.value in ("PASS", "WARN")


def test_title_block_rule_checks_fields_values_and_people() -> None:
    data, _, _ = sheet_data(model_for(2))
    del data["meta"]["layouts"][0]["title_block"]["approved_by"]
    r = result_of(validate(data), "PLOT.TITLE_BLOCK_FIELDS")
    assert r.result.value == "FAIL" and any("approved_by" in i["message"] and i["severity"] == "ERROR" for i in r.issues)
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["title_block"]["scale"]["value"] = "1:50"
    assert result_of(validate(data), "PLOT.TITLE_BLOCK_FIELDS").result.value == "FAIL"
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["title_block"]["sheet_code"]["value"] = ""
    assert result_of(validate(data), "PLOT.TITLE_BLOCK_FIELDS").result.value == "FAIL"
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["title_block"]["prepared_by"]["value"] = "Someone"
    r = result_of(validate(data), "PLOT.TITLE_BLOCK_FIELDS")
    assert r.result.value == "WARN" and any("never writes names" in i["message"] for i in r.issues)
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["title_block"] = {}
    assert result_of(validate(data), "PLOT.TITLE_BLOCK_FIELDS").result.value == "FAIL"
    blank, _, _ = sheet_data(model_for(2))
    blank["meta"]["layouts"][0]["title_block"]["project"]["value"] = ""
    r = result_of(validate(blank), "PLOT.TITLE_BLOCK_FIELDS")
    assert r.result.value == "WARN" and "project name is blank" in [i["message"] for i in r.issues if i["severity"] == "WARNING"][0]


def test_paper_size_and_orientation_rule() -> None:
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["paper_mm"] = [297.0, 420.0]  # portrait where landscape was declared
    r = result_of(validate(data), "PLOT.PAPER_ORIENTATION")
    assert r.result.value == "FAIL" and "A-101" in r.issues[0]["message"]
    data, _, _ = sheet_data(model_for(2))
    data["meta"]["layouts"][0]["paper_mm"] = None
    assert result_of(validate(data), "PLOT.PAPER_ORIENTATION").result.value == "FAIL"


def pdf_state(specs: list[layouts.SheetSpec], **over: Any) -> dict[str, Any]:
    pages = [{"page": i + 1, "size_mm": [420.2, 297.0], "paper_mm": [420.0, 297.0], "nonwhite_ratio": 0.05, "vector_paths": 100,
              "text_chars": 400, "sheet_code": s.code, "codes_found": [s.code]} for i, s in enumerate(specs)]
    return {"ok": True, "rendered": True, "note": "n", "pages": len(pages), "page_checks": pages, **over}


def test_pdf_rule_checks_pages_blankness_text_codes_and_size() -> None:
    data, specs, _ = sheet_data(model_for(2))
    data["meta"]["pdf_render_check"] = pdf_state(specs)
    r = result_of(validate(data), "PLOT.PDF_PAGES_MATCH_SHEETS")
    assert r.result.value == "PASS" and r.checked == len(specs)
    bad = pdf_state(specs)
    bad["page_checks"] = bad["page_checks"][:-1]
    data["meta"]["pdf_render_check"] = bad
    r = result_of(validate(data), "PLOT.PDF_PAGES_MATCH_SHEETS")
    assert r.result.value == "FAIL" and "pages but" in r.issues[0]["message"]
    for key, value, text in (("nonwhite_ratio", 0.0, "blank"), ("sheet_code", "A-999", "not found"), ("size_mm", [297.0, 420.0], "differs")):
        state = pdf_state(specs)
        state["page_checks"][1][key] = value
        data["meta"]["pdf_render_check"] = state
        r = result_of(validate(data), "PLOT.PDF_PAGES_MATCH_SHEETS")
        assert r.result.value == "FAIL" and any(text in i["message"] for i in r.issues), key
    state = pdf_state(specs)
    state["page_checks"][0]["text_chars"] = 0
    data["meta"]["pdf_render_check"] = state
    assert result_of(validate(data), "PLOT.PDF_PAGES_MATCH_SHEETS").result.value == "WARN"
    data["meta"]["pdf_render_check"] = {"ok": False, "rendered": False, "note": "no renderer", "page_checks": []}
    assert result_of(validate(data), "PLOT.PDF_PAGES_MATCH_SHEETS").result.value == "NOT_RUN"
    assert result_of(validate(data), "PLOT.PDF_RENDER_CHECKED").result.value == "FAIL"  # and the old rule says it was not inspected


# --------------------------------------------------------------------------- re-open evidence and FINAL_STATUS

def counts(model_space: int = 100, layouts_: int = 3) -> dict[str, Any]:
    return {"model_space": model_space, "model_by_object": {"AcDbLine": 60, "AcDbMText": 40}, "model_marked": model_space,
            "layouts": {f"A-10{i}": {"entities": 32, "marked": 32, "media": "m", "paper_mm": [420.0, 297.0],
                                     "viewports": [{"scale_denominator": 100.0, "locked": True}]} for i in range(layouts_)}}


def test_compare_counts_finds_every_kind_of_difference() -> None:
    assert layouts.compare_counts(counts(), counts()) == []
    assert layouts.compare_counts(None, counts()) == ["nothing to compare with"]
    changed = counts(99)
    changed["model_by_object"]["AcDbLine"] = 59
    changed["layouts"]["A-101"]["entities"] = 31
    changed["layouts"]["A-102"]["viewports"][0]["locked"] = False
    changed["layouts"]["A-102"]["paper_mm"] = [297.0, 420.0]
    diffs = layouts.compare_counts(counts(), changed)
    text = " | ".join(diffs)
    assert "model space has 99" in text and "AcDbLine" in text and "layout A-101 entities" in text
    assert "viewports" in text and "paper" in text
    assert any("layouts" in d for d in layouts.compare_counts(counts(), counts(layouts_=2)))


def test_reopen_rule_and_evidence_drive_final_status() -> None:
    model = model_for(2)
    ok = model.validation_model(extra_meta={"reopen_check": {"opened": True, "differences": [], "compared": "x"}})
    assert result_of(validate(ok), "FILE.REOPEN_COUNTS").result.value == "PASS"
    bad = model.validation_model(extra_meta={"reopen_check": {"opened": True, "differences": ["model space has 1 entities"]}})
    r = result_of(validate(bad), "FILE.REOPEN_COUNTS")
    assert r.result.value == "FAIL" and "differs" in r.issues[0]["message"]
    not_opened = model.validation_model(extra_meta={"reopen_check": {"opened": False}})
    assert result_of(validate(not_opened), "FILE.REOPEN_COUNTS").result.value == "FAIL"


def test_report_derives_dwg_evidence_from_the_reopen_record(tmp_path: Path) -> None:
    dwg = tmp_path / "x.dwg"
    dwg.write_bytes(b"AC1032" + b"\0" * 100)
    model = validation.DrawingModel.from_dict(model_for(2).validation_model())
    rep_ok = report.build_report(project="p", model=model, outputs=[str(dwg)],
                                 evidence={"dwg_reopen_check": {"opened": True, "differences": [], "compared": "the same drawing"},
                                           "dwg_opened_in_autocad": False})  # the flag is overridden by the evidence
    assert any("re-open evidence" in x and "equal" in x for x in rep_ok["LIMITATIONS"])
    assert not any("not confirmed to open" in x for x in rep_ok["LIMITATIONS"])
    rep_bad = report.build_report(project="p", model=model, outputs=[str(dwg)],
                                  evidence={"dwg_reopen_check": {"opened": True, "differences": ["a"], "compared": "c"},
                                            "dwg_opened_in_autocad": True})  # the flag cannot override a failed re-open
    assert rep_bad["FINAL_STATUS"]["verified_level"] == "DRAFT"
    assert any("NOT confirmed" in x for x in rep_bad["LIMITATIONS"])
    assert "the DWG was not confirmed to open in AutoCAD" in rep_bad["FINAL_STATUS"]["blocked_at"]["CAD_VALID"]
    flag_only = report.build_report(project="p", model=model, outputs=[str(dwg)], evidence={"dwg_opened_in_autocad": True})
    assert any("caller flag without re-open evidence" in x for x in flag_only["LIMITATIONS"])


# --------------------------------------------------------------------------- PDF checks (system Python / PyMuPDF)

def test_find_system_python_returns_none_or_a_working_interpreter() -> None:
    py = pdfcheck.find_system_python()
    if py is not None:
        assert subprocess.run([py, "-c", "import pymupdf"], capture_output=True, cwd=str(Path.cwd())).returncode == 0
    assert pdfcheck.find_system_python("C:/no/such/python.exe") in (None, py)


def make_pdf(py: str, path: Path, pages: list[tuple[float, float, str]]) -> None:
    script = (
        "import sys, json, pymupdf\n"
        "doc = pymupdf.open()\n"
        "for w, h, text in json.loads(sys.argv[2]):\n"
        "    p = doc.new_page(width=w, height=h)\n"
        "    if text:\n"
        "        p.insert_text((50, 100), text, fontsize=24)\n"
        "        p.draw_rect(pymupdf.Rect(30, 30, w - 30, h - 30), width=2)\n"
        "doc.save(sys.argv[1])\n")
    probe = path.with_suffix(".mk.py")
    probe.write_text(script, encoding="utf-8")
    run = subprocess.run([py, str(probe), str(path), json.dumps(pages)], capture_output=True, text=True, cwd=str(path.parent))
    assert run.returncode == 0, run.stderr


def test_render_check_with_a_real_pdf(tmp_path: Path) -> None:
    py = pdfcheck.find_system_python()
    if py is None:
        pytest.skip("no system Python with PyMuPDF")
    a3 = (420.0 / 25.4 * 72, 297.0 / 25.4 * 72)
    pdf = tmp_path / "ok.pdf"
    make_pdf(py, pdf, [(*a3, "A-101 plan"), (*a3, "A-102 plan")])
    sheets = [{"code": "A-101", "paper_mm": [420.0, 297.0]}, {"code": "A-102", "paper_mm": [420.0, 297.0]}]
    ok = pdfcheck.render_check(pdf, sheets, out_dir=tmp_path / "png", python=py)
    assert ok["ok"] and ok["rendered"] and ok["pages"] == 2 and (tmp_path / "png" / "page_01.png").is_file()
    assert all(p["nonwhite_ratio"] > 0.0005 and p["text_chars"] > 0 for p in ok["page_checks"])
    blank = tmp_path / "blank.pdf"
    make_pdf(py, blank, [(*a3, "A-101 plan"), (*a3, "")])
    bad = pdfcheck.render_check(blank, sheets, python=py)
    assert not bad["ok"] and any("page 2 is blank" in p for p in bad["problems"]) and any("no text" in p for p in bad["problems"])
    short = pdfcheck.render_check(pdf, sheets + [{"code": "A-103", "paper_mm": [420.0, 297.0]}], python=py)
    assert not short["ok"] and any("2 pages for 3 sheets" in p for p in short["problems"])
    wrong_size = tmp_path / "a4.pdf"
    make_pdf(py, wrong_size, [(595.0, 842.0, "A-101"), (*a3, "A-102")])
    sized = pdfcheck.render_check(wrong_size, sheets, python=py)
    assert not sized["ok"] and any("page 1 is" in p and "layout paper" in p for p in sized["problems"])
    merged = tmp_path / "merged.pdf"
    single = tmp_path / "one.pdf"
    make_pdf(py, single, [(*a3, "A-101")])
    result = pdfcheck.merge_pdfs([single, single, pdf], merged, py)
    assert result["ok"] and result["pages"] == 4
    assert pdfcheck.render_check(merged, sheets, python=py)["pages"] == 4


def test_render_check_says_so_when_it_cannot_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(pdfcheck, "find_system_python", lambda configured=None: None)
    res = pdfcheck.render_check(pdf, [{"code": "A-101"}])
    assert res["ok"] is False and res["rendered"] is False and "NOT rendered" in res["note"]
    assert pdfcheck.merge_pdfs([pdf], tmp_path / "m.pdf")["ok"] is False
    assert pdfcheck.render_check(tmp_path / "missing.pdf", [])["rendered"] is False


# --------------------------------------------------------------------------- idempotency markers

def test_view_marker_depends_on_what_is_drawn_not_on_the_tool() -> None:
    args = {**PARAMS, "floors": 2, "new_document": True, "if_exists": "replace", "sides": ["front"]}
    a = autocad_tools._view_marker(args, "ELV-FRONT", (4.0, -16.0))  # noqa: SLF001
    assert a.startswith("LGA:") and a.endswith(":ELV-FRONT")
    assert a == autocad_tools._view_marker({**args, "new_document": False, "sides": ["front", "rear"]}, "ELV-FRONT", (4.0, -16.0))  # noqa: SLF001
    assert a != autocad_tools._view_marker(args, "ELV-FRONT", (5.0, -16.0))  # noqa: SLF001
    assert a != autocad_tools._view_marker(args, "ELV-REAR", (4.0, -16.0))  # noqa: SLF001
    assert a != autocad_tools._view_marker({**args, "floors": 3}, "ELV-FRONT", (4.0, -16.0))  # noqa: SLF001
    assert a != autocad_tools._view_marker({**args, "layer_map": {"A-DOOR": "X"}}, "ELV-FRONT", (4.0, -16.0))  # noqa: SLF001
    keyed = autocad_tools._view_marker({**args, "idempotency_key": "job-7"}, "ELV-FRONT", (9.0, 9.0))  # noqa: SLF001
    assert keyed == "LGA:job-7:ELV-FRONT"


# --------------------------------------------------------------------------- tool handlers with a fake provider

class FakeExecutor:
    async def run(self, timeout: float, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)


class FakeAutoCad:
    """Behaves like the real provider where it matters to the handlers: a marker store (idempotency), layouts that are
    read back from what was asked for, and PDF files on disk."""

    def __init__(self) -> None:
        self.executor = FakeExecutor()
        self.entities: dict[str, int] = {}  # marker -> entity count
        self.total = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.layouts: dict[str, dict[str, Any]] = {}
        self.saved_counts = counts()
        self.reopen_after: dict[str, Any] | None = None
        self.unsaved = False

    def _store(self, marker: str | None, expected: int, if_exists: str, expect: int | None) -> tuple[bool, int, int]:
        """(skipped, reused, replaced)"""
        if marker is None or marker not in self.entities:
            return False, 0, 0
        have = self.entities[marker]
        if if_exists == "skip" and (expect is None or have == expect):
            return True, have, 0
        if if_exists == "error":
            raise ToolError(ErrorCode.CONFLICT, "exists")
        self.total -= have
        del self.entities[marker]
        return False, 0, have

    def draw_plan(self, plan: floorplan.Plan, new_document: bool, origin: Any, *, deadline: float, marker: str | None = None,
                  if_exists: str = "skip", expect: int | None = None) -> PlanResult:
        n = floorplan.expected_entity_count(plan)
        self.calls.append(("draw_plan", {"origin": origin, "marker": marker, "new_document": new_document}))
        skipped, reused, replaced = self._store(marker, n, if_exists, expect)
        if skipped:
            return PlanResult("Drawing9.dwg", {"other": reused}, True, reused, 0)
        if marker:
            self.entities[marker] = n
        self.total += n
        counts_ = {"wall": 2 * len(plan.walls), "text": len(plan.labels) + 2}
        counts_["other"] = n - sum(counts_.values())
        return PlanResult("Drawing9.dwg", counts_, False, 0, replaced)

    def draw_prims(self, prims: Any, layers_: Any, origin: Any, text_height: float, new_document: bool, *, deadline: float,
                   marker: str | None = None, if_exists: str = "skip", expect: int | None = None) -> PrimsResult:
        counts_: dict[str, int] = {}
        by_layer: dict[str, int] = {}
        for p in prims:
            for k in (["poly", "hatch"] if p.kind == "cut" else [p.kind]):
                counts_[k] = counts_.get(k, 0) + 1
            name = layers_[p.layer][0]
            by_layer[name] = by_layer.get(name, 0) + (2 if p.kind == "cut" else 1)
        n = sum(counts_.values())
        self.calls.append(("draw_prims", {"origin": origin, "marker": marker, "n": n}))
        skipped, reused, replaced = self._store(marker, n, if_exists, expect)
        before = self.total
        if skipped:
            return PrimsResult("Drawing9.dwg", {"reused": reused}, by_layer, before, before, [], [], True, reused, 0)
        if marker:
            self.entities[marker] = n
        self.total += n
        return PrimsResult("Drawing9.dwg", counts_, by_layer, before, self.total, list(by_layer), [], False, 0, replaced)

    def create_layout(self, spec: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        name = spec["name"]
        self.calls.append(("create_layout", {"name": name, "marker": spec["marker"]}))
        old = self.layouts.get(name)
        if old is not None and spec["if_exists"] == "skip":
            if old["marker"] != spec["marker"]:
                raise ToolError(ErrorCode.CONFLICT, f"layout {name!r} exists from another operation")
            if old["marked_entities"] == spec["expected_marked"]:
                return {**old, "reused": True, "replaced": False}
        prims = spec["frame_prims"]
        title_block: dict[str, dict[str, str]] = {}
        for p in prims:
            if p.kind == "text" and p.semantic.startswith("tb_"):
                kind, _, key = p.semantic.partition(":")
                title_block.setdefault(key, {})["label" if kind == "tb_label" else "value"] = p.text
        for entry in title_block.values():
            entry.setdefault("value", "")
        vx0, vy0, vx1, vy1 = spec["viewport_mm"]
        info = {"name": name, "media": spec["media_candidates"][0], "rotation": 0,
                "paper_mm": [float(v) for v in spec["media_candidates"][0].split("(")[1].split("_MM")[0].replace(".00", "").split("_x_")],
                "orientation": "landscape", "marker": spec["marker"], "entities": len(prims) + 1, "marked_entities": len(prims) + 1,
                "viewports": [{"marked": True, "role": "viewport", "scale_custom": spec["unit_mm"] / spec["scale_denominator"],
                               "scale_denominator": float(spec["scale_denominator"]), "locked": True, "on": True,
                               "width_mm": vx1 - vx0, "height_mm": vy1 - vy0}],
                "title_block": title_block, "reused": False, "replaced": old is not None, "frame_entities_drawn": len(prims),
                "view_window": spec["view_window"]}
        self.layouts[name] = info
        return info

    def read_layouts(self, names: Any, *, deadline: float) -> list[dict[str, Any]]:
        return [v for k, v in self.layouts.items() if not names or k in names]

    def plot_layouts(self, names: list[str], path: Path, *, deadline: float) -> dict[str, Any]:
        self.calls.append(("plot_layouts", {"names": list(names), "path": str(path)}))
        path.write_bytes(b"%PDF-1.7\nfake page for " + names[0].encode())
        return {"path": str(path), "plotted": True, "device": "DWG To PDF.pc3", "layout": names[0], "restore_errors": []}

    def reopen_check(self, path: Path, keep_open: bool, *, deadline: float) -> dict[str, Any]:
        if self.unsaved:
            raise ToolError(ErrorCode.CONFLICT, "unsaved changes")
        return {"path": str(path), "was_open_before": True, "before": self.saved_counts,
                "after": self.reopen_after or self.saved_counts, "opened_name": path.name, "opened_read_only": True,
                "kept_open": keep_open}


@dataclass
class TmpContext(ToolContext):
    tmp: Path | None = None

    def artifact_dir(self, kind: str) -> Path:
        assert self.tmp is not None
        directory = self.tmp / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class Harness:
    def __init__(self, config: Config, tmp: Path) -> None:
        self.config = config
        self.tmp = tmp
        self.registry = build_registry(config.path("paths.tools_dir"))
        self.services = Services(config)
        self.provider = FakeAutoCad()
        self.services._extra["autocad"] = self.provider  # noqa: SLF001

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        spec = self.registry.specs[tool]
        ctx = TmpContext(request_id=uuid.uuid4().hex, task_id="multistorey-unit", step_id=tool,
                         deadline=time.monotonic() + spec.timeout_ms / 1000.0, config=self.config,
                         services=self.services, cancel_event=threading.Event(), tmp=self.tmp)
        _, outcome = await self.registry.invoke(ctx, tool, args)
        return outcome


def all_checks_pass(outcome: Any) -> bool:
    return all(c.passed for c in outcome.checks)


HOUSE3 = {**PARAMS, "floors": 3}


async def test_new_tools_are_in_the_contract(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    for name in ("autocad.draw_stair", "autocad.create_layout", "autocad.plot_sheets", "autocad.reopen_check"):
        spec = h.registry.specs[name]
        assert spec.timeout_ms >= 300000
    assert not [t for t in h.registry.unbound_tools() if t.startswith("autocad.")]
    contract = json.loads((Path(config.path("paths.tools_dir")) / "autocad.json").read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in contract["tools"]}
    for name in ("autocad.draw_floor_plan", "autocad.draw_elevation", "autocad.draw_section", "autocad.draw_schedules",
                 "autocad.house_model", "autocad.draw_stair", "autocad.create_layout"):
        props = by_name[name]["input_schema"]["properties"]
        assert {"floors", "floor_heights", "stair", "profile"} <= set(props), name
    for name in ("autocad.draw_floor_plan", "autocad.draw_elevation", "autocad.draw_section", "autocad.draw_schedules",
                 "autocad.draw_stair", "autocad.create_layout"):
        assert {"if_exists", "idempotency_key"} <= set(by_name[name]["input_schema"]["properties"]), name
    assert by_name["autocad.draw_elevation"]["idempotent"] is True and "LGA_OP" in by_name["autocad.draw_elevation"]["description"]
    for name in ("autocad.draw_stair", "autocad.create_layout", "autocad.plot_sheets", "autocad.reopen_check"):
        t = by_name[name]
        assert t["side_effects"] == "yes" and t["verification"] == "self" and t["policy_category"] == "autocad.write", name
    assert "prepared" in by_name["autocad.create_layout"]["description"] and "NEVER" in by_name["autocad.create_layout"]["description"]
    # the single-storey call of an old planner still validates against the extended schema
    assert by_name["autocad.draw_floor_plan"]["input_schema"]["additionalProperties"] is False


async def test_draw_floor_plan_floors_three_and_its_twin_call(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    out = await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False})
    assert all_checks_pass(out)
    assert [f["floor"] for f in out.data["floors"]] == [1, 2, 3]
    origins = [tuple(f["origin"]) for f in out.data["floors"]]
    assert origins == [houseviews.floor_plan_origin(model_for(3), n) for n in (1, 2, 3)]
    assert [s["id"] for s in out.data["stairs"]] == ["ST1", "ST2"] and out.data["stairs"][0]["riser_count"] == 20
    kinds = [v["kind"] for v in out.data["views"]]
    assert kinds == ["plan", "plan", "plan", "roof_plan"] and out.data["views"][1]["slab_openings"]
    first_total = h.provider.total
    assert first_total > 0
    again = await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False})
    assert all_checks_pass(again) and h.provider.total == first_total
    assert all(f["idempotency"]["skipped"] for f in again.data["floors"])
    assert "already present" in again.observation
    replaced = await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False, "if_exists": "replace"})
    assert all_checks_pass(replaced) and h.provider.total == first_total
    assert all(f["idempotency"]["replaced"] > 0 for f in replaced.data["floors"])
    other = await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False, "origin": {"x": 100, "y": 0}})
    assert h.provider.total == 2 * first_total  # another origin is another operation
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False, "if_exists": "error"})
    assert exc.value.code == ErrorCode.CONFLICT
    assert other.data["operation_id"] != out.data["operation_id"]


async def test_single_storey_floor_plan_is_still_one_call_and_idempotent(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    out = await h.call("autocad.draw_floor_plan", {**PARAMS, "new_document": False})
    assert all_checks_pass(out) and "floors" not in out.data and out.data["rooms"]
    total = h.provider.total
    again = await h.call("autocad.draw_floor_plan", {**PARAMS, "new_document": False})
    assert all_checks_pass(again) and h.provider.total == total and again.data["idempotency"]["skipped"]
    assert [c[0] for c in h.provider.calls] == ["draw_plan", "draw_plan"]


async def test_partial_earlier_call_is_completed_not_doubled(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    await h.call("autocad.draw_elevation", {**PARAMS, "sides": ["front"]})
    marker = next(iter(h.provider.entities))
    h.provider.entities[marker] -= 5  # an earlier call was cut short: 5 entities are missing
    h.provider.total -= 5
    out = await h.call("autocad.draw_elevation", {**PARAMS, "sides": ["front"]})
    idem = out.data["views"][0]["idempotency"]
    assert all_checks_pass(out) and not idem["skipped"] and idem["replaced"] > 0
    assert h.provider.entities[marker] == out.data["views"][0]["entities"]


async def test_draw_tools_share_entities_through_the_marker(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    await h.call("autocad.draw_floor_plan", {**HOUSE3, "new_document": False})
    n = h.provider.total
    stair = await h.call("autocad.draw_stair", {**HOUSE3, "floor": 2})
    assert all_checks_pass(stair) and h.provider.total == n and stair.data["view"]["idempotency"]["skipped"]
    assert stair.data["view"]["slab_openings"][0]["stair_id"] == "ST1" and len(stair.data["stairs"]) == 2
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.draw_stair", {**PARAMS, "floors": 1, "floor": 1})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.draw_stair", {**HOUSE3, "floor": 4})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_all_view_tools_are_idempotent_and_floors_are_passed_on(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    runs = [("autocad.draw_elevation", {**HOUSE3, "sides": list(housemodel.SIDES)}), ("autocad.draw_section", dict(HOUSE3)),
            ("autocad.draw_schedules", dict(HOUSE3))]
    first = {}
    for tool, args in runs:
        first[tool] = await h.call(tool, args)
        assert all_checks_pass(first[tool])
    total = h.provider.total
    for tool, args in runs:
        again = await h.call(tool, args)
        assert all_checks_pass(again)
    assert h.provider.total == total
    sec = first["autocad.draw_section"].data["view"]
    assert sec["cut"]["axis"] == "x" and sec["floors"] == 3 and [s["id"] for s in sec["stairs"]] == ["ST1", "ST2"]
    assert len(first["autocad.draw_schedules"].data["views"]) == 9
    assert len(first["autocad.draw_elevation"].data["views"][0]["levels"]) == 6
    lines = (tmp_path / "autocad" / "operations.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(line)["kind"] == "CREATE" for line in lines) and len(lines) >= 6


async def test_house_model_returns_stairs_and_takes_measured_sheet_evidence(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    out = await h.call("autocad.house_model", {**HOUSE3, "profile": {"max_riser_height": {"value": 0.15, "verified_by": "t"}}})
    detail = out.data["summary"]["stair_detail"]
    assert out.data["summary"]["floors"] == 3 and len(detail) == 2 and detail[0]["riser_height"] <= 0.15
    assert "risers of" in out.observation and "recomputed" in out.observation
    assert out.data["model"]["meta"]["floors"][2]["ceiling_id"] == "LV-CEIL3"
    sheet = await h.call("autocad.house_model", {**PARAMS, "floors": 2, "reopen_check": {"opened": True, "differences": []},
                                                  "pdf_render_check": {"ok": True, "page_checks": []}})
    assert sheet.data["model"]["meta"]["reopen_check"]["opened"] is True
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.house_model", {**PARAMS, "floors": 2, "stair": {"width": 2.95, "tread": 0.3}})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_create_layout_reads_everything_back_and_is_idempotent(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    args = {**PARAMS, "floors": 2, "paper": "A3", "orientation": "landscape",
            "title_block": {"project": "Nha thu nghiem", "status": "DRAFT"}, "sheets": ["A-101", "A-102", "A-301"]}
    out = await h.call("autocad.create_layout", args)
    assert all_checks_pass(out), [c for c in out.checks if not c.passed]
    assert [s["code"] for s in out.data["sheet_index"]] == ["A-101", "A-102", "A-301"]
    assert all(s["paper_mm"] == [420.0, 297.0] and s["scale"] == "1:100" for s in out.data["sheet_index"])
    block = out.data["layouts"][0]["title_block"]
    assert block["project"]["value"] == "Nha thu nghiem" and block["prepared_by"]["value"] == ""
    assert block["checked_by"]["value"] == "" and block["approved_by"]["value"] == ""
    rep = validate(model_for(2).validation_model(extra_meta={"layouts": out.data["layouts"], "sheet_index": out.data["sheet_index"]}))
    for rid in PLOT_RULES:
        assert result_of(rep, rid).result.value == "PASS", (rid, result_of(rep, rid).evidence)
    calls = len(h.provider.calls)
    again = await h.call("autocad.create_layout", args)
    assert all_checks_pass(again)
    # a second identical call finds its layouts (same marker) and adds none
    assert len(h.provider.calls) == calls + 3 and len(h.provider.layouts) == 3
    assert [c[1]["marker"] for c in h.provider.calls[:3]] == [c[1]["marker"] for c in h.provider.calls[3:6]]
    for bad in ({"title_block": {"prepared_by": "Nguyen Van A"}}, {"scales": {"plan": "100"}}, {"paper": "A5"}):
        with pytest.raises(ToolError) as exc:
            await h.call("autocad.create_layout", {**args, **bad})
        assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.create_layout", {**args, "sheets": ["Z-1"]})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:  # the same sheet code with other content is not silently left as it was
        await h.call("autocad.create_layout", {**args, "paper": "A4", "scales": {"plan": "1:50"}})
    assert exc.value.code == ErrorCode.CONFLICT
    changed = await h.call("autocad.create_layout", {**args, "paper": "A4", "scales": {"plan": "1:50"}, "if_exists": "replace"})
    assert any(a for a in changed.data["scale_adjusted"]) and "Scales adjusted to fit" in changed.observation
    assert all_checks_pass(changed) and changed.data["layouts"][0]["paper"] == "A4"


async def test_create_layout_fails_when_autocad_reads_back_something_else(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    original = h.provider.create_layout

    def unlocked(spec: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        info = original(spec, deadline=deadline)
        info["viewports"][0]["locked"] = False
        info["viewports"][0]["scale_denominator"] = 99.0
        return info

    h.provider.create_layout = unlocked  # type: ignore[method-assign]
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.create_layout", {**PARAMS, "floors": 2, "sheets": ["A-101"]})
    assert exc.value.code == ErrorCode.VERIFICATION_FAILED
    assert "viewport_scale" in exc.value.message and "viewport_locked" in exc.value.message


def fake_pdf_tools(monkeypatch: pytest.MonkeyPatch, python: str | None, pages_written: dict[str, int] | None = None) -> None:
    monkeypatch.setattr(pdfcheck, "find_system_python", lambda configured=None: python)

    def merge(parts: Any, out: Path, py: str | None = None) -> dict[str, Any]:
        out.write_bytes(b"%PDF-1.7\nmerged " + b" ".join(Path(p).read_bytes() for p in parts))
        return {"ok": True, "pages": len(parts), "note": "merged"}

    def render(pdf: Path, sheets: list[dict[str, Any]], *, out_dir: Any = None, python: Any = None, **_: Any) -> dict[str, Any]:
        n = (pages_written or {}).get("pages", len(sheets))
        pages = [{"page": i + 1, "size_mm": [420.0, 297.0], "paper_mm": s["paper_mm"], "nonwhite_ratio": 0.04, "vector_paths": 10,
                  "text_chars": 50, "sheet_code": s["code"], "codes_found": [s["code"]], "png": None}
                 for i, s in enumerate(sheets[:n])]
        problems = [] if n == len(sheets) else [f"{n} pages for {len(sheets)} sheets"]
        return {"ok": not problems, "rendered": True, "note": "rendered", "pages": n, "page_checks": pages, "expected_pages": len(sheets),
                "problems": problems, "renderer": "fake"}

    monkeypatch.setattr(pdfcheck, "merge_pdfs", merge)
    monkeypatch.setattr(pdfcheck, "render_check", render)


async def make_layouts(h: Harness, sheets: list[str]) -> Any:
    return await h.call("autocad.create_layout", {**PARAMS, "floors": 2, "sheets": sheets, "title_block": {"project": "P"}})


async def test_plot_sheets_builds_one_pdf_and_reports_the_render(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pdf_tools(monkeypatch, "fake-python")
    h = Harness(config, tmp_path)
    layouts_out = await make_layouts(h, ["A-101", "A-102", "A-301"])
    target = tmp_path / "out" / "set.pdf"
    out = await h.call("autocad.plot_sheets", {"path": str(target), "overwrite": False})
    assert all_checks_pass(out) and target.is_file() and out.data["sheets"] == ["A-101", "A-102", "A-301"]
    assert out.data["pages"] == 3 and out.data["pdf_render_check"]["rendered"] is True and out.data["pdf_render_check"]["ok"]
    plotted = [c[1] for c in h.provider.calls if c[0] == "plot_layouts"]
    assert [p["names"] for p in plotted] == [["A-101"], ["A-102"], ["A-301"]]
    assert not list(target.parent.glob("*.part*.pdf"))  # the single-sheet files are gone after the merge
    meta_rep = validate(model_for(2).validation_model(extra_meta={
        "sheet_index": layouts_out.data["sheet_index"], "layouts": layouts_out.data["layouts"],
        "pdf_render_check": out.data["pdf_render_check"]}))
    assert result_of(meta_rep, "PLOT.PDF_PAGES_MATCH_SHEETS").result.value == "PASS"
    with pytest.raises(ToolError) as exc:  # an existing file needs overwrite
        await h.call("autocad.plot_sheets", {"path": str(target), "overwrite": False})
    assert exc.value.code == ErrorCode.CONFLICT
    again = await h.call("autocad.plot_sheets", {"path": str(target), "overwrite": True, "sheets": ["A-301", "A-101"]})
    assert again.data["sheets"] == ["A-301", "A-101"] and "backup" in again.data and Path(again.data["backup"]["backup"]).is_file()
    one = await h.call("autocad.plot_sheets", {"path": str(tmp_path / "one.pdf"), "overwrite": False, "sheets": ["A-102"]})
    assert one.data["pages"] == 1


async def test_plot_sheets_page_count_mismatch_fails_the_check(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pdf_tools(monkeypatch, "fake-python", {"pages": 2})
    h = Harness(config, tmp_path)
    await make_layouts(h, ["A-101", "A-102", "A-301"])
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.plot_sheets", {"path": str(tmp_path / "x.pdf"), "overwrite": False})
    assert exc.value.code == ErrorCode.VERIFICATION_FAILED and "pdf_pages_equal_sheets" in exc.value.message


async def test_plot_sheets_without_pymupdf_says_it_could_not_merge_or_render(config: Config, tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pdf_tools(monkeypatch, None)
    h = Harness(config, tmp_path)
    await make_layouts(h, ["A-101", "A-102"])
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.plot_sheets", {"path": str(tmp_path / "x.pdf"), "overwrite": False})
    assert exc.value.code == ErrorCode.EXECUTION_FAILED and "PyMuPDF" in exc.value.message
    single = await h.call("autocad.plot_sheets", {"path": str(tmp_path / "one.pdf"), "overwrite": False, "sheets": ["A-101"]})
    assert single.data["pdf_render_check"] is None and "NOT rendered" in single.observation
    assert single.data["pages"] in (None, 1)
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.plot_sheets", {"path": str(tmp_path / "y.pdf"), "overwrite": False, "sheets": ["Z-9"]})
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_plot_sheets_needs_layouts(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pdf_tools(monkeypatch, "fake-python")
    h = Harness(config, tmp_path)
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.plot_sheets", {"path": str(tmp_path / "x.pdf"), "overwrite": False})
    assert exc.value.code == ErrorCode.NOT_FOUND and "create them first" in exc.value.message


async def test_reopen_check_compares_counts_and_gives_evidence(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    dwg = tmp_path / "saved.dwg"
    dwg.write_bytes(b"AC1032" + b"\0" * 50)
    out = await h.call("autocad.reopen_check", {"path": str(dwg)})
    assert all_checks_pass(out) and out.data["evidence"]["dwg_opened_in_autocad"] is True
    assert out.data["reopen_check"]["differences"] == [] and "equal" in out.observation
    h.provider.reopen_after = counts(99)
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.reopen_check", {"path": str(dwg)})
    assert exc.value.code == ErrorCode.VERIFICATION_FAILED and "model space has 99" in exc.value.message
    h.provider.reopen_after = None
    h.provider.unsaved = True
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.reopen_check", {"path": str(dwg)})
    assert exc.value.code == ErrorCode.CONFLICT
    h.provider.unsaved = False
    compared = await h.call("autocad.reopen_check", {"path": str(dwg), "expected_counts": counts()})
    assert compared.data["reopen_check"]["compared"].startswith("the same drawing")
    with pytest.raises(ToolError) as exc:
        await h.call("autocad.reopen_check", {"path": str(tmp_path / "x.txt")})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_report_uses_the_reopen_record_from_the_tool(config: Config, tmp_path: Path) -> None:
    h = Harness(config, tmp_path)
    dwg = tmp_path / "saved.dwg"
    dwg.write_bytes(b"AC1032" + b"\0" * 50)
    reopened = await h.call("autocad.reopen_check", {"path": str(dwg)})
    model = await h.call("autocad.house_model", {**PARAMS, "floors": 2, "reopen_check": reopened.data["reopen_check"]})
    verdict = await h.call("autocad.validate_model", {"model": model.data["model"]})
    assert {r["rule_id"]: r["result"] for r in verdict.data["results"]}["FILE.REOPEN_COUNTS"] == "PASS"
    rep = await h.call("autocad.report", {"project": "p", "model": model.data["model"], "outputs": [str(dwg)],
                                           "evidence": reopened.data["evidence"]})
    assert rep.data["final_status"] in ("DRAFT", "CAD_VALID")  # never above what was verified
    assert any("re-open evidence" in x for x in rep.data["report"]["LIMITATIONS"])
    assert rep.data["final_status"] not in ("COORDINATED", "CODE_REVIEWED")
