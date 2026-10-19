"""Sheet set, paper-space layouts, title blocks and viewport geometry for a generated house (spec 12 and 13). Pure Python.

The model space holds the plans, elevations, section and tables 1:1 in metres (see `houseviews`). A *sheet* is a paper-space
layout named after its sheet code (A-101 plan of floor 1, A-201 front elevation, A-301 section, A-501 door/window
tables, ...). The codes are an office configuration (`sheet_codes`), not a universal rule. This module computes, without
AutoCAD:

* the sheet list with the model-space window each sheet shows and the viewport scale (an exact 1:N, chosen as the largest
  standard scale from the requested one that fits the paper; an adjusted scale is reported, never hidden);
* the paper size / orientation in millimetres, the frame, the viewport rectangle and the title block as `Prim`s in
  paper millimetres.

Title block policy (spec 13): project, sheet code, sheet name, scale, date, revision and status fields are written; the
people fields (prepared / checked / approved, signatures) are NEVER written by this code: they are drawn as visibly empty
cells. Client and design-office names are written only when the caller supplies them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from lga_worker.autocad import houseviews
from lga_worker.autocad.housemodel import (
    SIDES,
    HouseModel,
    floor_level_id,
    plan_sheet_code,
)
from lga_worker.autocad.houseviews import TL, Prim

PAPERS_MM: dict[str, tuple[float, float]] = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
                                             "A1": (841.0, 594.0)}  # landscape (width, height)
STANDARD_SCALES = (20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500)
TTLB = "A-ANNO-TTLB"
VIEWPORT_LAYER = "A-NPLT"
BAND_MM = 60.0  # title band height for portrait paper
STRIP_MM = 70.0  # title strip width for landscape paper
FRAME_LEFT, FRAME_OTHER = 20.0, 5.0

# (key, Vietnamese label, who may fill it)  filled: tool | caller | never
TITLE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("project", "Dự án", "caller"),
    ("client", "Chủ đầu tư", "caller"),
    ("designer", "Đơn vị thiết kế", "caller"),
    ("prepared_by", "Người lập", "never"),
    ("checked_by", "Kiểm tra", "never"),
    ("approved_by", "Phê duyệt", "never"),
    ("sheet_code", "Mã bản vẽ", "tool"),
    ("scale", "Tỷ lệ", "tool"),
    ("date", "Ngày", "caller"),
    ("revision", "Revision", "caller"),
    ("status", "Trạng thái", "caller"),
)
CALLER_FIELDS = tuple(k for k, _l, who in TITLE_FIELDS if who == "caller")
NEVER_FIELDS = tuple(k for k, _l, who in TITLE_FIELDS if who == "never")
REQUIRED_FILLED = ("project", "sheet_code", "scale", "sheet_title")  # must carry a value in every title block
DEFAULT_STATUS = "DRAFT"


class LayoutError(ValueError):
    pass


def parse_scale(text: str | int | float) -> int:
    """'1:100' -> 100. Only 1:N scales (reductions) are accepted."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        n = float(text)
    else:
        parts = str(text).strip().replace(" ", "").split(":")
        if len(parts) != 2 or parts[0] != "1":
            raise LayoutError(f"scale must look like '1:100', got {text!r}")
        try:
            n = float(parts[1])
        except ValueError as exc:
            raise LayoutError(f"scale must look like '1:100', got {text!r}") from exc
    if not math.isfinite(n) or n < 1 or n > 5000 or abs(n - round(n)) > 1e-9:
        raise LayoutError(f"scale denominator must be a whole number from 1 to 5000, got {text!r}")
    return int(round(n))


def scale_text(denominator: int) -> str:
    return f"1:{denominator}"


def paper_mm(paper: str, orientation: str) -> tuple[float, float]:
    key = str(paper).upper()
    if key not in PAPERS_MM:
        raise LayoutError(f"paper must be one of {sorted(PAPERS_MM)}, got {paper!r}")
    if orientation not in ("landscape", "portrait"):
        raise LayoutError(f"orientation must be 'landscape' or 'portrait', got {orientation!r}")
    w, h = PAPERS_MM[key]
    return (w, h) if orientation == "landscape" else (h, w)


