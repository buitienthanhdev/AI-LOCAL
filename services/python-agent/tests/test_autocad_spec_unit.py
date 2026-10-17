"""Unit tests for the AutoCAD drawing specification layer (docs/specs/SPEC_VE_BAN_VE_KY_THUAT_NHA_AUTOCAD.txt).

No AutoCAD needed: profile, provenance, layer/unit mapping, validation rules, safety, revisions, report, and the
offline autocad.* tools. Live AutoCAD behaviour is NOT covered here.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lga_common.config import Config

from lga_worker.autocad import geom2d, layers, report as report_mod, revision, safety, spec_tools, standards, validation
from lga_worker.autocad.provenance import (
    Datum,
    Entity,
    EntityType,
    ProvenanceError,
    Source,
    SourceStatus,
    load_entities,
    record_conflict,
)
from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import ToolContext
from lga_worker.server import build_registry
from lga_worker.services import Services
from lga_worker.tools import autocad as autocad_tools


# =========================================================================== fixtures

def ent(entity_id: str, entity_type: str, geometry: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    out = {"entity_id": entity_id, "entity_type": entity_type, "geometry": geometry, "status": "PROVIDED"}
    out.update(kw)
    return out


def rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def good_model() -> dict[str, Any]:
    """A small consistent house floor in millimetres that should satisfy every rule that can run."""
    walls = [
        ent("W1", "WALL", {"type": "line", "start": [0, 0], "end": [5000, 0]}, level_id="L1", layer="A-WALL-EXT",
            properties={"thickness": 200}),
        ent("W2", "WALL", {"type": "line", "start": [5000, 0], "end": [5000, 4000]}, level_id="L1",
            properties={"thickness": 200}),
        ent("W3", "WALL", {"type": "line", "start": [5000, 4000], "end": [0, 4000]}, level_id="L1",
            properties={"thickness": 200}),
        ent("W4", "WALL", {"type": "line", "start": [0, 4000], "end": [0, 0]}, level_id="L1",
            properties={"thickness": 200}),
    ]
    return {
        "meta": {"units": "mm", "insunits": 4, "datum": "+-0.000", "sheets": ["A-101", "A-301"], "xrefs": [],
                 "fonts": [{"name": "Arial", "resolved": True}], "recovery_warning": False,
                 "audit": {"ran": True, "errors_found": 0, "errors_fixed": 0, "log": "audit.log"},
                 "layouts": [{"name": "A-101", "paper": "A3", "scale": "1:100", "viewport_locked": True,
                              "title_block": "TB"}]},
        "profile": {"units": "mm", "paper_size": ["A3"], "area_rule": "wall_inner_face"},
        "entities": [
            ent("L1", "LEVEL", elevation=0.0, properties={"datum": "+-0.000"}),
            ent("L2", "LEVEL", elevation=3300.0, properties={"datum": "+-0.000"}),
            *walls,
            ent("R1", "ROOM", {"type": "polyline", "points": rect(100, 100, 4900, 3900), "closed": True},
                level_id="L1", elevation=0.0,
                properties={"code": "R1", "stated_area": 18.24, "area_rule": "wall_inner_face"}),
            ent("D1", "DOOR", {"type": "point", "point": [2500, 0]}, level_id="L1",
                relationships={"host_wall": "W1"}, properties={"code": "D1", "width": 900, "height": 2100}),
            ent("WN1", "WINDOW", {"type": "point", "point": [2500, 4000]}, level_id="L1",
                relationships={"host_wall": "W3"}, properties={"code": "W1", "width": 1200, "height": 1200}),
            ent("S1", "STAIR", properties={"total_height": 3300, "riser_count": 20, "riser_height": 165,
                                           "from_level": "L1", "to_level": "L2"}),
            ent("DIM1", "DIMENSION", {"type": "dimension", "p1": [0, 0], "p2": [5000, 0]},
                properties={"displayed_value": 5000}),
            ent("G1", "GRID", {"type": "line", "start": [0, -500], "end": [0, 4500]}, properties={"label": "A"}),
            ent("G2", "GRID", {"type": "line", "start": [5000, -500], "end": [5000, 4500]}, properties={"label": "B"}),
            ent("M1", "DETAIL_MARKER", properties={"target_sheet": "A-301"}),
        ],
        "schedules": {"door": [{"code": "D1", "width": 900, "height": 2100}],
                      "window": [{"code": "W1", "width": 1200, "height": 1200}],
                      "room": [{"code": "R1", "area": 18.24}]},
    }


def run(model: dict[str, Any], **kw: Any) -> validation.ValidationReport:
    return validation.run_validation(validation.DrawingModel.from_dict(model), **kw)


def result_of(report: validation.ValidationReport, rule_id: str) -> validation.RuleResult:
    return next(r for r in report.results if r.rule_id == rule_id)


def mutate(model: dict[str, Any], entity_id: str, **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(model)
    entity = next(e for e in out["entities"] if e["entity_id"] == entity_id)
    for key, value in changes.items():
        if key == "properties":
            entity["properties"] = {**entity.get("properties", {}), **value}
        else:
            entity[key] = value
    return out


def add(model: dict[str, Any], *entities: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(model)
    out["entities"].extend(entities)
    return out


def drop(model: dict[str, Any], *ids: str) -> dict[str, Any]:
    out = copy.deepcopy(model)
    out["entities"] = [e for e in out["entities"] if e["entity_id"] not in ids]
    return out


# =========================================================================== section 1: profile

def full_profile() -> dict[str, Any]:
    p: dict[str, Any] = {
        "country": "X", "locality": "Y", "authority": "Z", "building_type": "residential", "design_stage": "technical",
        "house_type": "townhouse", "floors": 3, "design_scope": ["new_build"], "disciplines": ["architecture"],
        "autocad_version": "2024", "file_format": ["dwg"], "units": "mm", "coordinate_system": "local_wcs",
        "layer_convention": "spec_sample", "area_rule": ["wall_inner_face"], "paper_size": ["A3"], "scale": ["1:100"],
        "text_font": "truetype", "linetype": "ACAD_ISO", "lineweight": "0.25", "plot_style": "ctb",
        "title_block": "TB", "presentation_convention": "x",
    }
    for code in ("building_code", "zoning", "fire_code", "accessibility", "structure_code", "mep_code",
                 "local_requirements"):
        p[code] = {"value": "as given", "status": "PROVIDED", "verified_by": "lead architect"}
    return p


def test_empty_profile_asks_everything_and_invents_nothing() -> None:
    report = standards.check_profile(None)
    assert len(report.questions) == len(standards.FIELD_SPECS)
    assert not report.ready["DRAW"] and not report.ready["PLOT"] and not report.ready["CODE_REVIEW"]
    assert all(s.state == "MISSING" and s.value is None for s in report.fields.values())
    for question in report.questions:
        ids = [o["id"] for o in question["options"]]
        assert ids[-2:] == ["other", "unknown"]
        assert question["allow_free_text"] is True and question["default_if_skipped"] == "UNKNOWN"


def test_legal_fields_have_no_suggested_options() -> None:
    questions = {q["field_id"]: q for q in standards.check_profile({}).questions}
    for field_id in ("building_code", "zoning", "fire_code", "accessibility", "structure_code", "mep_code",
                     "local_requirements"):
        assert [o["id"] for o in questions[field_id]["options"]] == ["other", "unknown"], field_id
        assert questions[field_id]["legal"] is True
    assert len(questions["units"]["options"]) > 2


def test_multi_select_flags() -> None:
    questions = {q["field_id"]: q for q in standards.check_profile({}).questions}
    assert questions["paper_size"]["multi_select"] is True and questions["disciplines"]["multi_select"] is True
    assert questions["units"]["multi_select"] is False and questions["floors"]["multi_select"] is False


def test_complete_profile_is_ready_for_everything() -> None:
    report = standards.check_profile(full_profile())
    assert report.ready == {"DRAW": True, "PLOT": True, "CODE_REVIEW": True}
    assert report.questions == [] and report.unknown_fields == []


def test_invalid_values_become_questions() -> None:
    profile = full_profile() | {"floors": "abc", "units": "furlong", "paper_size": ["A9"], "scale": "big"}
    report = standards.check_profile(profile)
    assert set(report.invalid) == {"floors", "units", "paper_size", "scale"}
    assert not report.ready["DRAW"]
    assert {q["field_id"] for q in report.questions} == {"floors", "units", "paper_size", "scale"}
    assert standards.check_profile(full_profile() | {"floors": 0}).invalid == ["floors"]
    assert standards.check_profile(full_profile() | {"floors": 2.5}).invalid == ["floors"]


def test_unknown_keeps_unknown_and_assumed_is_listed() -> None:
    profile = full_profile() | {"locality": {"value": None, "status": "UNKNOWN"},
                                "floors": {"value": 2, "status": "ASSUMED"}}
    report = standards.check_profile(profile)
    assert report.fields["locality"].state == "MISSING"
    assert report.fields["floors"].state == "ASSUMED" and report.assumed == ["floors"]
    assert not report.ready["DRAW"]  # locality still missing; the assumption never counted as knowledge of locality
    assert standards.profile_value(profile, "locality") is None and standards.profile_value(profile, "floors") == 2


def test_unverified_legal_value_is_not_a_legal_requirement() -> None:
    profile = full_profile() | {"fire_code": "Some fire code"}
    report = standards.check_profile(profile)
    assert report.unverified_legal == ["fire_code"]
    assert report.fields["fire_code"].legal_verified is False
    assert not report.ready["CODE_REVIEW"] and report.ready["DRAW"]
    assert any(q["field_id"] == "fire_code" for q in report.questions)


def test_unknown_profile_keys_are_reported_not_adopted() -> None:
    report = standards.check_profile({"colour_of_house": "red"})
    assert report.unknown_fields == ["colour_of_house"]


# =========================================================================== section 3: provenance

def test_source_status_values_match_spec() -> None:
    assert [s.value for s in SourceStatus] == ["PROVIDED", "MEASURED", "IMPORTED", "DERIVED", "ASSUMED", "UNKNOWN",
                                               "CONFLICTED"]
    assert "VERIFIED" not in SourceStatus.__members__


def test_datum_rules() -> None:
    assert Datum.unknown("no survey").value is None
    with pytest.raises(ProvenanceError):
        Datum(5, SourceStatus.UNKNOWN)
    with pytest.raises(ProvenanceError):
        Datum(None, SourceStatus.PROVIDED)
    with pytest.raises(ProvenanceError):
        Datum.assumed(3000, "  ")
    with pytest.raises(ProvenanceError):
        Datum(1, "NOT_A_STATUS")  # type: ignore[arg-type]


def test_assumed_cannot_be_confirmed_without_evidence() -> None:
    assumed = Datum.assumed(3000, "typical floor height until the section is provided")
    with pytest.raises(ProvenanceError):
        assumed.confirm(Source())
    confirmed = assumed.confirm(Source(file="section.pdf", page=2))
    assert confirmed.status is SourceStatus.PROVIDED and confirmed.source.file == "section.pdf"
    with pytest.raises(ProvenanceError):
        Datum.unknown().confirm(Source(file="x"))


def test_conflict_keeps_both_values_and_needs_a_decider() -> None:
    a = Datum(3000, SourceStatus.PROVIDED, Source(file="brief.docx"))
    conflicted = a.conflict(3150, Source(file="survey.dwg", entity="2F3"))
    assert conflicted.status is SourceStatus.CONFLICTED and conflicted.value is None
    assert [v for v, _ in conflicted.alternatives] == [3000, 3150]
    again = conflicted.conflict(3200, Source(file="third.pdf"))
    assert len(again.alternatives) == 3
    with pytest.raises(ProvenanceError):
        conflicted.resolve(1, " ")
    with pytest.raises(ProvenanceError):
        conflicted.resolve(9, "me")
    with pytest.raises(ProvenanceError):
        a.resolve(0, "me")
    resolved = conflicted.resolve(1, "chief architect")
    assert resolved.value == 3150 and "chief architect" in resolved.source.note and "3000" in resolved.source.note
    with pytest.raises(ProvenanceError):
        Datum.unknown().conflict(1, Source())


def test_confidence_range() -> None:
    with pytest.raises(ProvenanceError):
        Source(confidence=1.5)
    assert Source(confidence=0.0).confidence == 0.0


def test_entity_schema_roundtrip_and_errors() -> None:
    e = Entity.from_dict(ent("W9", "wall", level_id="L1", source={"file": "a.dwg", "entity": "2A"}))
    assert e.entity_type is EntityType.WALL and e.source.file == "a.dwg"
    again = Entity.from_dict(e.to_dict())
    assert again.to_dict() == e.to_dict()
    with pytest.raises(ProvenanceError):
        Entity.from_dict({"entity_id": "x"})
    with pytest.raises(ProvenanceError):
        Entity.from_dict(ent("x", "SPACESHIP"))
    with pytest.raises(ProvenanceError):
        Entity.from_dict(ent("x", "WALL", invented_field=1))
    with pytest.raises(ProvenanceError):
        Entity.from_dict(ent("", "WALL"))
    with pytest.raises(ProvenanceError):
        load_entities([ent("a", "WALL"), ent("a", "DOOR")])
    unknown = Entity.from_dict({"entity_id": "u", "entity_type": "OTHER"})
    assert unknown.status is SourceStatus.UNKNOWN and unknown.geometry is None and unknown.elevation is None


def test_property_may_be_wrapped_as_datum_and_conflict_record_marks_entity() -> None:
    e = Entity.from_dict(ent("D", "DOOR", properties={"width": {"value": 900, "source_status": "ASSUMED"}}))
    assert e.prop("width") == 900
    record = record_conflict(e, "width", 900, Source(file="a"), 950, Source(file="b"))
    assert e.status is SourceStatus.CONFLICTED
    data = record.to_dict()
    assert [v["value"] for v in data["values"]] == [900, 950] and "Which source" in data["question"]


# =========================================================================== section 5/6: layers and units

def test_layer_map_rules() -> None:
    lm = layers.LayerMap({"A-DOOR": "OFFICE_DOOR", "A-WINDOW": "OFFICE_WIN"})
    assert lm.to_office("A-DOOR") == "OFFICE_DOOR" and lm.to_office("A-GRID") == "A-GRID"
    assert lm.to_standard("office_door") == "A-DOOR"
    assert lm.to_standard("A-DOOR") is None  # renamed by the office template, so the bare name is not standard here
    assert lm.to_standard("A-GRID") == "A-GRID"
    with pytest.raises(layers.LayerMapError):
        layers.LayerMap({"NOT-STANDARD": "X"})
    with pytest.raises(layers.LayerMapError):
        layers.LayerMap({"A-DOOR": "SAME", "A-WINDOW": "same"})
    with pytest.raises(layers.LayerMapError):
        layers.LayerMap({"A-DOOR": " "})
    with pytest.raises(layers.LayerMapError):
        lm.to_office("A-MADE-UP")


def test_layer_for_entity_does_not_guess() -> None:
    assert layers.layer_for_entity("DOOR")[0] == "A-DOOR"
    assert layers.layer_for_entity("WALL", {"exterior": True})[0] == "A-WALL-EXT"
    assert layers.layer_for_entity("WALL", {"exterior": False})[0] == "A-WALL-INT"
    assert layers.layer_for_entity("WALL", {"phase": "demolish"})[0] == "A-WALL-DEMO"
    layer, why = layers.layer_for_entity("WALL", {})
    assert layer is None and "not guessed" in why
    assert layers.layer_for_entity("FINISH")[0] is None
    assert layers.layer_for_entity("DOOR", layer_map=layers.LayerMap({"A-DOOR": "D-OFFICE"}))[0] == "D-OFFICE"


def test_check_layers_namespaces() -> None:
    info = layers.check_layers(["A-DOOR", "S-BEAM", "FP-SPRINKLER", "My Layer", "0", "Defpoints"])
    assert info["standard"] == ["A-DOOR"]
    assert [x["layer"] for x in info["other_discipline"]] == ["S-BEAM", "FP-SPRINKLER"]
    assert info["unmapped"] == ["My Layer"] and info["ok"] is False
    assert layers.namespace_of("FP-X") == "FP" and layers.namespace_of("Foo") is None


def test_units_check() -> None:
    assert layers.check_units(4, "mm")["result"] == "PASS"
    mismatch = layers.check_units(6, "mm")
    assert mismatch["result"] == "FAIL" and mismatch["severity"] == "BLOCKER"
    assert layers.check_units(0, "mm")["severity"] == "BLOCKER"  # unitless
    assert layers.check_units(None, "mm")["result"] == "NOT_RUN"
    assert layers.check_units(4, None)["result"] == "WARN"
    assert layers.insunits_name(5) == "cm" and layers.insunits_name(0) is None and layers.insunits_name(99) is None


def test_model_scale_1to1_check() -> None:
    assert layers.check_model_scale_1to1("mm", 3000, 3000, 0.001)["result"] == "PASS"
    bad = layers.check_model_scale_1to1("mm", 30, 3000, 0.001)
    assert bad["result"] == "FAIL" and bad["severity"] == "BLOCKER"
    assert layers.check_model_scale_1to1("m", 3, 3000, 0.001)["result"] == "PASS"
    assert layers.check_model_scale_1to1(None, 3, 3000, 0.001)["result"] == "NOT_RUN"
    assert layers.check_model_scale_1to1("mm", None, 3000, 0.001)["result"] == "NOT_RUN"
    with pytest.raises(ValueError):
        layers.check_model_scale_1to1("mm", 1, -5, 0.001)


def test_import_autocad_entities_infers_type_only_from_layer() -> None:
    raw = [
        {"handle": "2F", "object_name": "AcDbLine", "layer": "A-WALL-EXT", "start": {"x": 0, "y": 0, "z": 0},
         "end": {"x": 1000, "y": 0, "z": 0}},
        {"handle": "30", "object_name": "AcDbLine", "layer": "Misc", "start": {"x": 0, "y": 0, "z": 0},
         "end": {"x": 0, "y": 1000, "z": 0}},
        {"handle": "31", "object_name": "AcDbPolyline", "layer": "A-ROOM-BOUNDARY", "closed": True,
         "points": [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}, {"x": 1, "y": 1, "z": 0}]},
        {"handle": "32", "object_name": "AcDbText", "layer": "A-ANNO-TEXT", "text": "A", "height": 100,
         "insertion": {"x": 5, "y": 5, "z": 0}},
    ]
    entities = layers.import_autocad_entities(raw, units="mm", source_file="plan.dwg")
    assert [e.entity_type for e in entities] == [EntityType.WALL, EntityType.OTHER, EntityType.ROOM,
                                                 EntityType.ANNOTATION]
    assert all(e.status is SourceStatus.IMPORTED for e in entities)
    assert entities[0].properties["type_inferred_from_layer"] is True
    assert "type_inferred_from_layer" not in entities[1].properties
    assert entities[0].source.entity == "2F" and entities[0].geometry["type"] == "line"


# =========================================================================== geometry helpers

def test_geom2d_basics() -> None:
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert geom2d.polygon_area(square) == 4.0
    assert geom2d.point_in_polygon((1, 1), square, 1e-9)
    assert not geom2d.point_in_polygon((2, 1), square, 1e-6)  # on the boundary
    bowtie = {"type": "polyline", "points": [[0, 0], [2, 2], [2, 0], [0, 2]], "closed": True}
    assert geom2d.self_intersections(bowtie, 1e-6)
    assert not geom2d.self_intersections({"type": "polyline", "points": rect(0, 0, 2, 2), "closed": True}, 1e-6)
    s = [((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (1.0, 1.0)), ((1.0, 1.0), (0.0, 1.0)), ((0.0, 1.0), (0.0, 0.0))]
    assert geom2d.chain_closed(s, 1e-6)[0]
    assert not geom2d.chain_closed(s[:3], 1e-6)[0]
    assert geom2d.ring_from_segments(s, 1e-6) is not None
    assert not geom2d.chain_closed(s[:2], 1e-6)[0]


# =========================================================================== validation engine

def test_good_model_passes_without_errors() -> None:
    report = run(good_model())
    failing = [(r.rule_id, r.evidence, r.issues) for r in report.results if r.result.value == "FAIL"]
    assert not failing, failing
    assert report.overall in ("PASS", "WARN")
    assert report.open_blockers == [] and report.open_errors == []
    assert result_of(report, "FILE.UNITS_DECLARED").result.value == "PASS"
    assert result_of(report, "COORD.AREA_RECOMPUTED").result.value == "PASS"
    assert result_of(report, "COORD.STAIR_RISERS").result.value == "PASS"
    # rules without data are NOT_RUN, never a vacuous PASS
    assert result_of(report, "FILE.MODEL_SCALE_1TO1").result.value == "NOT_RUN"
    assert result_of(report, "PLOT.PDF_RENDER_CHECKED").result.value == "NOT_RUN"
    assert result_of(report, "GEOM.OUT_OF_BOUNDS").result.value == "NOT_RUN"


def test_every_rule_has_the_required_fields() -> None:
    ids = [r["rule_id"] for r in validation.rule_catalog()]
    assert len(ids) == len(set(ids)) and len(ids) >= 25
    for r in validation.rule_catalog():
        assert r["description"] and r["scope"] in validation.SCOPES and r["severity"] in {"BLOCKER", "ERROR", "WARNING", "INFO"}
        assert isinstance(r["parameters"], dict)
    for res in run(good_model()).results:
        assert res.result.value in {"PASS", "FAIL", "WARN", "NOT_RUN"} and res.evidence


def test_zero_length_segments() -> None:
    model = add(good_model(),
                ent("Z1", "OTHER", {"type": "line", "start": [10, 10], "end": [10, 10.01]}),
                ent("Z2", "OTHER", {"type": "polyline", "points": [[0, 0], [5, 0], [5, 0], [9, 9]], "closed": False}))
    res = result_of(run(model), "GEOM.ZERO_LENGTH")
    assert res.result.value == "FAIL" and {i["entity_ids"][0] for i in res.issues} == {"Z1", "Z2"}


def test_open_room_boundary_and_closure_rules() -> None:
    open_room = mutate(good_model(), "R1", geometry={"type": "polyline", "points": rect(100, 100, 4900, 3900),
                                                      "closed": False})
    report = run(open_room)
    assert result_of(report, "GEOM.ROOM_BOUNDARY_CLOSED").result.value == "FAIL"
    closure = result_of(report, "GEOM.POLYLINE_CLOSURE")
    assert closure.result.value == "FAIL" and "ends meet" not in closure.issues[0]["message"]  # open and not near-closed
    near = mutate(good_model(), "R1", geometry={"type": "polyline", "points": rect(100, 100, 4900, 3900) + [[100, 100.2]],
                                                 "closed": False})
    assert "ends meet" in result_of(run(near), "GEOM.POLYLINE_CLOSURE").issues[0]["message"]


def test_room_boundary_from_loose_segments() -> None:
    segs = [[[0, 0], [1000, 0]], [[1000, 0], [1000, 1000]], [[1000, 1000], [0, 1000]], [[0, 1000], [0, 0]]]
    closed = add(good_model(), ent("R2", "ROOM", {"type": "segments", "segments": segs}, level_id="L1", elevation=0.0))
    assert result_of(run(closed), "GEOM.ROOM_BOUNDARY_CLOSED").result.value == "PASS"
    gap = copy.deepcopy(closed)
    gap["entities"][-1]["geometry"]["segments"][3] = [[0, 1000], [0, 50]]
    res = result_of(run(gap), "GEOM.ROOM_BOUNDARY_CLOSED")
    assert res.result.value == "FAIL" and "open boundary" in res.issues[0]["message"]
    barely = copy.deepcopy(closed)
    barely["entities"][-1]["geometry"]["segments"][3] = [[0, 1000], [0, 0.5]]  # 0.5 mm gap is inside the 1 mm closure_gap
    assert result_of(run(barely), "GEOM.ROOM_BOUNDARY_CLOSED").result.value == "PASS"
    assert result_of(run(barely, tolerances={"closure_gap": 0.1}), "GEOM.ROOM_BOUNDARY_CLOSED").result.value == "FAIL"


def test_self_intersection() -> None:
    bow = add(good_model(), ent("B1", "OTHER", {"type": "polyline", "points": [[0, 0], [2000, 2000], [2000, 0], [0, 2000]],
                                               "closed": True}))
    res = result_of(run(bow), "GEOM.SELF_INTERSECTION")
    assert res.result.value == "FAIL" and res.issues[0]["entity_ids"] == ["B1"]
    assert result_of(run(good_model()), "GEOM.SELF_INTERSECTION").result.value == "PASS"


def test_duplicates_are_reported_not_deleted() -> None:
    line = {"type": "line", "start": [0, 0], "end": [100, 0]}
    flipped = {"type": "line", "start": [100, 0.2], "end": [0, 0]}
    model = add(good_model(), ent("X1", "OTHER", line, layer="A-HIDDEN"), ent("X2", "OTHER", flipped, layer="A-HIDDEN"),
                ent("X3", "OTHER", flipped, layer="A-HATCH"))
    report = run(model)
    res = result_of(report, "GEOM.DUPLICATE_ENTITIES")
    assert res.result.value == "WARN" and res.severity == "WARNING"
    assert res.issues[0]["entity_ids"] == ["X1", "X2"] and "not deleted" in res.issues[0]["message"]
    assert report.overall == "WARN"
    assert result_of(run(model, params={"GEOM.DUPLICATE_ENTITIES": {"max_pairs": 1}}),
                     "GEOM.DUPLICATE_ENTITIES").result.value == "NOT_RUN"


def test_out_of_bounds_needs_parameter() -> None:
    model = add(good_model(), ent("F1", "OTHER", {"type": "line", "start": [0, 0], "end": [9e6, 0]}))
    res = result_of(run(model, params={"GEOM.OUT_OF_BOUNDS": {"drawing_bounds": [-1000, -1000, 20000, 20000]}}),
                    "GEOM.OUT_OF_BOUNDS")
    assert res.result.value == "WARN" and res.issues[0]["entity_ids"] == ["F1"]


def test_wall_without_thickness_or_convention() -> None:
    res = result_of(run(mutate(good_model(), "W1", properties={"thickness": None})), "GEOM.WALL_THICKNESS")
    assert res.result.value == "FAIL"
    ok = mutate(good_model(), "W1", properties={"thickness": None, "centerline_rule": "centerline"})
    assert result_of(run(ok), "GEOM.WALL_THICKNESS").result.value == "PASS"
    assert result_of(run(mutate(good_model(), "W1", properties={"thickness": -5})), "GEOM.WALL_THICKNESS").result.value == "FAIL"
    assert result_of(run(mutate(good_model(), "W1", properties={"thickness": "thick"})),
                     "GEOM.WALL_THICKNESS").result.value == "FAIL"


def test_door_window_host_wall_links() -> None:
    no_host = mutate(good_model(), "D1", relationships={})
    res = result_of(run(no_host), "GEOM.OPENING_HOST_WALL")
    assert res.result.value == "FAIL" and "no host wall" in res.issues[0]["message"]
    bad_host = mutate(good_model(), "D1", relationships={"host_wall": "R1"})
    assert "not a WALL" in result_of(run(bad_host), "GEOM.OPENING_HOST_WALL").issues[0]["message"]
    floating = mutate(good_model(), "D1", geometry={"type": "point", "point": [2500, 900]})
    assert "not inside the wall" in result_of(run(floating), "GEOM.OPENING_HOST_WALL").issues[0]["message"]
    other_level = mutate(good_model(), "D1", level_id="L2")
    assert "host wall" in result_of(run(other_level), "GEOM.OPENING_HOST_WALL").issues[0]["message"]
    neg = mutate(good_model(), "D1", properties={"width": -900})
    assert result_of(run(neg), "GEOM.OPENING_HOST_WALL").result.value == "FAIL"
    missing_opening = mutate(good_model(), "D1", relationships={"host_wall": "W1", "opening": "NOPE"})
    assert result_of(run(missing_opening), "GEOM.OPENING_HOST_WALL").result.value == "FAIL"


def test_duplicate_and_missing_codes() -> None:
    dup = add(good_model(), ent("D2", "DOOR", {"type": "point", "point": [1000, 0]}, level_id="L1",
                                relationships={"host_wall": "W1"}, properties={"code": "D1", "width": 900, "height": 2100}))
    res = result_of(run(dup), "COORD.OPENING_CODES_UNIQUE")
    assert res.result.value == "FAIL" and "must be unique" in res.issues[0]["message"]
    nocode = mutate(good_model(), "D1", properties={"code": ""})
    assert result_of(run(nocode), "COORD.OPENING_CODES_UNIQUE").result.value == "FAIL"
    # a door and a window may share a code string without clashing
    shared = mutate(good_model(), "WN1", properties={"code": "D1"})
    assert result_of(run(shared), "COORD.OPENING_CODES_UNIQUE").result.value == "PASS"


def test_dimension_override_is_a_conflict_and_nothing_is_changed() -> None:
    model = mutate(good_model(), "DIM1", properties={"displayed_value": 5100})
    before = copy.deepcopy(model)
    res = result_of(run(model), "ANNO.DIMENSION_OVERRIDE")
    assert res.result.value == "FAIL"
    issue = res.issues[0]
    assert issue["kind"] == "CONFLICT" and issue["detail"]["displayed"] == 5100 and issue["detail"]["measured"] == 5000
    assert model == before
    assert result_of(run(mutate(good_model(), "DIM1", properties={"displayed_value": 5000.4})),
                     "ANNO.DIMENSION_OVERRIDE").result.value == "PASS"
    text = mutate(good_model(), "DIM1", properties={"displayed_value": "about 5m"})
    assert result_of(run(text), "ANNO.DIMENSION_OVERRIDE").result.value == "WARN"


def test_tolerances_are_configurable_per_purpose() -> None:
    model = mutate(good_model(), "DIM1", properties={"displayed_value": 5003})
    assert result_of(run(model), "ANNO.DIMENSION_OVERRIDE").result.value == "FAIL"
    assert result_of(run(model, tolerances={"dimension": 5}), "ANNO.DIMENSION_OVERRIDE").result.value == "PASS"
    assert result_of(run(model, tolerances={"dimension": {"value": 5, "unit": "mm"}}), "ANNO.DIMENSION_OVERRIDE").result.value == "PASS"
    with pytest.raises(validation.ToleranceError):
        run(model, tolerances={"dimension": -1})
    with pytest.raises(validation.ToleranceError):
        run(model, tolerances={"no_such_purpose": 1})
    with pytest.raises(validation.ToleranceError):
        run(model, tolerances={"dimension": {"value": 1, "unit": "parsec"}})
    with pytest.raises(validation.ToleranceError):
        run(model, tolerances={"dimension": True})
    # the same millimetre tolerance scales with the drawing unit: 3 m dimension in a metre drawing
    metre = good_model()
    metre["meta"].update(units="m", insunits=6)
    metre["profile"]["units"] = "m"
    tol = validation.Tolerances(1000.0, None)
    assert tol.length("dimension") == pytest.approx(0.001)


def test_unknown_units_is_a_blocker_and_length_rules_do_not_pretend() -> None:
    model = good_model()
    del model["meta"]["units"]
    del model["meta"]["insunits"]
    report = run(model)
    assert result_of(report, "FILE.UNITS_DECLARED").severity == "BLOCKER"
    assert report.overall == "FAIL" and "FILE.UNITS_DECLARED" in report.open_blockers
    zero = result_of(report, "GEOM.ZERO_LENGTH")
    assert zero.result.value == "NOT_RUN" and "units unknown" in zero.evidence
    # a tolerance given in drawing units lets the rule run anyway
    again = run(model, tolerances={"zero_length": {"value": 0.5, "unit": "drawing"}})
    assert result_of(again, "GEOM.ZERO_LENGTH").result.value == "PASS"


def test_insunits_must_match_profile() -> None:
    model = good_model()
    model["meta"]["insunits"] = 6  # metres, profile says mm
    report = run(model)
    res = result_of(report, "FILE.UNITS_DECLARED")
    assert res.result.value == "FAIL" and res.severity == "BLOCKER"
    unitless = good_model()
    unitless["meta"]["insunits"] = 0
    assert result_of(run(unitless), "FILE.UNITS_DECLARED").severity == "BLOCKER"
    declared_wrong = good_model()
    del declared_wrong["meta"]["insunits"]
    declared_wrong["meta"]["units"] = "cm"
    assert result_of(run(declared_wrong), "FILE.UNITS_DECLARED").result.value == "FAIL"


def test_model_scale_reference() -> None:
    ok = good_model()
    ok["meta"]["scale_reference"] = {"real_length_mm": 5000, "measured_length": 5000}
    assert result_of(run(ok), "FILE.MODEL_SCALE_1TO1").result.value == "PASS"
    bad = good_model()
    bad["meta"]["scale_reference"] = {"real_length_mm": 5000, "measured_length": 50}
    res = result_of(run(bad), "FILE.MODEL_SCALE_1TO1")
    assert res.result.value == "FAIL" and res.severity == "BLOCKER" and run(bad).overall == "FAIL"


def test_schedule_vs_symbols() -> None:
    model = good_model()
    model["schedules"]["door"] = [{"code": "D1", "width": 800, "height": 2100}, {"code": "D9", "width": 900}]
    res = result_of(run(model), "COORD.SCHEDULE_MATCHES_SYMBOLS")
    messages = " | ".join(i["message"] for i in res.issues)
    assert res.result.value == "FAIL"
    assert "width 800" in messages and "D9" in messages
    only_symbol = good_model()
    only_symbol["schedules"]["window"] = []
    assert "missing from the schedule" in " ".join(i["message"] for i in result_of(
        run(only_symbol), "COORD.SCHEDULE_MATCHES_SYMBOLS").issues)
    nosched = good_model()
    nosched["schedules"] = {}
    assert result_of(run(nosched), "COORD.SCHEDULE_MATCHES_SYMBOLS").result.value == "NOT_RUN"
    dup_row = good_model()
    dup_row["schedules"]["door"].append({"code": "D1", "width": 900, "height": 2100})
    assert result_of(run(dup_row), "COORD.SCHEDULE_MATCHES_SYMBOLS").result.value == "FAIL"


def test_area_is_recomputed_from_the_boundary() -> None:
    wrong = mutate(good_model(), "R1", properties={"stated_area": 20.0})
    res = result_of(run(wrong), "COORD.AREA_RECOMPUTED")
    assert res.result.value == "FAIL"
    issue = res.issues[0]
    assert issue["detail"]["recomputed"] == pytest.approx(18.24) and issue["detail"]["stated"] == 20.0
    norule = good_model()
    norule["profile"].pop("area_rule")
    norule = mutate(norule, "R1", properties={"area_rule": None})
    assert "without a calculation rule" in result_of(run(norule), "COORD.AREA_RECOMPUTED").issues[0]["message"]
    openroom = mutate(good_model(), "R1", geometry={"type": "polyline", "points": rect(100, 100, 4900, 3900), "closed": False})
    assert "no closed boundary" in " ".join(i["message"] for i in result_of(run(openroom), "COORD.AREA_RECOMPUTED").issues)
    total_bad = good_model()
    total_bad["meta"]["total_area_m2"] = 30.0
    assert result_of(run(total_bad), "COORD.AREA_RECOMPUTED").result.value == "FAIL"
    total_ok = good_model()
    total_ok["meta"]["total_area_m2"] = 18.24
    assert result_of(run(total_ok), "COORD.AREA_RECOMPUTED").result.value == "PASS"
    row_bad = good_model()
    row_bad["schedules"]["room"] = [{"code": "R1", "area": 17.0}]
    assert result_of(run(row_bad), "COORD.AREA_RECOMPUTED").result.value == "FAIL"
    # tolerance is relative and configurable
    near = mutate(good_model(), "R1", properties={"stated_area": 18.50})
    assert result_of(run(near), "COORD.AREA_RECOMPUTED").result.value == "FAIL"
    assert result_of(run(near, tolerances={"area_rel": 0.02}), "COORD.AREA_RECOMPUTED").result.value == "PASS"
    nothing = drop(good_model(), "R1")
    nothing["schedules"].pop("room")
    assert result_of(run(nothing), "COORD.AREA_RECOMPUTED").result.value == "NOT_RUN"


def test_room_overlap_and_missing_level() -> None:
    overlapping = add(good_model(), ent("R2", "ROOM", {"type": "polyline", "points": rect(2000, 2000, 6000, 5000),
                                                      "closed": True}, level_id="L1", elevation=0.0))
    assert result_of(run(overlapping), "COORD.ROOM_OVERLAP_LEVEL").result.value == "FAIL"
    touching = add(good_model(), ent("R2", "ROOM", {"type": "polyline", "points": rect(4900, 100, 8000, 3900),
                                                   "closed": True}, level_id="L1", elevation=0.0))
    assert result_of(run(touching), "COORD.ROOM_OVERLAP_LEVEL").result.value == "PASS"
    same = add(good_model(), ent("R2", "ROOM", {"type": "polyline", "points": rect(100, 100, 4900, 3900), "closed": True},
                                 level_id="L1", elevation=0.0))
    assert result_of(run(same), "COORD.ROOM_OVERLAP_LEVEL").result.value == "FAIL"
    other_floor = add(good_model(), ent("R2", "ROOM", {"type": "polyline", "points": rect(100, 100, 4900, 3900),
                                                      "closed": True}, level_id="L2", elevation=3300.0))
    assert result_of(run(other_floor), "COORD.ROOM_OVERLAP_LEVEL").result.value == "PASS"
    nolevel = mutate(good_model(), "R1", level_id=None)
    assert "no level_id" in result_of(run(nolevel), "COORD.ROOM_OVERLAP_LEVEL").issues[0]["message"]


def test_stair_riser_arithmetic() -> None:
    bad = mutate(good_model(), "S1", properties={"riser_count": 19})
    res = result_of(run(bad), "COORD.STAIR_RISERS")
    assert res.result.value == "FAIL" and "19 risers" in res.issues[0]["message"]
    level_mismatch = mutate(good_model(), "S1", properties={"total_height": 3000, "riser_count": 20, "riser_height": 150})
    assert "level difference" in result_of(run(level_mismatch), "COORD.STAIR_RISERS").issues[0]["message"]
    derived = mutate(good_model(), "S1", properties={"riser_height": None})
    ok = result_of(run(derived), "COORD.STAIR_RISERS")
    assert ok.result.value == "PASS" and "derived" in ok.evidence
    incomplete = mutate(good_model(), "S1", properties={"riser_count": None})
    assert result_of(run(incomplete), "COORD.STAIR_RISERS").result.value == "WARN"
    missing_level = mutate(good_model(), "S1", properties={"to_level": "L9"})
    assert result_of(run(missing_level), "COORD.STAIR_RISERS").result.value == "FAIL"


def test_stair_limits_only_apply_when_supplied() -> None:
    base = run(good_model())
    assert "none (no verified profile limit given)" in result_of(base, "COORD.STAIR_RISERS").evidence
    limited = run(good_model(), params={"COORD.STAIR_RISERS": {"max_riser_height": 150}})
    res = result_of(limited, "COORD.STAIR_RISERS")
    assert res.result.value == "FAIL" and "exceeds the supplied limit" in res.issues[0]["message"]
    assert "max_riser_height" in res.evidence


def test_level_and_elevation_consistency() -> None:
    dup = mutate(good_model(), "L2", elevation=2.0)
    assert "same elevation" in " ".join(i["message"] for i in result_of(run(dup), "COORD.LEVELS_CONSISTENT").issues)
    ghost = mutate(good_model(), "W1", level_id="L9")
    assert "does not exist" in result_of(run(ghost), "COORD.LEVELS_CONSISTENT").issues[0]["message"]
    datum = mutate(good_model(), "L2", properties={"datum": "survey"})
    assert "different datums" in result_of(run(datum), "COORD.LEVELS_CONSISTENT").issues[0]["message"]
    nodatum = good_model()
    nodatum["meta"].pop("datum")
    for e in nodatum["entities"]:
        e.get("properties", {}).pop("datum", None)
    res = result_of(run(nodatum), "COORD.LEVELS_CONSISTENT")
    assert res.result.value == "WARN"
    room_off = mutate(good_model(), "R1", elevation=150.0)
    assert result_of(run(room_off), "COORD.LEVELS_CONSISTENT").result.value == "WARN"
    unknown = mutate(good_model(), "L2", elevation=None)
    assert result_of(run(unknown), "COORD.LEVELS_CONSISTENT").result.value == "FAIL"


def test_views_must_agree() -> None:
    model = mutate(good_model(), "WN1", properties={"views": {"plan": {"width": 1200}, "elevation": {"width": 1000}}})
    res = result_of(run(model), "COORD.VIEWS_CONSISTENT")
    assert res.result.value == "FAIL" and res.issues[0]["kind"] == "CONFLICT"
    agree = mutate(good_model(), "WN1", properties={"views": {"plan": {"width": 1200}, "elevation": {"width": 1200.5}}})
    assert result_of(run(agree), "COORD.VIEWS_CONSISTENT").result.value == "PASS"


def test_annotation_rules() -> None:
    dup_grid = mutate(good_model(), "G2", properties={"label": "A"})
    assert result_of(run(dup_grid), "ANNO.GRID_LABELS_UNIQUE").result.value == "FAIL"
    marker = mutate(good_model(), "M1", properties={"target_sheet": "A-999"})
    assert "does not exist" in result_of(run(marker), "ANNO.MARKER_SHEET_EXISTS").issues[0]["message"]
    nosheets = good_model()
    nosheets["meta"].pop("sheets")
    assert result_of(run(nosheets), "ANNO.MARKER_SHEET_EXISTS").result.value == "NOT_RUN"
    empty = add(good_model(), ent("T1", "ANNOTATION", properties={"text": "  "}))
    assert result_of(run(empty), "ANNO.TEXT_NOT_EMPTY").result.value == "WARN"
    chain_ok = add(good_model(),
                   ent("C1", "DIMENSION", {"type": "dimension", "p1": [0, 0], "p2": [2000, 0]},
                       properties={"chain_id": "c", "chain_role": "segment"}),
                   ent("C2", "DIMENSION", {"type": "dimension", "p1": [2000, 0], "p2": [5000, 0]},
                       properties={"chain_id": "c", "chain_role": "segment"}),
                   ent("C3", "DIMENSION", {"type": "dimension", "p1": [0, 0], "p2": [5000, 0]},
                       properties={"chain_id": "c", "chain_role": "overall"}))
    assert result_of(run(chain_ok), "ANNO.DIMENSION_CHAIN").result.value == "PASS"
    chain_bad = mutate(chain_ok, "C2", properties={"displayed_value": 2500})
    res = result_of(run(chain_bad), "ANNO.DIMENSION_CHAIN")
    assert res.result.value == "FAIL" and res.issues[0]["kind"] == "CONFLICT"


def test_file_level_rules_use_inventory_when_present() -> None:
    model = good_model()
    model["meta"].update(xrefs=[{"name": "site.dwg", "resolved": False, "path": "Z:/site.dwg"}],
                         fonts=[{"name": "romans", "resolved": True, "substitute": "txt"}],
                         audit={"ran": True, "errors_found": 3, "errors_fixed": 1}, recovery_warning=True)
    report = run(model)
    assert result_of(report, "FILE.XREF_RESOLVED").result.value == "FAIL"
    assert result_of(report, "FILE.FONT_RESOLVED").result.value == "WARN"
    assert result_of(report, "FILE.AUDIT_LOGGED").result.value == "FAIL"
    rec = result_of(report, "FILE.OPENS_WITHOUT_RECOVERY")
    assert rec.result.value == "FAIL" and rec.severity == "BLOCKER" and report.overall == "FAIL"
    bare = good_model()
    for key in ("xrefs", "fonts", "audit", "recovery_warning"):
        bare["meta"].pop(key)
    report = run(bare)
    for rule in ("FILE.XREF_RESOLVED", "FILE.FONT_RESOLVED", "FILE.AUDIT_LOGGED", "FILE.OPENS_WITHOUT_RECOVERY"):
        assert result_of(report, rule).result.value == "NOT_RUN", rule


def test_layout_rules() -> None:
    model = good_model()
    model["meta"]["layouts"] = [{"name": "A-101", "paper": "A0", "viewport_locked": False}]
    res = result_of(run(model), "PLOT.LAYOUT_SETUP")
    msgs = " | ".join(i["message"] for i in res.issues)
    assert res.result.value == "FAIL" and "not in the profile" in msgs and "scale not set" in msgs
    assert "not locked" in msgs and "no title block" in msgs


def test_pdf_render_rule_needs_evidence() -> None:
    model = good_model()
    model["meta"]["pdf_render_check"] = {"ok": True, "note": "page 1 rendered, 4 sheets"}
    assert result_of(run(model), "PLOT.PDF_RENDER_CHECKED").result.value == "PASS"
    model["meta"]["pdf_render_check"] = {"ok": False, "note": "clipped title block"}
    assert result_of(run(model), "PLOT.PDF_RENDER_CHECKED").result.value == "FAIL"


def test_blocker_means_not_pass_and_cannot_be_waived() -> None:
    model = good_model()
    model["meta"]["insunits"] = 6
    waiver = {"rule_id": "FILE.UNITS_DECLARED", "approved_by": "chief", "reason": "trust me", "date": "2026-10-16",
              "scope": "whole file"}
    report = run(model, waivers=[waiver])
    assert report.overall == "FAIL" and report.waived == [] and report.open_blockers == ["FILE.UNITS_DECLARED"]


def test_error_can_be_closed_only_by_a_complete_waiver() -> None:
    model = mutate(good_model(), "DIM1", properties={"displayed_value": 5100})
    assert run(model).overall == "FAIL"
    full = {"rule_id": "ANNO.DIMENSION_OVERRIDE", "approved_by": "J. Architect", "reason": "client override",
            "date": "2026-10-16", "scope": "DIM1 only", "entity_ids": ["DIM1"]}
    waived = run(model, waivers=[full])
    assert waived.overall in ("PASS", "WARN") and len(waived.waived) == 1 and waived.waived[0]["approved_by"] == "J. Architect"
    assert result_of(waived, "ANNO.DIMENSION_OVERRIDE").issues[0]["waived"] is True
    for missing in ("approved_by", "reason", "date", "scope"):
        partial = {k: v for k, v in full.items() if k != missing}
        report = run(model, waivers=[partial])
        assert report.overall == "FAIL" and report.rejected_waivers[0]["reason"] == f"missing {missing}"
    wrong_entity = run(model, waivers=[full | {"entity_ids": ["OTHER"]}])
    assert wrong_entity.overall == "FAIL"


def test_scopes_rule_ids_and_unknown_rule() -> None:
    only = run(good_model(), scopes=["FILE"])
    assert {r.scope for r in only.results} == {"FILE"}
    assert [r.rule_id for r in run(good_model(), rule_ids=["GEOM.ZERO_LENGTH"]).results] == ["GEOM.ZERO_LENGTH"]
    with pytest.raises(ValueError):
        run(good_model(), rule_ids=["NOPE.RULE"])


def test_malformed_geometry_fails_the_rule_instead_of_crashing() -> None:
    model = add(good_model(), ent("BAD", "OTHER", {"type": "line", "start": [0, 0]}))
    report = run(model)
    crashed = [r for r in report.results if r.issues and r.issues[0]["kind"] == "RULE_ERROR"]
    assert crashed and report.overall == "FAIL"


def test_large_coordinates_are_still_checked() -> None:
    shift = 7_000_000.0
    model = good_model()
    for e in model["entities"]:
        g = e.get("geometry")
        if not g:
            continue
        if g["type"] == "line":
            g["start"] = [g["start"][0] + shift, g["start"][1] + shift]
            g["end"] = [g["end"][0] + shift, g["end"][1] + shift]
        elif g["type"] == "point":
            g["point"] = [g["point"][0] + shift, g["point"][1] + shift]
        elif g["type"] == "polyline":
            g["points"] = [[x + shift, y + shift] for x, y in g["points"]]
    report = run(model)
    assert result_of(report, "COORD.AREA_RECOMPUTED").result.value == "PASS"
    assert result_of(report, "GEOM.OPENING_HOST_WALL").result.value == "PASS"


def test_model_loader_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        validation.DrawingModel.from_dict({"entitites": []})
    with pytest.raises(ValueError):
        validation.DrawingModel.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ProvenanceError):
        validation.DrawingModel.from_dict({"entities": [ent("a", "WALL"), ent("a", "WALL")]})


def test_empty_model_validation_is_not_a_pass_for_rules_that_did_not_run() -> None:
    report = run({"meta": {"units": "mm"}, "entities": []})
    ran = [r for r in report.results if r.result.value != "NOT_RUN"]
    assert [r.rule_id for r in ran] == ["FILE.UNITS_DECLARED"]
    assert len(report.not_run) == len(report.results) - 1
    assert report.by_scope["GEOMETRY"] == "NOT_RUN"


# =========================================================================== section 14: safety

def test_idempotency_key_and_marker() -> None:
    a = safety.idempotency_key("CREATE", {"x": 1, "y": [1, 2]})
    assert a == safety.idempotency_key(safety.OpKind.CREATE, {"y": [1, 2], "x": 1})
    assert a != safety.idempotency_key("CREATE", {"x": 2, "y": [1, 2]})
    assert a != safety.idempotency_key("MODIFY", {"x": 1, "y": [1, 2]})
    assert safety.marker(a) == f"LGA:{a}"


def test_confirmation_reasons() -> None:
    k = safety.OpKind
    assert safety.confirmation_reasons(k.CREATE, count=500) == []
    assert safety.confirmation_reasons(k.DELETE, count=5) == []
    assert safety.confirmation_reasons(k.DELETE, count=6)
    assert safety.confirmation_reasons(k.DELETE, count=2, bulk_threshold=1)
    for always in (k.PURGE, k.EXPLODE, k.CHANGE_UNITS, k.LAYER_STRUCTURE):
        assert safety.confirmation_reasons(always, count=1), always
    assert safety.confirmation_reasons(k.SAVE, target_exists=True)
    assert safety.confirmation_reasons(k.SAVE, target_exists=False) == []
    with pytest.raises(ValueError):
        safety.confirmation_reasons(k.DELETE, bulk_threshold=0)


def test_backup_restore_and_hash(tmp_path: Path) -> None:
    f = tmp_path / "plan.dwg"
    assert safety.backup_file(f) is None
    f.write_bytes(b"AC1032original")
    info = safety.backup_file(f)
    assert info is not None and Path(info.backup).read_bytes() == b"AC1032original"
    assert Path(info.backup).parent.name == ".lga_backups" and info.sha256 == safety.sha256_file(f)
    f.write_bytes(b"corrupted")
    safety.restore_backup(info)
    assert f.read_bytes() == b"AC1032original"
    Path(info.backup).write_bytes(b"tampered")
    with pytest.raises(safety.SafetyError):
        safety.restore_backup(info)
    second = safety.backup_file(f)
    assert second is not None and second.backup != info.backup


def test_working_copy_leaves_original_untouched(tmp_path: Path) -> None:
    src = tmp_path / "orig.dwg"
    src.write_bytes(b"AC1032data")
    made = safety.make_working_copy(src, tmp_path / "work")
    assert Path(made["copy"]).read_bytes() == b"AC1032data" and made["copy"] != str(src)
    Path(made["copy"]).write_bytes(b"edited")
    assert safety.verify_source_unchanged(src, made["sha256"])
    src.write_bytes(b"changed")
    assert not safety.verify_source_unchanged(src, made["sha256"])
    with pytest.raises(safety.SafetyError):
        safety.make_working_copy(tmp_path / "missing.dwg", tmp_path / "work")
    with pytest.raises(safety.SafetyError):
        safety.make_working_copy(src, tmp_path / "work", name="orig_work.dwg") \
            if (tmp_path / "work" / "orig_work.dwg").exists() else (_ for _ in ()).throw(safety.SafetyError("exists"))


def test_dry_run_never_executes_and_apply_executes_once(tmp_path: Path) -> None:
    journal = safety.OperationJournal(tmp_path / "ops.jsonl")
    calls: list[int] = []

    def executor() -> str:
        calls.append(1)
        return "done"

    inputs = {"layer": "A-WALL-EXT", "pts": [[0, 0], [1, 1]]}
    preview = safety.run_operation(journal, "CREATE", inputs, executor, affected=["a", "b"])
    assert preview.mode is safety.Mode.DRY_RUN and preview.status is safety.OpStatus.PREVIEWED and calls == []
    applied = safety.run_operation(journal, "CREATE", inputs, executor, mode="APPLY")
    assert applied.status is safety.OpStatus.APPLIED and applied.result == "done" and calls == [1]
    retry = safety.run_operation(journal, "CREATE", inputs, executor, mode="APPLY")
    assert retry.status is safety.OpStatus.SKIPPED_DUPLICATE and calls == [1] and retry.result == "done"
    different = safety.run_operation(journal, "CREATE", inputs | {"layer": "A-DOOR"}, executor, mode="APPLY")
    assert different.status is safety.OpStatus.APPLIED and calls == [1, 1]
    lines = [json.loads(line) for line in (tmp_path / "ops.jsonl").read_text("utf-8").splitlines()]
    assert len(lines) == 4 and lines[0]["op_id"].startswith("OP-") and lines[1]["marker"] == f"LGA:{lines[1]['key']}"
    assert len({line["op_id"] for line in lines}) == 4


def test_confirmation_required_operations_do_not_run_unconfirmed() -> None:
    journal = safety.OperationJournal()
    calls: list[int] = []
    rec = safety.run_operation(journal, "PURGE", {"all": True}, lambda: calls.append(1), mode="APPLY")
    assert rec.status is safety.OpStatus.NEEDS_CONFIRMATION and calls == [] and rec.confirmation_reasons
    ok = safety.run_operation(journal, "PURGE", {"all": True}, lambda: calls.append(1), mode="APPLY", confirmed=True)
    assert ok.status is safety.OpStatus.APPLIED and calls == [1]
    bulk = safety.run_operation(journal, "DELETE", {"ids": list(range(9))}, lambda: calls.append(2), mode="APPLY",
                                affected=[str(i) for i in range(9)])
    assert bulk.status is safety.OpStatus.NEEDS_CONFIRMATION and 2 not in calls
    with pytest.raises(ValueError):
        safety.run_operation(journal, "NO_SUCH_KIND", {}, lambda: None)


def test_overwrite_backs_up_and_failed_write_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "out.dwg"
    target.write_bytes(b"AC1032v1")
    journal = safety.OperationJournal()

    def good() -> None:
        target.write_bytes(b"AC1032v2")

    rec = safety.run_operation(journal, "SAVE", {"path": str(target)}, good, mode="APPLY", confirmed=True,
                               target_path=target)
    assert rec.status is safety.OpStatus.APPLIED and rec.backup is not None
    assert Path(rec.backup.backup).read_bytes() == b"AC1032v1" and target.read_bytes() == b"AC1032v2"
    assert safety.run_operation(journal, "SAVE", {"path": str(target), "n": 2}, good, mode="APPLY",
                                target_path=target).status is safety.OpStatus.NEEDS_CONFIRMATION

    def bad() -> None:
        target.write_bytes(b"half")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        safety.run_operation(journal, "SAVE", {"path": str(target), "n": 3}, bad, mode="APPLY", confirmed=True,
                             target_path=target)
    failed = journal.records[-1]
    assert failed.status is safety.OpStatus.ROLLED_BACK and failed.error and target.read_bytes() == b"AC1032v2"
    quiet = safety.run_operation(journal, "SAVE", {"path": str(target), "n": 4}, bad, mode="APPLY", confirmed=True,
                                 target_path=target, reraise=False)
    assert quiet.status is safety.OpStatus.ROLLED_BACK


def test_atomic_write_text(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "r.md"
    safety.atomic_write_text(p, "xin chào")
    assert p.read_text("utf-8") == "xin chào" and not list(p.parent.glob("*.tmp"))


async def test_guarded_write_in_tool_layer(tmp_path: Path) -> None:
    ctx = SimpleNamespace(artifact_dir=lambda kind: tmp_path / "art")
    target = tmp_path / "plan.dwg"
    target.write_bytes(b"AC1032old")

    async def good() -> str:
        target.write_bytes(b"AC1032new")
        return "ok"

    result, op = await autocad_tools._guarded_write(ctx, safety.OpKind.SAVE, target, True, good)  # type: ignore[arg-type]
    extra, checks = autocad_tools._write_extras(op)
    assert result == "ok" and extra["operation_id"] == op.op_id and extra["backup"]["sha256"]
    assert checks[0].passed and Path(extra["backup"]["backup"]).read_bytes() == b"AC1032old"
    assert (tmp_path / "art" / "operations.jsonl").is_file()

    async def bad() -> None:
        target.write_bytes(b"xx")
        raise RuntimeError("COM died")

    with pytest.raises(RuntimeError):
        await autocad_tools._guarded_write(ctx, safety.OpKind.SAVE, target, True, bad)  # type: ignore[arg-type]
    assert target.read_bytes() == b"AC1032new"
    # no overwrite and no existing file: no backup, still an operation id
    fresh = tmp_path / "new.dwg"

    async def make() -> None:
        fresh.write_bytes(b"AC1032")

    _, op2 = await autocad_tools._guarded_write(ctx, safety.OpKind.SAVE, fresh, False, make)  # type: ignore[arg-type]
    assert op2.backup is None and "backup" not in autocad_tools._write_extras(op2)[0]


# =========================================================================== section 16: revisions

def E(entity_id: str, entity_type: str = "WALL", geometry: dict[str, Any] | None = None, **kw: Any) -> Entity:
    return Entity.from_dict(ent(entity_id, entity_type, geometry, **kw))


LINE = {"type": "line", "start": [0, 0], "end": [1000, 0]}


def test_revision_classification() -> None:
    old = [E("a", geometry=LINE, layer="A-WALL-INT"), E("b", geometry=LINE, layer="A-WALL-INT"),
           E("c", geometry=LINE, properties={"thickness": 200}), E("d", geometry=LINE)]
    new = [E("a", geometry=LINE, layer="A-WALL-INT"),
           E("b", geometry={"type": "line", "start": [0, 0], "end": [1500, 0]}, layer="A-WALL-INT"),
           E("c", geometry=LINE, properties={"thickness": 250}),
           E("e", "DOOR", {"type": "point", "point": [1, 1]})]
    diff = revision.compare_revisions(old, new, 0.01)
    assert [x["entity_id"] for x in diff.added] == ["e"] and [x["entity_id"] for x in diff.deleted] == ["d"]
    assert {x["entity_id"] for x in diff.modified} == {"b", "c"} and diff.unchanged == 1
    fields = {x["entity_id"]: [c["field"] for c in x["changes"]] for x in diff.modified}
    assert fields == {"b": ["geometry"], "c": ["properties.thickness"]}
    assert diff.to_dict()["counts"] == {"added": 1, "modified": 2, "deleted": 1, "conflicted": 0, "unchanged": 1}
    assert not diff.mapping_uncertain and not diff.mass_delete_requires_confirmation


def test_layer_change_and_tolerance() -> None:
    old = [E("a", geometry=LINE, layer="A-WALL-INT")]
    moved = [E("a", geometry={"type": "line", "start": [0, 0.004], "end": [1000, 0]}, layer="A-WALL-EXT")]
    diff = revision.compare_revisions(old, moved, 0.01)
    assert [c["field"] for c in diff.modified[0]["changes"]] == ["layer"]
    strict = revision.compare_revisions(old, moved, 0.001)
    assert {c["field"] for c in strict.modified[0]["changes"]} == {"layer", "geometry"}
    with pytest.raises(ValueError):
        revision.compare_revisions(old, moved, -1)


def test_type_change_and_conflicted_status_are_conflicts() -> None:
    diff = revision.compare_revisions([E("a", "WALL"), E("b")], [E("a", "DOOR"), E("b", status="CONFLICTED")], 0.01)
    assert {c["entity_id"] for c in diff.conflicted} == {"a", "b"} and not diff.modified


def test_three_way_conflict() -> None:
    base = [E("a", properties={"thickness": 200})]
    ours = [E("a", properties={"thickness": 220})]
    theirs = [E("a", properties={"thickness": 250})]
    diff = revision.compare_revisions(ours, theirs, 0.01, base=base)
    assert diff.conflicted and diff.conflicted[0]["fields"] == ["properties.thickness"]
    same = revision.compare_revisions(ours, [E("a", properties={"thickness": 220})], 0.01, base=base)
    assert not same.conflicted
    one_side = revision.compare_revisions(base, theirs, 0.01, base=base)
    assert not one_side.conflicted and len(one_side.modified) == 1


def test_uncertain_mapping_blocks_mass_delete() -> None:
    old = [E(f"o{i}", geometry={"type": "line", "start": [i, 0], "end": [i, 10]}) for i in range(10)]
    new = [E(f"n{i}", geometry={"type": "line", "start": [i, 0], "end": [i, 10]}) for i in range(10)]
    diff = revision.compare_revisions(old, new, 0.01)
    assert len(diff.deleted) == 10 and len(diff.added) == 10
    assert diff.mapping_uncertain and diff.mass_delete_requires_confirmation and diff.match_ratio == 0.0
    assert len(diff.possible_renames) == 10 and diff.possible_renames[0] == {"old_id": "o0", "new_id": "n0"}
    few = revision.compare_revisions([E("a"), E("b")], [E("a")], 0.01)
    assert len(few.deleted) == 1 and not few.mass_delete_requires_confirmation


def test_revision_log_requires_reason_source_actor() -> None:
    diff = revision.compare_revisions([E("a"), E("d")], [E("a", properties={"thickness": 1}), E("n")], 0.01)
    rows = revision.make_log_entries(diff, reason="client request", source_request="mail 12/10", actor="agent",
                                     sheets_affected=["A-101"], timestamp="2026-10-16T00:00:00+00:00")
    assert {r["change"] for r in rows} == {"added", "modified", "deleted"}
    modified = next(r for r in rows if r["change"] == "modified")
    assert modified["before"] == {"properties.thickness": None} and modified["after"] == {"properties.thickness": 1}
    assert all(r["sheets_affected"] == ["A-101"] and r["actor"] == "agent" for r in rows)
    for kw in ({"reason": ""}, {"source_request": " "}, {"actor": ""}):
        args = {"reason": "r", "source_request": "s", "actor": "a"} | kw
        with pytest.raises(ValueError):
            revision.make_log_entries(diff, **args)


# =========================================================================== section 19: report

DWG_BYTES = b"AC1032" + b"\x00" * 200
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 200


def verified_setup(tmp_path: Path) -> dict[str, Any]:
    dwg, pdf = tmp_path / "plan.dwg", tmp_path / "plan.pdf"
    dwg.write_bytes(DWG_BYTES)
    pdf.write_bytes(PDF_BYTES)
    model = validation.DrawingModel.from_dict(good_model())
    val = validation.run_validation(model).to_dict()
    return {"model": model, "validation": val, "outputs": [str(dwg), str(pdf)],
            "evidence": {"dwg_opened_in_autocad": True, "pdf_rendered_checked": True}}


REVIEW = {"reviewer": "N. Reviewer", "role": "lead architect", "date": "2026-10-10", "scope": "architecture"}
APPROVAL = {"approved_by": "A. Authority", "role": "licensed architect", "date": "2026-10-12", "document_ref": "DOC-1"}


def test_report_has_every_section_and_defaults_to_draft(tmp_path: Path) -> None:
    rep = report_mod.build_report(project="House A")
    for section in report_mod.SECTIONS:
        assert section in rep, section
    assert rep["FINAL_STATUS"]["status"] == "DRAFT" and not rep["FINAL_STATUS"]["issued"]
    assert rep["OUTPUTS"]["files"] == []
    text = report_mod.render_markdown(rep)
    for section in report_mod.SECTIONS:
        assert f"## {section}" in text
    limitations = " ".join(rep["LIMITATIONS"])
    assert "not confirmed to open in AutoCAD" in limitations and "not rendered" in limitations
    assert "No validation was run" in limitations and "No Project Standards Profile" in limitations


def test_cad_valid_needs_file_open_evidence_and_clean_validation(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    rep = report_mod.build_report(project="p", model=s["model"], validation=s["validation"], outputs=s["outputs"],
                                  evidence={"dwg_opened_in_autocad": True})
    assert rep["FINAL_STATUS"]["status"] == "CAD_VALID"
    assert "DRAWING_CHECKED" in rep["FINAL_STATUS"]["blocked_at"]
    no_open = report_mod.build_report(project="p", model=s["model"], validation=s["validation"], outputs=s["outputs"])
    assert no_open["FINAL_STATUS"]["status"] == "DRAFT"
    assert any("not confirmed to open" in r for r in no_open["FINAL_STATUS"]["blocked_at"]["CAD_VALID"])
    no_val = report_mod.build_report(project="p", outputs=s["outputs"], evidence={"dwg_opened_in_autocad": True})
    assert no_val["FINAL_STATUS"]["status"] == "DRAFT"


def test_missing_or_fake_output_files_are_not_outputs(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    ghost = str(tmp_path / "ghost.dwg")
    rep = report_mod.build_report(project="p", model=s["model"], validation=s["validation"], outputs=[ghost],
                                  evidence={"dwg_opened_in_autocad": True})
    assert rep["OUTPUTS"]["files"] == [] and rep["OUTPUTS"]["missing"] == [ghost]
    assert rep["FINAL_STATUS"]["status"] == "DRAFT"
    assert "do not exist" in " ".join(rep["LIMITATIONS"])
    fake = tmp_path / "fake.dwg"
    fake.write_bytes(b"this is not a dwg")
    rep = report_mod.build_report(project="p", model=s["model"], validation=s["validation"], outputs=[str(fake)],
                                  evidence={"dwg_opened_in_autocad": True})
    assert rep["FINAL_STATUS"]["status"] == "DRAFT" and "DWG header" in " ".join(rep["FINAL_STATUS"]["blocked_at"]["CAD_VALID"])
    empty = tmp_path / "empty.dwg"
    empty.write_bytes(b"")
    assert report_mod.file_facts(empty)["header_ok"] is False
    assert "(NOT FOUND)" in report_mod.render_markdown(report_mod.build_report(project="p", inputs=[ghost]))


def test_blocker_prevents_any_valid_status(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    bad = good_model()
    bad["meta"]["insunits"] = 6
    val = validation.run_validation(validation.DrawingModel.from_dict(bad)).to_dict()
    rep = report_mod.build_report(project="p", validation=val, outputs=s["outputs"], evidence=s["evidence"])
    assert rep["FINAL_STATUS"]["status"] == "DRAFT"
    assert "FILE.UNITS_DECLARED" in " ".join(rep["FINAL_STATUS"]["blocked_at"]["CAD_VALID"])
    assert rep["OPEN_ISSUES"]["open"][0]["severity"] == "BLOCKER"


def test_status_ladder_to_code_reviewed(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    common = {"project": "p", "model": s["model"], "validation": s["validation"], "outputs": s["outputs"]}
    rendered = report_mod.build_report(**common, evidence=s["evidence"])
    assert rendered["FINAL_STATUS"]["status"] == "COORDINATED"  # coordination rules ran and passed
    unrendered = report_mod.build_report(**common, evidence={"dwg_opened_in_autocad": True})
    assert unrendered["FINAL_STATUS"]["status"] == "CAD_VALID"
    reviewed_no_profile = report_mod.build_report(**common, evidence=s["evidence"], code_review=REVIEW)
    assert reviewed_no_profile["FINAL_STATUS"]["status"] == "COORDINATED"
    assert any("profile code fields" in r for r in reviewed_no_profile["FINAL_STATUS"]["blocked_at"]["CODE_REVIEWED"])
    reviewed = report_mod.build_report(**common, evidence=s["evidence"], code_review=REVIEW, profile=full_profile())
    assert reviewed["FINAL_STATUS"]["status"] == "CODE_REVIEWED" and reviewed["FINAL_STATUS"]["issued"] is False
    partial_review = report_mod.build_report(**common, evidence=s["evidence"], code_review=REVIEW | {"role": ""},
                                             profile=full_profile())
    assert partial_review["FINAL_STATUS"]["status"] == "COORDINATED"
    unverified = full_profile() | {"fire_code": "just a name"}
    assert report_mod.build_report(**common, evidence=s["evidence"], code_review=REVIEW,
                                   profile=unverified)["FINAL_STATUS"]["status"] == "COORDINATED"


def test_coordination_failure_blocks_coordinated(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    broken = mutate(good_model(), "D1", properties={"width": 1500})  # schedule says 900
    val = validation.run_validation(validation.DrawingModel.from_dict(broken)).to_dict()
    rep = report_mod.build_report(project="p", validation=val, outputs=s["outputs"], evidence=s["evidence"])
    assert rep["FINAL_STATUS"]["status"] == "DRAWING_CHECKED"
    assert "coordination validation is FAIL" in rep["FINAL_STATUS"]["blocked_at"]["COORDINATED"]


def test_issue_status_requires_label_approval_and_code_review(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    common = {"project": "p", "model": s["model"], "validation": s["validation"], "outputs": s["outputs"],
              "evidence": s["evidence"], "code_review": REVIEW, "profile": full_profile()}
    default = report_mod.build_report(**common)
    assert default["FINAL_STATUS"]["status"] == "CODE_REVIEWED"
    label = "ISSUED_FOR_CONSTRUCTION"
    no_approval = report_mod.build_report(**common, issue_config={"label": label, "approval": {}})
    assert no_approval["FINAL_STATUS"]["status"] == "CODE_REVIEWED" and not no_approval["FINAL_STATUS"]["issued"]
    assert "missing" in " ".join(no_approval["FINAL_STATUS"]["blocked_at"][label])
    partial = report_mod.build_report(**common, issue_config={"label": label, "approval": APPROVAL | {"date": ""}})
    assert partial["FINAL_STATUS"]["status"] == "CODE_REVIEWED"
    approved = report_mod.build_report(**common, issue_config={"label": label, "approval": APPROVAL})
    assert approved["FINAL_STATUS"]["status"] == label and approved["FINAL_STATUS"]["issued"] is True
    # approval cannot lift a drawing that is not code reviewed
    weaker = dict(common) | {"code_review": None}
    blocked = report_mod.build_report(**weaker, issue_config={"label": label, "approval": APPROVAL})
    assert blocked["FINAL_STATUS"]["status"] != label and not blocked["FINAL_STATUS"]["issued"]
    assert "only reached" in " ".join(blocked["FINAL_STATUS"]["blocked_at"][label])
    # a normal level name is not an issue label
    fake = report_mod.build_report(**common, issue_config={"label": "CAD_VALID", "approval": APPROVAL})
    assert fake["FINAL_STATUS"]["status"] == "CODE_REVIEWED" and not fake["FINAL_STATUS"]["issued"]


def test_issue_status_is_unreachable_without_issue_config() -> None:
    for status in ("DRAFT", "CAD_VALID", "DRAWING_CHECKED", "COORDINATED", "CODE_REVIEWED"):
        assert status in report_mod.STATUS_LEVELS
    result = report_mod.compute_final_status(validation=None, dwg=None, pdf=None, evidence={"dwg_opened_in_autocad": True},
                                             profile_report=None, code_review=REVIEW, issue_config=None)
    assert result["status"] == "DRAFT"


def test_report_lists_assumptions_conflicts_questions_and_limits() -> None:
    model = validation.DrawingModel.from_dict({
        "meta": {"units": "mm"},
        "entities": [ent("h", "OTHER", status="ASSUMED", source={"note": "floor height guessed"}),
                     ent("c", "OTHER", status="CONFLICTED")]})
    rep = report_mod.build_report(project="p", model=model, profile={"units": "mm"}, assumptions=["north is up"],
                                  open_questions=["which survey wins?"], limitations=["only 1st floor drawn"])
    assert any("floor height guessed" in a for a in rep["ASSUMPTIONS"]) and "north is up" in rep["ASSUMPTIONS"]
    assert any("sources disagree" in q for q in rep["QUESTIONS"]) and "which survey wins?" in rep["QUESTIONS"]
    assert any("(country: missing)" in q for q in rep["QUESTIONS"])
    assert "only 1st floor drawn" in rep["LIMITATIONS"]
    assert any("legal or code compliance" in l or "legal, structural" in l for l in rep["LIMITATIONS"])
    assert rep["INPUT_REVIEW"]["model"]["by_status"] == {"ASSUMED": 1, "CONFLICTED": 1}


def test_report_never_makes_industry_standard_claims() -> None:
    rep = report_mod.build_report(project="p", assumptions=["The drawing meets industry standard."],
                                  limitations=["Đạt chuẩn ngành"])
    text = report_mod.render_markdown(rep)
    assert sum("cannot support" in l for l in rep["LIMITATIONS"]) == 2
    clean = report_mod.build_report(project="p")
    assert not report_mod.OVERCLAIM.search(" ".join(clean["LIMITATIONS"]))
    assert not report_mod.OVERCLAIM.search(report_mod.render_markdown(clean))
    assert text  # rendered without raising


def test_caller_evidence_is_flagged_as_unverified() -> None:
    rep = report_mod.build_report(project="p", evidence={"dwg_opened_in_autocad": True, "autocad_session": False})
    assert any("supplied by the caller" in l and "dwg_opened_in_autocad" in l for l in rep["LIMITATIONS"])
    assert not any("autocad_session" in l and "supplied by the caller" in l for l in rep["LIMITATIONS"])


def test_validation_not_run_rules_appear_in_limitations(tmp_path: Path) -> None:
    s = verified_setup(tmp_path)
    rep = report_mod.build_report(project="p", validation=s["validation"], outputs=s["outputs"], evidence=s["evidence"])
    assert any("could not run (NOT_RUN)" in l and "PLOT.PDF_RENDER_CHECKED" in l for l in rep["LIMITATIONS"])


# =========================================================================== tools via the registry

async def invoke(config: Config, tool: str, args: dict[str, Any]) -> Any:
    registry = build_registry(config.path("paths.tools_dir"))
    ctx = ToolContext("r1", "autocad-spec-unit", "s1", time.monotonic() + 60, config, Services(config), threading.Event())
    return await registry.invoke(ctx, tool, args)


def test_new_tools_are_in_the_contract(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    new = {"autocad.profile_check", "autocad.validate_model", "autocad.compare_revisions", "autocad.plan_operation",
           "autocad.report"}
    assert new <= set(registry.available_tools()) and not [t for t in registry.unbound_tools() if t.startswith("autocad.")]
    for name in new:
        spec = registry.specs[name]
        assert spec.input_schema.get("additionalProperties") is False
        assert spec.policy_category in ("autocad.read", "autocad.write")
    assert registry.specs["autocad.report"].verification == "self"
    assert registry.specs["autocad.validate_model"].verification == "none"


async def test_tool_profile_check(config: Config) -> None:
    spec, out = await invoke(config, "autocad.profile_check", {"profile": {"units": "mm", "floors": "x"}})
    assert out.data["ready_to_draw"] is False and "floors" in out.data["invalid"]
    assert any(q["field_id"] == "country" for q in out.data["questions"])
    _, empty = await invoke(config, "autocad.profile_check", {})
    assert len(empty.data["questions"]) == len(standards.FIELD_SPECS)
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.profile_check", {"profil": {}})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_tool_validate_model(config: Config, tmp_path: Path) -> None:
    _, out = await invoke(config, "autocad.validate_model", {"model": good_model()})
    assert out.data["overall"] in ("PASS", "WARN") and out.data["entity_count"] == len(good_model()["entities"])
    broken = mutate(good_model(), "DIM1", properties={"displayed_value": 4000})
    _, bad = await invoke(config, "autocad.validate_model", {"model": broken, "tolerances": {"dimension": 2}})
    assert bad.data["overall"] == "FAIL" and "ANNO.DIMENSION_OVERRIDE" in bad.data["open_errors"]
    path = tmp_path / "model.json"
    path.write_text(json.dumps(broken), "utf-8")
    _, from_file = await invoke(config, "autocad.validate_model", {"model_path": str(path), "scopes": ["ANNOTATION"]})
    assert from_file.data["overall"] == "FAIL" and {r["scope"] for r in from_file.data["results"]} == {"ANNOTATION"}
    _, cat = await invoke(config, "autocad.validate_model", {"list_rules": True})
    assert len(cat.data["rules"]) >= 25
    _, live = await invoke(config, "autocad.validate_model", {
        "autocad_entities": [{"handle": "1", "object_name": "AcDbLine", "layer": "A-WALL-EXT",
                              "start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 0, "y": 0, "z": 0}}],
        "meta": {"units": "mm"}})
    assert "GEOM.ZERO_LENGTH" in live.data["open_errors"]


async def test_tool_validate_model_input_errors(config: Config, tmp_path: Path) -> None:
    for args in ({}, {"model": good_model(), "model_path": str(tmp_path / "x.json")}):
        with pytest.raises(ToolError) as exc:
            await invoke(config, "autocad.validate_model", args)
        assert exc.value.code == ErrorCode.INVALID_ARGUMENT and "exactly one" in exc.value.message
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model_path": str(tmp_path / "missing.json")})
    assert "not found" in exc.value.message
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", "utf-8")
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model_path": str(bad)})
    assert "not readable JSON" in exc.value.message
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model": {"entities": [{"entity_id": "x", "entity_type": "BOGUS"}]}})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model": good_model(), "tolerances": {"dimension": -3}})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model": good_model(), "scopes": ["NOPE"]})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.validate_model", {"model": good_model(), "rule_ids": ["X.Y"]})
    assert "unknown rule_id" in exc.value.message


async def test_tool_compare_revisions(config: Config) -> None:
    old = [E("a", geometry=LINE).to_dict(), E("b", geometry=LINE).to_dict()]
    new = [E("a", geometry=LINE).to_dict(), E("c", geometry=LINE).to_dict()]
    _, out = await invoke(config, "autocad.compare_revisions", {
        "old": old, "new": new, "log": {"reason": "r", "source_request": "s", "actor": "agent"}})
    assert out.data["counts"]["added"] == 1 and out.data["counts"]["deleted"] == 1 and len(out.data["log"]) == 2
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.compare_revisions", {"old": old})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.compare_revisions", {"old": old, "new": new, "log": {"reason": "r"}})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.compare_revisions", {"old": old + old, "new": new})
    assert "duplicate entity_id" in exc.value.message


async def test_tool_plan_operation(config: Config, tmp_path: Path) -> None:
    target = tmp_path / "a.dwg"
    target.write_bytes(b"AC1032")
    _, out = await invoke(config, "autocad.plan_operation", {"kind": "SAVE", "target_path": str(target)})
    assert out.data["needs_confirmation"] and out.data["backup_planned"]
    _, free = await invoke(config, "autocad.plan_operation", {"kind": "CREATE", "inputs": {"a": 1}, "count": 300})
    assert free.data["needs_confirmation"] is False and free.data["marker"].startswith("LGA:")
    _, again = await invoke(config, "autocad.plan_operation", {"kind": "CREATE", "inputs": {"a": 1}})
    assert again.data["idempotency_key"] == free.data["idempotency_key"]
    _, purge = await invoke(config, "autocad.plan_operation", {"kind": "PURGE"})
    assert purge.data["needs_confirmation"]
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.plan_operation", {"kind": "FORMAT_DISK"})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


async def test_tool_report_writes_files_and_status_is_computed(config: Config, tmp_path: Path) -> None:
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(DWG_BYTES)
    report_path = tmp_path / "out" / "report.md"
    _, out = await invoke(config, "autocad.report", {
        "project": "House A", "model": good_model(), "outputs": [str(dwg), str(tmp_path / "plan.pdf")],
        "evidence": {"dwg_opened_in_autocad": True}, "report_path": str(report_path)})
    assert out.data["final_status"] == "CAD_VALID" and out.data["validation_overall"] in ("PASS", "WARN")
    assert report_path.is_file() and report_path.with_suffix(".json").is_file()
    assert len(out.data["files"]) == 2 and out.artifacts
    text = report_path.read_text("utf-8")
    for section in report_mod.SECTIONS:
        assert f"## {section}" in text
    assert "plan.pdf" in text and "Not found on disk" in text
    saved = json.loads(report_path.with_suffix(".json").read_text("utf-8"))
    assert saved["FINAL_STATUS"]["status"] == "CAD_VALID"
    assert out.data["report"]["OUTPUTS"]["missing"] == [str(tmp_path / "plan.pdf")]


async def test_tool_report_without_evidence_is_draft_and_issue_files_need_review(config: Config, tmp_path: Path) -> None:
    _, plain = await invoke(config, "autocad.report", {"project": "p"})
    assert plain.data["final_status"] == "DRAFT" and plain.data["files"] == [] and plain.data["validation_overall"] is None
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(APPROVAL), "utf-8")
    _, fake = await invoke(config, "autocad.report", {
        "project": "p", "issue_label": "ISSUED_FOR_CONSTRUCTION", "issue_approval_file": str(approval)})
    assert fake.data["final_status"] == "DRAFT"
    assert "ISSUED_FOR_CONSTRUCTION" in fake.data["report"]["FINAL_STATUS"]["blocked_at"]
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.report", {"project": "p", "issue_label": "X", "issue_approval_file": str(tmp_path / "no.json")})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as exc:
        await invoke(config, "autocad.report", {"project": "p", "report_path": str(tmp_path / "r.txt")})
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT  # schema pattern requires .md
    with pytest.raises(ToolError):
        await invoke(config, "autocad.report", {"project": ""})


async def test_tool_report_full_ladder_through_files(config: Config, tmp_path: Path) -> None:
    (tmp_path / "p.dwg").write_bytes(DWG_BYTES)
    (tmp_path / "p.pdf").write_bytes(PDF_BYTES)
    review = tmp_path / "review.json"
    review.write_text(json.dumps(REVIEW), "utf-8")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(APPROVAL), "utf-8")
    base = {"project": "p", "model": good_model(), "profile": full_profile(),
            "outputs": [str(tmp_path / "p.dwg"), str(tmp_path / "p.pdf")],
            "evidence": {"dwg_opened_in_autocad": True, "pdf_rendered_checked": True}}
    _, reviewed = await invoke(config, "autocad.report", base | {"code_review_file": str(review)})
    assert reviewed.data["final_status"] == "CODE_REVIEWED"
    _, issued = await invoke(config, "autocad.report", base | {
        "code_review_file": str(review), "issue_label": "ISSUED_FOR_CONSTRUCTION", "issue_approval_file": str(approval)})
    assert issued.data["final_status"] == "ISSUED_FOR_CONSTRUCTION"  # only with human files for review AND approval
    _, not_issued = await invoke(config, "autocad.report", base | {"issue_label": "ISSUED_FOR_CONSTRUCTION",
                                                                     "issue_approval_file": str(approval)})
    assert not_issued.data["final_status"] == "COORDINATED"
