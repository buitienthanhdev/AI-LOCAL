"""Template versions, learning from real screenshots, and promotion gates (spec sections 12 and 13).

Layout under `<root>/<template>/`:
    best.json                 the version used for drawing (absent: the template built into the code)
    runs.jsonl                one line per REAL drawing: version, parameters, quality, screenshot, defects
    candidates/<id>.json      a changed parameter set proposed because of repeated, observed defects
    history/<version>.json    previous best versions (the way back)

Rules: a run is only recorded when it has a screenshot that exists and a quality report (nothing is learned from a plan
that was not executed); a candidate replaces the best version only when it passes every quality gate AND scores at least
as high; the code-built template always remains the offline fallback.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from lga_worker.paint import doraemon

ADJUSTMENTS: dict[str, tuple[str, float]] = {
    "silhouette_too_wide": ("hand_reach", -1.0), "silhouette_too_narrow": ("hand_reach", +1.0),
    "head_too_wide": ("head_aspect", -0.02), "head_too_narrow": ("head_aspect", +0.02),
    "figure_too_large": ("height_fraction", -0.01), "figure_too_small": ("height_fraction", +0.01),
}
REPEAT_THRESHOLD = 2
RECENT_RUNS = 12


def quality_score(q: dict[str, Any]) -> float:
    """0..1; 0 unless the drawing was executed and safe. Never a likeness score on its own."""
    if not q.get("DRAW_EXECUTION_PASS"):
        return 0.0
    visual = q.get("visual_quality", {})
    fidelity = q.get("character_fidelity", {})
    cov = visual.get("worst_stroke_coverage")
    v = (1.0 if cov is None else float(cov)) if visual.get("ok") else 0.0
    errs = [e for e in (fidelity.get("landmark_errors") or {}).values() if e is not None]
    f = 0.0
    if fidelity.get("ok"):
        f = 1.0 - min(1.0, (sum(errs) / len(errs)) * 10.0 if errs else 0.0)
    return round(0.3 + 0.3 * v + 0.4 * f, 4)


def defects_from_quality(q: dict[str, Any]) -> list[str]:
    """Defect codes observed on the screenshot (what the learning step may act on)."""
    out: list[str] = []
    fid = q.get("character_fidelity", {})
    for c in fid.get("checks", []):
        if c.get("ok"):
            continue
        name, value, expected = c["name"], c["value"], c["expected"]
        if name == "silhouette_aspect" and isinstance(value, (int, float)) and isinstance(expected, list):
            out.append("silhouette_too_wide" if value > expected[1] else "silhouette_too_narrow")
        elif name == "figure_height_fraction" and isinstance(value, (int, float)) and isinstance(expected, list):
            out.append("figure_too_large" if value > expected[1] else "figure_too_small")
        elif name == "landmark_error":
            out.append("landmark_off")
    vis = q.get("visual_quality", {})
    if vis.get("spill_ratio", 0) > 0.01:
        out.append("ink_spill")
    if vis.get("worst_stroke_coverage") is not None and vis["worst_stroke_coverage"] < 0.9:
        out.append("broken_outline")
    if (vis.get("stray_specks") or {}).get("count"):
        out.append("stray_dots")
    return out


class TemplateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        return self.root / name

    # ------------------------------------------------------------------------------------------- best version
    def best(self, name: str) -> dict[str, Any] | None:
        path = self._dir(name) / "best.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def params(self, name: str) -> doraemon.Params:
        """The best promoted parameters, or the template built into the code (offline fallback) when there are none or
        the stored ones are outside the allowed ratios."""
        best = self.best(name)
        if best:
            try:
                p = doraemon.Params.from_dict(best["params"])
                if not p.out_of_range():
                    return p
            except (KeyError, TypeError, ValueError):
                pass
        return doraemon.Params()

    # ---------------------------------------------------------------------------------------------- real runs
    def record_run(self, name: str, *, version: str, params: dict[str, Any], quality: dict[str, Any],
                   screenshot: str, reference_status: str = "OFFLINE_TEMPLATE") -> dict[str, Any]:
        if not screenshot or not Path(screenshot).is_file():
            raise ValueError("a run without an existing screenshot is not recorded: nothing is learned from a plan "
                             "that was not executed")
        if not quality or "DRAW_EXECUTION_PASS" not in quality:
            raise ValueError("a run without a quality report is not recorded")
        row = {"id": uuid.uuid4().hex[:12], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "template": name, "version": version,
               "params": params, "screenshot": screenshot, "reference_status": reference_status,
               "draw_execution_pass": bool(quality["DRAW_EXECUTION_PASS"]),
               "visual_quality_pass": bool(quality["VISUAL_QUALITY_PASS"]), "score": quality_score(quality),
               "defects": defects_from_quality(quality)}
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "runs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def runs(self, name: str, limit: int = RECENT_RUNS) -> list[dict[str, Any]]:
        path = self._dir(name) / "runs.jsonl"
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[-limit:]

    def repeated_defects(self, name: str, threshold: int = REPEAT_THRESHOLD) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.runs(name):
            for d in set(row.get("defects", [])):
                counts[d] = counts.get(d, 0) + 1
        return {d: n for d, n in counts.items() if n >= threshold}

    # ---------------------------------------------------------------------------------------------- candidates
    def propose_candidate(self, name: str) -> dict[str, Any] | None:
        """A parameter set that corrects the defects seen at least twice (None when there is nothing to correct). The
        change is clamped to the allowed ratios and written as a candidate; it is NOT used for drawing yet."""
        repeated = self.repeated_defects(name)
        changes = {d: ADJUSTMENTS[d] for d in repeated if d in ADJUSTMENTS}
        if not changes:
            return None
        base = self.params(name)
        values = base.to_dict()
        for _, (param, delta) in changes.items():
            values[param] = values[param] + delta
        candidate = doraemon.Params.from_dict(values).clamped()
        if candidate == base:
            return None
        cid = uuid.uuid4().hex[:10]
        doc = {"id": cid, "template": name, "base_version": (self.best(name) or {}).get("version", doraemon.VERSION),
               "params": candidate.to_dict(), "because": {d: repeated[d] for d in changes}, "status": "candidate",
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        d = self._dir(name) / "candidates"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return doc

    def candidate(self, name: str, cid: str) -> dict[str, Any] | None:
        path = self._dir(name) / "candidates" / f"{cid}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def promote(self, name: str, cid: str, *, quality: dict[str, Any], constraints_ok: bool) -> tuple[bool, list[str]]:
        """Make a candidate the best version, only when a REAL drawing made with it passed every gate and did not score
        lower than the current best. Returns (promoted, reasons it was refused)."""
        doc = self.candidate(name, cid)
        if doc is None:
            return False, [f"no candidate {cid}"]
        reasons: list[str] = []
        if not constraints_ok:
            reasons.append("the template constraints do not all pass")
        if not quality.get("DRAW_EXECUTION_PASS"):
            reasons.append("DRAW_EXECUTION_PASS is false")
        if not quality.get("VISUAL_QUALITY_PASS"):
            reasons.append("VISUAL_QUALITY_PASS is false")
        score = quality_score(quality)
        best = self.best(name)
        if best and score < float(best.get("score", 0.0)):
            reasons.append(f"score {score} is lower than the current best {best.get('score')}")
        if reasons:
            return False, reasons
        d = self._dir(name)
        if best:
            (d / "history").mkdir(parents=True, exist_ok=True)
            (d / "history" / f"{best.get('version', 'unknown')}-{best.get('promoted_at', 'x').replace(':', '')}.json").write_text(
                json.dumps(best, ensure_ascii=False, indent=1), encoding="utf-8")
        version = f"{doraemon.VERSION}+{cid}"
        new = {"version": version, "params": doc["params"], "score": score, "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "candidate": cid, "because": doc.get("because", {}), "reviewed": False}
        (d / "best.json").write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        doc["status"] = "promoted"
        (d / "candidates" / f"{cid}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return True, []
