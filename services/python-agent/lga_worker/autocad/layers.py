"""Layer standard mapping, units and 1:1 model-space checks (spec sections 5 and 6). Pure Python.

The A-* names are SAMPLE names from the specification. When the office has its own template, `LayerMap` maps each
standard name to the office layer so the drawing code asks for "A-DOOR" and the file gets the office name.
Other disciplines use their own namespace (S-, E-, M-, P-, FP-); architectural code must not put such entities on
A-* layers without a stated responsibility boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lga_worker.autocad.provenance import Entity, EntityType, Source, SourceStatus

STANDARD_LAYERS: dict[str, str] = {
    "A-SITE-BOUNDARY": "Site boundary / setback lines (sourced)",
    "A-SITE-CONTEXT": "Existing context",
    "A-GRID": "Structural grid",
    "A-WALL-EXT": "Exterior walls",
    "A-WALL-INT": "Interior walls",
    "A-WALL-EXIST": "Existing walls",
    "A-WALL-DEMO": "Walls to be demolished",
    "A-OPENING": "Openings",
    "A-DOOR": "Doors",
    "A-WINDOW": "Windows",
    "A-COLUMN-ARCH": "Columns as drawn by the architect",
    "A-STAIR": "Stairs",
    "A-RAILING": "Railings",
    "A-ROOF": "Roof",
    "A-CEILING": "Ceiling",
    "A-FLOOR-FINISH": "Floor finish areas",
    "A-WALL-FINISH": "Wall finish areas",
    "A-ROOM-BOUNDARY": "Room boundaries",
    "A-ROOM-TAG": "Room labels",
    "A-FURN": "Illustrative furniture",
    "A-FIXT": "Fixed equipment",
    "A-GLAZING": "Glazing",
    "A-SECTION-CUT": "Section cut lines",
    "A-SECTION-PROJ": "Section projection lines",
    "A-HIDDEN": "Hidden lines",
    "A-HATCH": "Material hatches",
    "A-DIMS": "Dimensions",
    "A-ANNO-TEXT": "Annotation text",
    "A-ANNO-LEVEL": "Level annotations",
    "A-ANNO-GRID": "Grid annotations",
    "A-ANNO-KEYNOTE": "Material keynotes",
    "A-DETAIL-MARKER": "Detail reference markers",
    "A-AREA": "Area boundaries",
    "A-OVERHEAD": "Objects above the cut plane",
    "A-NPLT": "Controlled non-plotting objects",
}

NAMESPACES = {
    "A": "Architecture", "S": "Structure", "E": "Electrical", "M": "HVAC / mechanical", "P": "Plumbing",
    "FP": "Fire protection",
}

LAYER_ENTITY_TYPE: dict[str, EntityType] = {
    "A-SITE-BOUNDARY": EntityType.SITE, "A-SITE-CONTEXT": EntityType.SITE, "A-GRID": EntityType.GRID,
    "A-WALL-EXT": EntityType.WALL, "A-WALL-INT": EntityType.WALL, "A-WALL-EXIST": EntityType.WALL,
    "A-WALL-DEMO": EntityType.WALL, "A-OPENING": EntityType.OPENING, "A-DOOR": EntityType.DOOR,
    "A-WINDOW": EntityType.WINDOW, "A-COLUMN-ARCH": EntityType.COLUMN, "A-STAIR": EntityType.STAIR,
    "A-RAILING": EntityType.RAILING, "A-ROOF": EntityType.ROOF, "A-ROOM-BOUNDARY": EntityType.ROOM,
    "A-FIXT": EntityType.FIXTURE, "A-FURN": EntityType.FIXTURE, "A-DIMS": EntityType.DIMENSION,
    "A-ANNO-TEXT": EntityType.ANNOTATION, "A-ANNO-LEVEL": EntityType.ANNOTATION,
    "A-ANNO-GRID": EntityType.ANNOTATION, "A-ANNO-KEYNOTE": EntityType.ANNOTATION,
    "A-DETAIL-MARKER": EntityType.DETAIL_MARKER,
}


class LayerMapError(ValueError):
    pass


def namespace_of(layer: str) -> str | None:
    """'FP-' is checked before 'F...'; returns the namespace key or None when the name has no known prefix."""
    upper = layer.strip().upper()
    for key in sorted(NAMESPACES, key=len, reverse=True):
        if upper.startswith(key + "-"):
            return key
    return None


@dataclass
class LayerMap:
    """standard name -> office layer name. An empty map means "use the standard names as they are"."""

    to_office_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reverse: dict[str, str] = {}
        for std, office in self.to_office_map.items():
            if std not in STANDARD_LAYERS:
                raise LayerMapError(f"{std!r} is not a standard layer name; known: see STANDARD_LAYERS")
            if not str(office).strip():
                raise LayerMapError(f"office layer for {std} is empty")
            key = str(office).strip().upper()
            if key in reverse and reverse[key] != std:
                raise LayerMapError(f"office layer {office!r} is the target of both {reverse[key]} and {std}")
            reverse[key] = std
        self._reverse = reverse

    def to_office(self, standard: str) -> str:
        if standard not in STANDARD_LAYERS:
            raise LayerMapError(f"{standard!r} is not a standard layer name")
        return self.to_office_map.get(standard, standard)

    def to_standard(self, layer: str) -> str | None:
        """The standard name behind a layer in the file (office name or the standard name itself), else None."""
        upper = layer.strip().upper()
        if upper in self._reverse:
            return self._reverse[upper]
        # a standard name used verbatim counts only when the office map does not rename it
        if upper in STANDARD_LAYERS and upper not in self.to_office_map:
            return upper
        return None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> LayerMap:
        return cls({str(k): str(v) for k, v in (raw or {}).items()})


def layer_for_entity(entity_type: EntityType | str, properties: Mapping[str, Any] | None = None,
                     layer_map: LayerMap | None = None) -> tuple[str | None, str]:
    """(layer in the file or None, reason). Ambiguous cases return None instead of guessing."""
    layer_map = layer_map or LayerMap()
    props = properties or {}
    etype = entity_type if isinstance(entity_type, EntityType) else EntityType(str(entity_type).upper())
    standard: str | None
    reason = ""
    if etype is EntityType.WALL:
        phase = str(props.get("phase") or "").lower()
        if phase == "existing":
            standard = "A-WALL-EXIST"
        elif phase == "demolish":
            standard = "A-WALL-DEMO"
        elif props.get("exterior") is True:
            standard = "A-WALL-EXT"
        elif props.get("exterior") is False:
            standard = "A-WALL-INT"
        else:
            return None, "wall layer needs 'exterior' true/false or 'phase' existing/demolish; not guessed"
    else:
        by_type = {v: k for k, v in LAYER_ENTITY_TYPE.items() if k not in ("A-SITE-CONTEXT", "A-FURN", "A-ANNO-LEVEL",
                                                                              "A-ANNO-GRID", "A-ANNO-KEYNOTE",
                                                                              "A-WALL-EXIST", "A-WALL-DEMO")}
        standard = by_type.get(etype)
        if standard is None:
            return None, f"no standard layer for {etype.value}; choose one explicitly"
    return layer_map.to_office(standard), reason


def classify_layer(layer: str, layer_map: LayerMap | None = None) -> dict[str, Any]:
    layer_map = layer_map or LayerMap()
    standard = layer_map.to_standard(layer)
    ns = namespace_of(layer)
    return {"layer": layer, "standard": standard, "namespace": ns,
            "discipline": NAMESPACES.get(ns) if ns else None,
            "entity_type": LAYER_ENTITY_TYPE[standard].value if standard in LAYER_ENTITY_TYPE else None}


def check_layers(layers: Sequence[str], layer_map: LayerMap | None = None) -> dict[str, Any]:
    """Classify the layers of a file: standard, other-discipline, unmapped. Layer '0' and 'Defpoints' are system layers."""
    layer_map = layer_map or LayerMap()
    system = {"0", "DEFPOINTS"}
    standard: list[str] = []
    other_discipline: list[dict[str, str]] = []
    unmapped: list[str] = []
    for layer in layers:
        if layer.strip().upper() in system:
            continue
        info = classify_layer(layer, layer_map)
        if info["standard"]:
            standard.append(layer)
        elif info["namespace"] and info["namespace"] != "A":
            other_discipline.append({"layer": layer, "discipline": info["discipline"]})
        else:
            unmapped.append(layer)
    return {"standard": standard, "other_discipline": other_discipline, "unmapped": unmapped,
            "ok": not unmapped}


# --------------------------------------------------------------------------- units

# AutoCAD INSUNITS value -> (name, millimetres per unit). Only values with a clear mapping are listed.
INSUNITS: dict[int, tuple[str, float | None]] = {
    0: ("unitless", None), 1: ("in", 25.4), 2: ("ft", 304.8), 3: ("mi", 1_609_344.0), 4: ("mm", 1.0),
    5: ("cm", 10.0), 6: ("m", 1000.0), 7: ("km", 1_000_000.0), 9: ("mil", 0.0254), 10: ("yd", 914.4),
    14: ("dm", 100.0),
}
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8, "dm": 100.0, "yd": 914.4}


def unit_to_mm(unit: str | None) -> float | None:
    return UNIT_TO_MM.get(str(unit).strip().lower()) if unit else None


def insunits_name(code: int | None) -> str | None:
    if code is None:
        return None
    entry = INSUNITS.get(int(code))
    return entry[0] if entry and entry[1] is not None else None


def check_units(insunits: int | None, expected_unit: str | None) -> dict[str, Any]:
    """Compare the drawing's INSUNITS with the unit the profile asks for. Unknown inputs give NOT_RUN or FAIL, never PASS."""
    if insunits is None:
        return {"result": "NOT_RUN", "evidence": "INSUNITS was not read from the drawing", "unit": None}
    code = int(insunits)
    name = insunits_name(code)
    if name is None:
        return {"result": "FAIL", "severity": "BLOCKER", "unit": None,
                "evidence": f"INSUNITS={code} is unitless/unsupported: model-space distances cannot be trusted"}
    if not expected_unit:
        return {"result": "WARN", "severity": "WARNING", "unit": name,
                "evidence": f"INSUNITS={code} ({name}) but the profile has no units to compare with"}
    expected = str(expected_unit).strip().lower()
    if expected != name:
        return {"result": "FAIL", "severity": "BLOCKER", "unit": name,
                "evidence": f"INSUNITS={code} ({name}) differs from profile units {expected!r}; no automatic rescale"}
    return {"result": "PASS", "unit": name, "evidence": f"INSUNITS={code} ({name}) matches the profile"}