def mm_per_model_unit(denominator: int, unit_mm: float = 1000.0) -> float:
    """Paper mm per drawing unit at 1:N. The model is in metres (unit_mm = 1000): 1:100 -> 10 mm per metre."""
    return unit_mm / denominator


@dataclass
class SheetSpec:
    code: str
    title: str
    kind: str  # plan | roof_plan | elevation | section | schedule
    window: tuple[float, float, float, float]  # model space xmin, ymin, xmax, ymax (metres) the viewport shows
    requested_scale: int
    scale: int = 0
    scale_adjusted: bool = False
    level_id: str | None = None
    view_ids: list[str] = field(default_factory=list)

    @property
    def scale_label(self) -> str:
        return scale_text(self.scale or self.requested_scale)


@dataclass
class Frame:
    paper: str
    orientation: str
    width: float
    height: float
    frame: tuple[float, float, float, float]
    viewport: tuple[float, float, float, float]  # x0, y0, x1, y1 in paper mm
    title: tuple[float, float, float, float]  # title block region


def frame_for(paper: str, orientation: str) -> Frame:
    w, h = paper_mm(paper, orientation)
    f = (FRAME_LEFT, FRAME_OTHER, w - FRAME_OTHER, h - FRAME_OTHER)
    if orientation == "landscape":
        title = (f[2] - STRIP_MM, f[1], f[2], f[3])
        vp = (f[0] + 5.0, f[1] + 5.0, title[0] - 5.0, f[3] - 5.0)
    else:
        title = (f[0], f[1], f[2], f[1] + BAND_MM)
        vp = (f[0] + 5.0, title[3] + 5.0, f[2] - 5.0, f[3] - 5.0)
    return Frame(str(paper).upper(), orientation, w, h, f, vp, title)


def fit_scale(window: tuple[float, float, float, float], frame: Frame, requested: int, margin_mm: float = 4.0,
              unit_mm: float = 1000.0) -> tuple[int, bool]:
    """The requested 1:N when the window fits the viewport, else the next coarser standard scale that does."""
    vx0, vy0, vx1, vy1 = frame.viewport
    avail_w, avail_h = vx1 - vx0 - 2 * margin_mm, vy1 - vy0 - 2 * margin_mm
    ww, wh = window[2] - window[0], window[3] - window[1]
    need = max(ww * unit_mm / avail_w, wh * unit_mm / avail_h)
    if requested >= need - 1e-9:
        return requested, False
    for cand in STANDARD_SCALES:
        if cand >= requested and cand >= need - 1e-9:
            return cand, True
    raise LayoutError(f"the content ({ww:.1f} x {wh:.1f} m) does not fit {frame.paper} {frame.orientation} even at "
                      f"1:{STANDARD_SCALES[-1]}; choose a larger paper")


def viewport_rect_for(spec: SheetSpec, frame: Frame, unit_mm: float = 1000.0) -> tuple[float, float, float, float]:
    """Paper rectangle (mm) of the viewport: exactly the content window at the sheet's scale, centred in the drawing area
    of the sheet. The viewport shows nothing but that window, so neighbouring views never show through."""
    vx0, vy0, vx1, vy1 = frame.viewport
    w = min((spec.window[2] - spec.window[0]) * unit_mm / spec.scale, vx1 - vx0)
    h = min((spec.window[3] - spec.window[1]) * unit_mm / spec.scale, vy1 - vy0)
    cx, cy = (vx0 + vx1) / 2, (vy0 + vy1) / 2
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def viewport_window_for(spec: SheetSpec, rect: Sequence[float], unit_mm: float = 1000.0) -> tuple[float, float, float, float]:
    """Model-space rectangle that fills the viewport rectangle `rect` exactly at the sheet's scale, centred on the content."""
    half_w = (rect[2] - rect[0]) / 2 * spec.scale / unit_mm
    half_h = (rect[3] - rect[1]) / 2 * spec.scale / unit_mm
    cx, cy = (spec.window[0] + spec.window[2]) / 2, (spec.window[1] + spec.window[3]) / 2
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h


# --------------------------------------------------------------------------- the sheet list

def _grow(box: tuple[float, float, float, float], by: float) -> tuple[float, float, float, float]:
    return box[0] - by, box[1] - by, box[2] + by, box[3] + by


