"""Provenance model for drawing data (spec section 3): where every value came from and how far it can be trusted.

Pure Python, no AutoCAD needed. Rules this module enforces:

* every datum carries a `source_status` (PROVIDED, MEASURED, IMPORTED, DERIVED, ASSUMED, UNKNOWN, CONFLICTED);
* a missing value is `None` + UNKNOWN, never an invented number;
* an ASSUMED value can only become PROVIDED through `Datum.confirm()` with real evidence, and there is no
  "VERIFIED" status that code could reach by itself;
* two disagreeing sources are both kept (`Datum.conflict`) and the question "which source wins?" is explicit.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class SourceStatus(str, enum.Enum):
    PROVIDED = "PROVIDED"      # given by the user / an authoritative document
    MEASURED = "MEASURED"      # measured from geometry or a survey
    IMPORTED = "IMPORTED"      # read from a file (DWG, DXF, PDF, IFC)
    DERIVED = "DERIVED"        # computed from other data (area from a boundary, ...)
    ASSUMED = "ASSUMED"        # a working assumption, must be listed and confirmed
    UNKNOWN = "UNKNOWN"        # no value
    CONFLICTED = "CONFLICTED"  # two or more sources disagree


class EntityType(str, enum.Enum):
    SITE = "SITE"
    GRID = "GRID"
    LEVEL = "LEVEL"
    WALL = "WALL"
    COLUMN = "COLUMN"
    SLAB = "SLAB"
    DOOR = "DOOR"
    WINDOW = "WINDOW"
    STAIR = "STAIR"
    RAILING = "RAILING"
    ROOF = "ROOF"
    ROOM = "ROOM"
    OPENING = "OPENING"
    FINISH = "FINISH"
    FIXTURE = "FIXTURE"
    ANNOTATION = "ANNOTATION"
    DIMENSION = "DIMENSION"
    DETAIL_MARKER = "DETAIL_MARKER"
    OTHER = "OTHER"


class ProvenanceError(ValueError):
    """An operation would break a provenance rule (for example promoting ASSUMED data without evidence)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status(value: Any, *, what: str = "source_status") -> SourceStatus:
    if isinstance(value, SourceStatus):
        return value
    try:
        return SourceStatus(str(value).strip().upper())
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SourceStatus)
        raise ProvenanceError(f"{what} {value!r} is not one of {allowed}") from exc


@dataclass
class Source:
    """Where a value came from: a file/entity/page reference, the unit it is expressed in, when, and how sure."""

    file: str | None = None
    entity: str | None = None      # entity handle / id in that file
    page: str | int | None = None  # PDF page, sheet, layout
    units: str | None = None
    timestamp: str | None = None
    confidence: float | None = None  # 0..1, None = not rated
    note: str = ""

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            raise ProvenanceError(f"confidence must be within 0..1, got {self.confidence!r}")

    def is_empty(self) -> bool:
        return not any((self.file, self.entity, self.page, self.note))

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "entity": self.entity, "page": self.page, "units": self.units,
                "timestamp": self.timestamp, "confidence": self.confidence, "note": self.note}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Source:
        raw = raw or {}
        return cls(file=raw.get("file"), entity=raw.get("entity"), page=raw.get("page"), units=raw.get("units"),
                   timestamp=raw.get("timestamp"), confidence=raw.get("confidence"), note=str(raw.get("note") or ""))