def check_model_scale_1to1(unit: str | None, measured_length: float | None, reference_length_mm: float | None,
                           relative_tolerance: float) -> dict[str, Any]:
    """Model space must be drawn 1:1 in real units. Needs one trusted real-world length to prove it."""
    factor = unit_to_mm(unit)
    if factor is None:
        return {"result": "NOT_RUN", "evidence": "drawing unit unknown: cannot convert a length to millimetres"}
    if measured_length is None or reference_length_mm is None:
        return {"result": "NOT_RUN",
                "evidence": "no reference length given (a real-world length and the same length measured in the drawing)"}
    if reference_length_mm <= 0 or relative_tolerance <= 0:
        raise ValueError("reference_length_mm and relative_tolerance must be positive")
    measured_mm = float(measured_length) * factor
    ratio = measured_mm / float(reference_length_mm)
    if math.isclose(ratio, 1.0, rel_tol=relative_tolerance):
        return {"result": "PASS", "evidence": f"measured {measured_mm:g} mm vs reference {reference_length_mm:g} mm "
                                              f"(ratio {ratio:.6g}, tolerance {relative_tolerance:g})"}
    return {"result": "FAIL", "severity": "BLOCKER",
            "evidence": f"measured {measured_mm:g} mm vs reference {reference_length_mm:g} mm: ratio {ratio:.6g} "
                        f"outside tolerance {relative_tolerance:g}; model space is not 1:1 in {unit}"}


