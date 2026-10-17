"""Revision comparison and log (spec section 16). Pure Python.

`compare_revisions(old, new, tolerance)` classifies every entity as added / modified / deleted / conflicted by
geometry, layer, properties and relationships (not a binary diff). Entities are matched by their stable id. When the
ids barely overlap the mapping is uncertain: deletions are then flagged and a mass deletion must not be accepted
without confirmation. Nothing here changes any file.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lga_worker.autocad.provenance import Entity, SourceStatus
from lga_worker.autocad.validation import _same_geometry

MIN_MATCH_RATIO = 0.5
BULK_DELETE = 5


def _equal(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, abs_tol=tol)
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a) == set(b) and all(_equal(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_equal(x, y, tol) for x, y in zip(a, b))
    return a == b


def _geometry_equal(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None, tol: float) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    try:
        return _same_geometry(a, b, tol) or (a.get("type") == b.get("type") and a.get("type") not in
                                             ("line", "polyline", "circle", "arc", "point", "segments") and _equal(a, b, tol))
    except (KeyError, TypeError, ValueError, IndexError):
        return _equal(a, b, tol)


def entity_changes(before: Entity, after: Entity, tol: float) -> list[dict[str, Any]]:
    """Field-level before/after for geometry, layer, properties, level, elevation, height, relationships."""
    changes: list[dict[str, Any]] = []
    if not _geometry_equal(before.geometry, after.geometry, tol):
        changes.append({"field": "geometry", "before": before.geometry, "after": after.geometry})
    for name in ("layer", "level_id", "units"):
        if getattr(before, name) != getattr(after, name):
            changes.append({"field": name, "before": getattr(before, name), "after": getattr(after, name)})
    for name in ("elevation", "height"):
        a, b = getattr(before, name), getattr(after, name)
        if (a is None) != (b is None) or (a is not None and not math.isclose(a, b, abs_tol=tol)):
            changes.append({"field": name, "before": a, "after": b})
    for key in sorted(set(before.properties) | set(after.properties)):
        a, b = before.properties.get(key), after.properties.get(key)
        if not _equal(a, b, tol):
            changes.append({"field": f"properties.{key}", "before": a, "after": b})
    if not _equal(before.relationships, after.relationships, tol):
        changes.append({"field": "relationships", "before": before.relationships, "after": after.relationships})
    return changes


@dataclass
class RevisionDiff:
    added: list[dict[str, Any]]
    modified: list[dict[str, Any]]
    deleted: list[dict[str, Any]]
    conflicted: list[dict[str, Any]]
    unchanged: int
    possible_renames: list[dict[str, str]]
    mapping_uncertain: bool
    mass_delete_requires_confirmation: bool
    match_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {"added": self.added, "modified": self.modified, "deleted": self.deleted,
                "conflicted": self.conflicted, "unchanged": self.unchanged,
                "possible_renames": self.possible_renames, "mapping_uncertain": self.mapping_uncertain,
                "mass_delete_requires_confirmation": self.mass_delete_requires_confirmation,
                "match_ratio": round(self.match_ratio, 4),
                "counts": {"added": len(self.added), "modified": len(self.modified), "deleted": len(self.deleted),
                           "conflicted": len(self.conflicted), "unchanged": self.unchanged}}


def compare_revisions(old: Sequence[Entity], new: Sequence[Entity], tolerance: float, *,
                      base: Sequence[Entity] | None = None, min_match_ratio: float = MIN_MATCH_RATIO,
                      bulk_delete: int = BULK_DELETE) -> RevisionDiff:
    if tolerance < 0:
        raise ValueError("tolerance must not be negative")
    old_by, new_by = {e.entity_id: e for e in old}, {e.entity_id: e for e in new}
    base_by = {e.entity_id: e for e in base} if base is not None else {}
    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    conflicted: list[dict[str, Any]] = []
    unchanged = 0
    for eid, before in old_by.items():
        after = new_by.get(eid)
        if after is None:
            deleted.append({"entity_id": eid, "entity_type": before.entity_type.value, "layer": before.layer,
                            "before": before.to_dict()})
            continue
        if before.entity_type != after.entity_type:
            conflicted.append({"entity_id": eid, "reason": "same id but different entity_type: id reused or mapping wrong",
                               "before": before.entity_type.value, "after": after.entity_type.value})
            continue
        if SourceStatus.CONFLICTED in (before.status, after.status):
            conflicted.append({"entity_id": eid, "reason": "entity is flagged CONFLICTED: a source disagreement is open",
                               "changes": entity_changes(before, after, tolerance)})
            continue
        changes = entity_changes(before, after, tolerance)
        if base is not None and eid in base_by:
            ours = {c["field"]: c for c in entity_changes(base_by[eid], before, tolerance)}
            theirs = {c["field"]: c for c in entity_changes(base_by[eid], after, tolerance)}
            clash = [f for f in ours if f in theirs and not _equal(ours[f]["after"], theirs[f]["after"], tolerance)]
            if clash:
                conflicted.append({"entity_id": eid, "reason": f"both revisions changed {clash} differently from the base",
                                   "fields": clash, "old": {f: ours[f]["after"] for f in clash},
                                   "new": {f: theirs[f]["after"] for f in clash}})
                continue
        if changes:
            modified.append({"entity_id": eid, "entity_type": after.entity_type.value, "layer": after.layer,
                             "changes": changes})
        else:
            unchanged += 1
    for eid, after in new_by.items():
        if eid not in old_by:
            added.append({"entity_id": eid, "entity_type": after.entity_type.value, "layer": after.layer,
                          "after": after.to_dict()})
    # a deleted + added pair with the same type, layer and geometry is probably the same object under a new id
    renames: list[dict[str, str]] = []
    for d in deleted:
        gone = old_by[d["entity_id"]]
        for a in added:
            now = new_by[a["entity_id"]]
            if gone.entity_type == now.entity_type and gone.layer == now.layer and gone.geometry and \
                    _geometry_equal(gone.geometry, now.geometry, tolerance):
                renames.append({"old_id": gone.entity_id, "new_id": now.entity_id})
                break
    matched = len(old_by) - len(deleted)
    ratio = matched / len(old_by) if old_by else 1.0
    uncertain = bool(old_by) and ratio < min_match_ratio
    needs_confirm = len(deleted) > bulk_delete or (uncertain and bool(deleted))
    return RevisionDiff(added, modified, deleted, conflicted, unchanged, renames, uncertain, needs_confirm, ratio)


def make_log_entries(diff: RevisionDiff, *, reason: str, source_request: str, actor: str,
                     sheets_affected: Sequence[str] = (), timestamp: str | None = None) -> list[dict[str, Any]]:
    """Revision-log rows: entity, before/after, reason, request source, time, who, sheets. Reason, source and actor are required."""
    for label, value in (("reason", reason), ("source_request", source_request), ("actor", actor)):
        if not str(value or "").strip():
            raise ValueError(f"revision log needs a non-empty {label}")
    stamp = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
    common = {"reason": reason, "source_request": source_request, "timestamp": stamp, "actor": actor,
              "sheets_affected": list(sheets_affected)}
    rows: list[dict[str, Any]] = []
    for a in diff.added:
        rows.append({"entity_id": a["entity_id"], "change": "added", "before": None, "after": a["after"], **common})
    for m in diff.modified:
        rows.append({"entity_id": m["entity_id"], "change": "modified",
                     "before": {c["field"]: c["before"] for c in m["changes"]},
                     "after": {c["field"]: c["after"] for c in m["changes"]}, **common})
    for d in diff.deleted:
        rows.append({"entity_id": d["entity_id"], "change": "deleted", "before": d["before"], "after": None,
                     "needs_confirmation": diff.mass_delete_requires_confirmation, **common})
    for c in diff.conflicted:
        rows.append({"entity_id": c["entity_id"], "change": "conflicted", "before": c.get("old", c.get("before")),
                     "after": c.get("new", c.get("after")), "detail": c["reason"], **common})
    return rows