def _union(boxes: Sequence[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _shift(box: tuple[float, float, float, float], origin: tuple[float, float]) -> tuple[float, float, float, float]:
    return box[0] + origin[0], box[1] + origin[1], box[2] + origin[0], box[3] + origin[1]


def sheet_specs(model: HouseModel, *, plan_origin: tuple[float, float] = (0.0, 0.0),
                scales: Mapping[str, str] | None = None, sheet_codes: Mapping[str, str] | None = None,
                extents: Mapping[str, Sequence[float]] | None = None, include: Sequence[str] | None = None
                ) -> list[SheetSpec]:
    """Every sheet of the house: plan per floor, roof plan (multi-storey), four elevations, section A-A, tables.

    `scales` maps a sheet kind (plan, roof_plan, elevation, section, schedule) or a sheet code to '1:N' (defaults 1:100,
    tables 1:50). `sheet_codes` maps a default code (A-101, ...) to the office code. `extents` maps a view id to a
    measured [xmin, ymin, xmax, ymax] in model space (from the draw tools' data.views[].extent) and wins over the
    default placement of the draw tools.
    """
    scales = dict(scales or {})
    codes = dict(sheet_codes or {})
    extents = dict(extents or {})
    default_scale = {"plan": 100, "roof_plan": 100, "elevation": 100, "section": 100, "schedule": 50}

    def code_of(default: str) -> str:
        return codes.get(default, default)

    def wanted(kind: str, code: str, default_code: str) -> int:
        raw = scales.get(code) or scales.get(default_code) or scales.get(kind)
        return parse_scale(raw) if raw else default_scale[kind]

    def window_of(view_ids: Sequence[str], fallback: tuple[float, float, float, float], margin: float = 1.5
                  ) -> tuple[float, float, float, float]:
        boxes = [tuple(extents[v]) for v in view_ids if v in extents]  # type: ignore[misc]
        return _grow(_union(boxes), margin) if boxes else fallback

    specs: list[SheetSpec] = []
    w, d = model.width, model.depth
    for n in range(1, model.floors + 1):
        dflt = plan_sheet_code(n)
        org = houseviews.floor_plan_origin(model, n, plan_origin)
        # plan, dimension lines, title above the plan and the area table to its right (about 12 m wide)
        fallback = _shift((-2.8, -5.2, w + 13.0, d + 2.8), org)
        vids = [f"PLAN-F{n}"]
        win = window_of(vids, fallback)
        if f"PLAN-F{n}" in extents:  # the measured box is the walls only; keep the table and dimension margins
            win = _union([win, fallback])
        specs.append(SheetSpec(code_of(dflt), f"MẶT BẰNG TẦNG {n}" if model.floors > 1 else "MẶT BẰNG TẦNG 1", "plan", win,
                               wanted("plan", code_of(dflt), dflt), level_id=floor_level_id(n), view_ids=vids))
    if model.floors > 1:
        dflt = houseviews.roof_plan_sheet_code(model)
        org = houseviews.floor_plan_origin(model, model.floors + 1, plan_origin)
        ov = model.roof["overhang"]
        fallback = _shift((-ov - 2.0, model.porch - ov - 3.0, w + ov + 2.0, d + ov + 2.0), org)
        specs.append(SheetSpec(code_of(dflt), "MẶT BẰNG MÁI", "roof_plan", window_of([houseviews.ROOF_PLAN_ID], fallback),
                               wanted("roof_plan", code_of(dflt), dflt), view_ids=[houseviews.ROOF_PLAN_ID]))
    for side in SIDES:
        view = houseviews.elevation_view(model, side)
        org = houseviews.default_origin(model, side, plan_origin)
        fallback = _grow(_shift(view.bbox, org), 1.0)
        dflt = houseviews.SIDE_SHEET[side]
        specs.append(SheetSpec(code_of(dflt), houseviews.SIDE_TITLE[side], "elevation", window_of([view.id], fallback),
                               wanted("elevation", code_of(dflt), dflt), view_ids=[view.id]))
    cut = houseviews.check_cut(model, houseviews.default_cut(model))
    sec = houseviews.section_view(model, cut)
    org = houseviews.default_origin(model, "section", plan_origin)
    dflt = houseviews.SECTION_SHEET
    specs.append(SheetSpec(code_of(dflt), f"MẶT CẮT {cut.code}", "section",
                           window_of([sec.id], _grow(_shift(sec.bbox, org), 1.0)),
                           wanted("section", code_of(dflt), dflt), view_ids=[sec.id]))
    tables = houseviews.schedule_views(model)
    sched_origin = houseviews.default_origin(model, "schedule", plan_origin)
    for dflt, title, kinds in ((houseviews.DOOR_SCHEDULE_SHEET, "BẢNG CỬA ĐI VÀ CỬA SỔ", ("door", "window")),
                               (houseviews.ROOM_SCHEDULE_SHEET, "BẢNG PHÒNG VÀ DIỆN TÍCH", ("room",))):
        group = [v for v in tables if v.record["schedule"] in kinds]
        fallback = _grow(_shift(_union([v.bbox for v in group]), sched_origin), 0.8)
        specs.append(SheetSpec(code_of(dflt), title, "schedule", window_of([v.id for v in group], fallback, 0.8),
                               wanted("schedule", code_of(dflt), dflt), view_ids=[v.id for v in group]))
    if include:
        keep = set(include)
        specs = [s for s in specs if s.code in keep]
        missing = keep - {s.code for s in specs}
        if missing:
            raise LayoutError(f"unknown sheet code(s) {sorted(missing)}; this house has "
                              f"{[s.code for s in sheet_specs(model, sheet_codes=sheet_codes)]}")
    codes_seen = [s.code for s in specs]
    if len(set(codes_seen)) != len(codes_seen):
        raise LayoutError(f"sheet codes are not unique: {codes_seen}")
    return specs


def fit_all(specs: Sequence[SheetSpec], frame: Frame) -> list[SheetSpec]:
    for s in specs:
        s.scale, s.scale_adjusted = fit_scale(s.window, frame, s.requested_scale)
    return list(specs)


# --------------------------------------------------------------------------- title block and frame prims (paper mm)

def title_values(spec: SheetSpec, block: Mapping[str, Any] | None) -> dict[str, str]:
    """The text of every title block field. People fields are always empty; unknown keys are refused."""
    block = dict(block or {})
    unknown = set(block) - set(CALLER_FIELDS)
    if unknown:
        never = sorted(unknown & set(NEVER_FIELDS))
        if never:
            raise LayoutError(f"title block field(s) {never} are never filled by this tool (names, signatures and "
                              "certificates must be added by the responsible person)")
        raise LayoutError(f"unknown title block field(s) {sorted(unknown)}; allowed: {list(CALLER_FIELDS)}")
    values = {k: "" for k, _l, _w in TITLE_FIELDS}
    for k in CALLER_FIELDS:
        raw = block.get(k)
        if raw is not None and str(raw).strip():
            values[k] = str(raw).strip()
    if values["date"].lower() == "today":
        values["date"] = date.today().strftime("%d/%m/%Y")
    if not values["status"]:
        values["status"] = DEFAULT_STATUS
    values["sheet_code"] = spec.code
    values["scale"] = spec.scale_label
    return values


def frame_prims(spec: SheetSpec, frame: Frame, block: Mapping[str, Any] | None) -> tuple[list[Prim], dict[str, Any]]:
    """Frame, title block and the viewport outline hint as paper-space primitives, plus the measured title block."""
    values = title_values(spec, block)
    prims: list[Prim] = []
    x0, y0, x1, y1 = frame.frame
    prims.append(Prim("poly", TTLB, (x0, y0, x1, y0, x1, y1, x0, y1), closed=True, semantic="frame", ref=spec.code))
    tx0, ty0, tx1, ty1 = frame.title
    prims.append(Prim("poly", TTLB, (tx0, ty0, tx1, ty0, tx1, ty1, tx0, ty1), closed=True, semantic="title_box",
                      ref=spec.code))
    cells: list[tuple[str, str, tuple[float, float, float, float]]] = []
    if frame.orientation == "landscape":
        cur = ty1
        title_h = 34.0
        prims.append(Prim("text", TTLB, (tx0 + 3.0, cur - 8.0), text=spec.title, height=5.0, attach=TL,
                          semantic="tb_value:sheet_title", ref=spec.code, data={"width": tx1 - tx0 - 6.0}))
        prims.append(Prim("text", TTLB, (tx0 + 1.5, cur - 1.5), text="Tên bản vẽ", height=1.8, attach=TL,
                          semantic="tb_label:sheet_title", ref=spec.code))
        cur -= title_h
        prims.append(Prim("line", TTLB, (tx0, cur, tx1, cur), semantic="tb_rule", ref=spec.code))
        row_h = 15.0
        half = (tx1 - tx0) / 2
        order = [("project",), ("client",), ("designer",), ("prepared_by", "checked_by"), ("approved_by",),
                 ("sheet_code", "scale"), ("date", "revision"), ("status",)]
        for row in order:
            n = len(row)
            for i, key in enumerate(row):
                cx0 = tx0 + i * (tx1 - tx0) / n
                cx1 = tx0 + (i + 1) * (tx1 - tx0) / n
                cells.append((key, "", (cx0, cur - row_h, cx1, cur)))
            prims.append(Prim("line", TTLB, (tx0, cur - row_h, tx1, cur - row_h), semantic="tb_rule", ref=spec.code))
            if n == 2:
                prims.append(Prim("line", TTLB, (tx0 + half, cur, tx0 + half, cur - row_h), semantic="tb_rule", ref=spec.code))
            cur -= row_h
    else:
        cols = 4
        cw = (tx1 - tx0) / cols
        row_h = 15.0
        rows = [("project", "client", "designer", "status"), ("prepared_by", "checked_by", "approved_by", "revision"),
                ("sheet_code", "scale", "date", "")]
        cur = ty1
        for r_i, row in enumerate(rows):
            for c_i, key in enumerate(row):
                if key:
                    cells.append((key, "", (tx0 + c_i * cw, cur - row_h, tx0 + (c_i + 1) * cw, cur)))
            prims.append(Prim("line", TTLB, (tx0, cur - row_h, tx1, cur - row_h), semantic="tb_rule", ref=spec.code))
            cur -= row_h
        for c_i in range(1, cols):
            prims.append(Prim("line", TTLB, (tx0 + c_i * cw, ty1, tx0 + c_i * cw, ty1 - 3 * row_h), semantic="tb_rule",
                              ref=spec.code))
        prims.append(Prim("text", TTLB, (tx0 + 3.0, cur - 8.0), text=spec.title, height=5.0, attach=TL,
                          semantic="tb_value:sheet_title", ref=spec.code, data={"width": tx1 - tx0 - 6.0}))
        prims.append(Prim("text", TTLB, (tx0 + 1.5, cur - 1.5), text="Tên bản vẽ", height=1.8, attach=TL,
                          semantic="tb_label:sheet_title", ref=spec.code))
    labels = {k: lab for k, lab, _w in TITLE_FIELDS}
    for key, _unused, (cx0, cy0, cx1, cy1) in cells:
        prims.append(Prim("text", TTLB, (cx0 + 1.5, cy1 - 1.5), text=labels[key], height=1.8, attach=TL,
                          semantic=f"tb_label:{key}", ref=spec.code))
        if values[key]:
            prims.append(Prim("text", TTLB, (cx0 + 1.5, cy0 + 2.0), text=values[key], height=3.2, attach=BL_,
                              semantic=f"tb_value:{key}", ref=spec.code))
    measured: dict[str, Any] = {}
    for key, label, _w in TITLE_FIELDS:
        measured[key] = {"label": label, "value": values[key], "blank": not values[key]}
    measured["sheet_title"] = {"label": "Tên bản vẽ", "value": spec.title, "blank": not spec.title}
    return prims, measured


BL_ = 7  # AutoCAD MText attachment point: bottom left


def view_frame_record(spec: SheetSpec, frame: Frame, viewport_mm: Sequence[float] | None = None) -> dict[str, Any]:
    return {"code": spec.code, "title": spec.title, "kind": spec.kind, "paper": frame.paper,
            "orientation": frame.orientation, "paper_mm": [frame.width, frame.height],
            "viewport_mm": list(viewport_mm or frame.viewport), "scale": spec.scale_label, "scale_denominator": spec.scale,
            "requested_scale": scale_text(spec.requested_scale), "scale_adjusted": spec.scale_adjusted,
            "window": [round(v, 4) for v in spec.window], "level_id": spec.level_id, "view_ids": spec.view_ids}


def expected_sheet_index(specs: Sequence[SheetSpec], frame: Frame | None = None) -> list[dict[str, Any]]:
    """`meta.sheet_index` for the validation model: what the drawing set declares."""
    out = []
    for s in specs:
        row: dict[str, Any] = {"code": s.code, "title": s.title, "kind": s.kind, "scale": s.scale_label,
                               "scale_denominator": s.scale, "layout": s.code}
        if frame is not None:
            row.update({"paper": frame.paper, "orientation": frame.orientation,
                        "paper_mm": [frame.width, frame.height]})
        out.append(row)
    return out


def title_keys() -> list[str]:
    return [k for k, _l, _w in TITLE_FIELDS] + ["sheet_title"]


def paper_name(paper_mm: Sequence[float]) -> str | None:
    for name, (w, h) in PAPERS_MM.items():
        if (abs(paper_mm[0] - w) <= 1.0 and abs(paper_mm[1] - h) <= 1.0) or (abs(paper_mm[0] - h) <= 1.0 and abs(paper_mm[1] - w) <= 1.0):
            return name
    return None


def layouts_meta(infos: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Measured layouts in the form of `meta.layouts` (the keys of the older PLOT.LAYOUT_SETUP rule included)."""
    out = []
    for i in infos:
        vps = [v for v in i.get("viewports", []) if v.get("role") == "viewport" or v.get("marked")]
        den = vps[0].get("scale_denominator") if vps else None
        block = {k: {"label": v.get("label", ""), "value": v.get("value", "")} for k, v in (i.get("title_block") or {}).items()}
        out.append({"name": i["name"], "media": i.get("media"), "paper_mm": i.get("paper_mm"),
                    "orientation": i.get("orientation"), "paper": paper_name(i.get("paper_mm") or [0, 0]),
                    "viewports": [dict(v) for v in i.get("viewports", [])],
                    "scale": f"1:{den:g}" if den else None, "viewport_locked": bool(vps) and all(v.get("locked") is True for v in vps),
                    "title_block": block, "entities": i.get("entities"), "marked_entities": i.get("marked_entities")})
    return out


def compare_counts(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[str]:
    """Differences between two `provider.drawing_counts` results (empty list = identical)."""
    if not before:
        return ["nothing to compare with"]
    diffs: list[str] = []
    if before.get("model_space") != after.get("model_space"):
        diffs.append(f"model space has {after.get('model_space')} entities after re-opening, {before.get('model_space')} before")
    for kind in sorted(set(before.get("model_by_object", {})) | set(after.get("model_by_object", {}))):
        b, a = before.get("model_by_object", {}).get(kind, 0), after.get("model_by_object", {}).get(kind, 0)
        if a != b:
            diffs.append(f"{kind}: {a} after, {b} before")
    if before.get("model_marked") != after.get("model_marked"):
        diffs.append(f"marked entities: {after.get('model_marked')} after, {before.get('model_marked')} before")
    bl, al = before.get("layouts", {}), after.get("layouts", {})
    if set(bl) != set(al):
        diffs.append(f"layouts {sorted(al)} after, {sorted(bl)} before")
    for name in sorted(set(bl) & set(al)):
        for key in ("entities", "marked", "media"):
            if bl[name].get(key) != al[name].get(key):
                diffs.append(f"layout {name} {key}: {al[name].get(key)} after, {bl[name].get(key)} before")
        if bl[name].get("paper_mm") != al[name].get("paper_mm"):
            diffs.append(f"layout {name} paper: {al[name].get('paper_mm')} after, {bl[name].get('paper_mm')} before")
        bv = [(v.get("scale_denominator"), v.get("locked")) for v in bl[name].get("viewports", [])]
        av = [(v.get("scale_denominator"), v.get("locked")) for v in al[name].get("viewports", [])]
        if bv != av:
            diffs.append(f"layout {name} viewports: {av} after, {bv} before")
    return diffs