# --------------------------------------------------------------------------- import from provider entities

def _xy(point: Mapping[str, Any]) -> list[float]:
    return [float(point["x"]), float(point["y"])]


def import_autocad_entities(raw: Sequence[Mapping[str, Any]], layer_map: LayerMap | None = None,
                            units: str | None = None, source_file: str | None = None) -> list[Entity]:
    """Convert `autocad.get_entities` output to the intermediate model.

    Geometry is IMPORTED. The entity TYPE is only inferred from a standard layer name (status stays IMPORTED, the
    property `type_inferred_from_layer` records it); any other layer gives OTHER. Nothing is inferred from shape.
    """
    layer_map = layer_map or LayerMap()
    out: list[Entity] = []
    for item in raw:
        handle = str(item["handle"])
        name = str(item.get("object_name", "")).replace("AcDb", "").lower()
        geometry: dict[str, Any] | None
        elevation: float | None = None
        if name == "line" and "start" in item and "end" in item:
            geometry = {"type": "line", "start": _xy(item["start"]), "end": _xy(item["end"])}
            elevation = float(item["start"].get("z", 0.0))
        elif name == "polyline" and "points" in item:
            geometry = {"type": "polyline", "points": [_xy(p) for p in item["points"]],
                        "closed": bool(item.get("closed"))}
            elevation = float(item["points"][0].get("z", 0.0)) if item["points"] else None
        elif name == "circle" and "center" in item:
            geometry = {"type": "circle", "center": _xy(item["center"]), "radius": float(item["radius"])}
        elif name in ("text", "mtext") and "insertion" in item:
            geometry = {"type": "point", "point": _xy(item["insertion"])}
        else:
            geometry = None
        layer = str(item.get("layer") or "")
        info = classify_layer(layer, layer_map) if layer else {"entity_type": None}
        etype = EntityType(info["entity_type"]) if info.get("entity_type") else EntityType.OTHER
        props: dict[str, Any] = {"object_name": item.get("object_name")}
        if "text" in item:
            props["text"] = item["text"]
        if "height" in item:
            props["text_height"] = item["height"]
        if etype is not EntityType.OTHER:
            props["type_inferred_from_layer"] = True
        out.append(Entity(entity_id=f"ACAD-{handle}", entity_type=etype, geometry=geometry, units=units,
                          elevation=elevation, properties=props, layer=layer or None,
                          source=Source(file=source_file, entity=handle, units=units, confidence=None,
                                        note="imported from AutoCAD"),
                          status=SourceStatus.IMPORTED))
    return out
