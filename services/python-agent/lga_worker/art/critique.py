"""Objective self-review of a picture that was written by a model (SVG): what a person would notice at a glance.

Every finding is measurable on the Scene (no opinion): empty or tiny subject, flat colour, no contrast, no details, no
outlines where the rest has them, no ground under a standing subject. The planner is told the findings and the fix, and
is expected to correct the SVG once before it reports the picture as finished.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from lga_worker.art.scene import Scene, luminance


@dataclass
class Finding:
    code: str
    severity: str  # "fix" | "note"
    message: str
    fix: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "fix": self.fix}


def _poly_area(points: tuple[tuple[float, float], ...]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2.0


def critique(scene: Scene) -> list[Finding]:
    out: list[Finding] = []
    sheet = scene.width * scene.height
    filled = [s for s in scene.shapes if s.fill is not None and s.closed and len(s.points) >= 3]
    if not filled:
        return [Finding("NO_FILLS", "fix", "nothing is filled: the picture is only lines",
                        "give the parts of the subject filled shapes in their real colours")]
    areas = [_poly_area(s.points) for s in filled]
    # coverage of the sheet by the union approximated through the bounding box of the non-background shapes
    xs = [p[0] for s in filled for p in s.points]
    ys = [p[1] for s in filled for p in s.points]
    box = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if box < 0.18 * sheet:
        out.append(Finding("SUBJECT_SMALL", "fix", f"the drawing occupies only {box / sheet:.0%} of the sheet",
                           "scale the subject up so it fills most of the height, and add its surroundings"))
    colours = {s.fill for s in filled}
    if len(colours) < 4:
        out.append(Finding("FEW_COLOURS", "fix", f"only {len(colours)} fill colour(s)",
                           "use real colours for every part: shades of the main colour for shading, a second tone for details"))
    lums = [luminance(c) for c in colours]
    if max(lums) - min(lums) < 0.3:
        out.append(Finding("FLAT_CONTRAST", "fix", "the tones are all close: the picture will look washed out",
                           "add darker shadow shapes and lighter highlight shapes"))
    big = sum(1 for a in areas if a > 0.04 * sheet)
    small = sum(1 for a in areas if a < 0.004 * sheet)
    if small < max(2, len(filled) // 8):
        out.append(Finding("NO_DETAILS", "fix", f"only {small} small detail shape(s)",
                           "add small parts: eyes with pupils and highlights, nose, mouth, buttons, texture marks, highlights"))
    if big == 0:
        out.append(Finding("NO_MASSES", "note", "no large shapes: the picture may look scattered",
                           "give the subject one or two large body masses and build the details on them"))
    outlined = sum(1 for s in scene.shapes if s.stroke is not None and s.closed)
    if 0 < outlined < 0.4 * len(filled):
        out.append(Finding("MIXED_OUTLINES", "note", "only some shapes have outlines",
                           "give every part of the subject the same kind of outline (or none)"))
    lowest = max(max(p[1] for p in s.points) for s in filled)
    ground = [s for s in filled if min(p[1] for p in s.points) > 0.6 * scene.height and _poly_area(s.points) > 0.02 * sheet]
    if lowest < 0.8 * scene.height and not ground and len(filled) > 12:
        out.append(Finding("NO_GROUND", "note", "nothing grounds the subject",
                           "add a ground, floor or a soft shadow shape under it"))
    cx = sum((min(p[0] for p in s.points) + max(p[0] for p in s.points)) / 2 * a for s, a in zip(filled, areas)) / (sum(areas) or 1)
    if abs(cx - scene.width / 2) > 0.28 * scene.width:
        out.append(Finding("OFF_CENTRE", "note", f"the visual weight sits far from the centre ({cx / scene.width:.0%} of the width)",
                           "centre the subject unless the composition is deliberately off-centre"))
    return out


def summary(findings: list[Finding]) -> list[str]:
    return [f"REVIEW {f.code}: {f.message}. FIX: {f.fix}" for f in findings if f.severity == "fix"] + \
           [f"review {f.code}: {f.message}" for f in findings if f.severity == "note"]