@dataclass
class Datum:
    """One value with its provenance. `alternatives` holds the competing (value, source) pairs when CONFLICTED."""

    value: Any = None
    status: SourceStatus = SourceStatus.UNKNOWN
    source: Source = field(default_factory=Source)
    alternatives: list[tuple[Any, Source]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = _status(self.status)
        if self.status is SourceStatus.UNKNOWN and self.value is not None:
            raise ProvenanceError("an UNKNOWN datum must have value None")
        if self.status not in (SourceStatus.UNKNOWN, SourceStatus.CONFLICTED) and self.value is None:
            raise ProvenanceError(f"a {self.status.value} datum needs a value (use UNKNOWN for a missing one)")

    @classmethod
    def unknown(cls, note: str = "") -> Datum:
        return cls(None, SourceStatus.UNKNOWN, Source(note=note))

    @classmethod
    def assumed(cls, value: Any, note: str) -> Datum:
        if not note.strip():
            raise ProvenanceError("an ASSUMED value must say why it is assumed")
        return cls(value, SourceStatus.ASSUMED, Source(note=note, timestamp=now_iso()))

    def confirm(self, evidence: Source) -> Datum:
        """ASSUMED/DERIVED -> PROVIDED, only when the caller names real evidence (file/entity/page or a note)."""
        if self.status in (SourceStatus.UNKNOWN, SourceStatus.CONFLICTED):
            raise ProvenanceError(f"a {self.status.value} datum cannot be confirmed; set or resolve it first")
        if evidence.is_empty():
            raise ProvenanceError("confirming a value needs evidence (file, entity, page or note); none was given")
        return Datum(self.value, SourceStatus.PROVIDED, evidence)

    def conflict(self, other_value: Any, other_source: Source) -> Datum:
        """Both values are kept; nothing is silently overwritten. The caller must ask which source wins."""
        if self.status is SourceStatus.UNKNOWN:
            raise ProvenanceError("nothing to conflict with: this datum is UNKNOWN")
        pairs = list(self.alternatives) if self.status is SourceStatus.CONFLICTED else [(self.value, self.source)]
        pairs.append((other_value, other_source))
        return Datum(None, SourceStatus.CONFLICTED, Source(note="sources disagree; user must choose"), pairs)

    def resolve(self, chosen_index: int, decided_by: str) -> Datum:
        if self.status is not SourceStatus.CONFLICTED:
            raise ProvenanceError("only a CONFLICTED datum can be resolved")
        if not decided_by.strip():
            raise ProvenanceError("resolving a conflict needs the name of whoever chose the source")
        if not 0 <= chosen_index < len(self.alternatives):
            raise ProvenanceError(f"chosen_index {chosen_index} is outside the {len(self.alternatives)} alternatives")
        value, source = self.alternatives[chosen_index]
        note = f"{source.note} | conflict resolved by {decided_by}; discarded: {self.alternatives_summary(chosen_index)}"
        return Datum(value, SourceStatus.PROVIDED,
                     Source(source.file, source.entity, source.page, source.units, now_iso(), source.confidence, note))

    def alternatives_summary(self, skip: int | None = None) -> str:
        return "; ".join(f"{v!r} from {s.file or s.entity or s.note or '?'}"
                         for i, (v, s) in enumerate(self.alternatives) if i != skip)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "source_status": self.status.value, "source": self.source.to_dict()}
        if self.alternatives:
            out["alternatives"] = [{"value": v, "source": s.to_dict()} for v, s in self.alternatives]
        return out


# --------------------------------------------------------------------------- entity schema

ENTITY_FIELDS = ("entity_id", "entity_type", "geometry", "coordinate_system", "units", "level_id", "elevation",
                 "height", "properties", "relationships", "source", "status", "revision", "validation_results",
                 "issue_ids", "layer")


@dataclass
class Entity:
    """Intermediate-model entity (spec section 3). Fields absent from the source stay None/UNKNOWN."""

    entity_id: str
    entity_type: EntityType
    geometry: dict[str, Any] | None = None
    coordinate_system: str | None = None
    units: str | None = None
    level_id: str | None = None
    elevation: float | None = None
    height: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    relationships: dict[str, Any] = field(default_factory=dict)
    source: Source = field(default_factory=Source)
    status: SourceStatus = SourceStatus.UNKNOWN
    revision: str | None = None
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)
    layer: str | None = None

    def __post_init__(self) -> None:
        if not str(self.entity_id).strip():
            raise ProvenanceError("entity_id must not be empty")
        self.entity_id = str(self.entity_id)
        if not isinstance(self.entity_type, EntityType):
            try:
                self.entity_type = EntityType(str(self.entity_type).strip().upper())
            except ValueError as exc:
                raise ProvenanceError(
                    f"entity {self.entity_id}: unknown entity_type {self.entity_type!r}; use OTHER when unsure") from exc
        self.status = _status(self.status, what=f"entity {self.entity_id}: status")

    def prop(self, name: str, default: Any = None) -> Any:
        value = self.properties.get(name, default)
        # a property may be wrapped as a Datum dict {"value":..,"source_status":..}
        if isinstance(value, Mapping) and "value" in value and "source_status" in value:
            return value["value"]
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id, "entity_type": self.entity_type.value, "geometry": self.geometry,
            "coordinate_system": self.coordinate_system, "units": self.units, "level_id": self.level_id,
            "elevation": self.elevation, "height": self.height, "properties": self.properties,
            "relationships": self.relationships, "source": self.source.to_dict(), "status": self.status.value,
            "revision": self.revision, "validation_results": self.validation_results, "issue_ids": self.issue_ids,
            "layer": self.layer,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Entity:
        if not isinstance(raw, Mapping):
            raise ProvenanceError(f"entity must be an object, got {type(raw).__name__}")
        unknown = set(raw) - set(ENTITY_FIELDS)
        if unknown:
            raise ProvenanceError(f"entity {raw.get('entity_id')!r}: unknown field(s) {sorted(unknown)}")
        for required in ("entity_id", "entity_type"):
            if required not in raw:
                raise ProvenanceError(f"entity is missing required field {required!r}")
        source = raw.get("source")
        return cls(
            entity_id=raw["entity_id"], entity_type=raw["entity_type"], geometry=raw.get("geometry"),
            coordinate_system=raw.get("coordinate_system"), units=raw.get("units"), level_id=raw.get("level_id"),
            elevation=raw.get("elevation"), height=raw.get("height"), properties=dict(raw.get("properties") or {}),
            relationships=dict(raw.get("relationships") or {}),
            source=source if isinstance(source, Source) else Source.from_dict(source),
            status=raw.get("status", SourceStatus.UNKNOWN), revision=raw.get("revision"),
            validation_results=list(raw.get("validation_results") or []), issue_ids=list(raw.get("issue_ids") or []),
            layer=raw.get("layer"),
        )


def load_entities(raw: list[Mapping[str, Any]]) -> list[Entity]:
    """Parse and check a list of entity dicts; duplicate entity_ids are an error (ids must be stable and unique)."""
    entities = [e if isinstance(e, Entity) else Entity.from_dict(e) for e in raw]
    seen: set[str] = set()
    for entity in entities:
        if entity.entity_id in seen:
            raise ProvenanceError(f"duplicate entity_id {entity.entity_id!r}: ids must be unique and stable")
        seen.add(entity.entity_id)
    return entities


@dataclass
class ConflictRecord:
    """Two sources disagree about the same field of the same entity. Both values are kept."""

    entity_id: str
    field: str
    values: list[tuple[Any, Source]]
    question: str = ""
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "field": self.field,
                "values": [{"value": v, "source": s.to_dict()} for v, s in self.values],
                "question": self.question or f"Which source is authoritative for {self.entity_id}.{self.field}?",
                "resolved": self.resolved}


def record_conflict(entity: Entity, field_name: str, existing: Any, existing_source: Source, incoming: Any,
                    incoming_source: Source) -> ConflictRecord:
    """Mark `entity` CONFLICTED and return the record that keeps both values (the entity's own value is unchanged)."""
    entity.status = SourceStatus.CONFLICTED
    return ConflictRecord(entity.entity_id, field_name, [(existing, existing_source), (incoming, incoming_source)])
