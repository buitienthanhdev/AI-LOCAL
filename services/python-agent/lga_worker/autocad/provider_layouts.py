"""Paper-space layouts, viewports, plotting and the re-open check of the AutoCAD provider. STA thread only.

A mixin of `AutoCadProvider`: it relies on the provider's `_writable_document`, `_application`, `_idempotent`, `_guard`,
`_find_marked`, `_apply_if_exists`, `_wait_for_file`, `_find_layer` and `settings`. Every COM call that can be rejected
as busy goes through `_idempotent`; calls that create objects are made once.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lga_worker.autocad import com, com_extra
from lga_worker.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

AC_PLOT_TYPE_LAYOUT = 5
AC_SCALE_1_TO_1 = 16
AC_MILLIMETRES = 1
TEXT_STYLE = "LGA_VN"
PLOT_STYLE_PREFERENCE = ("monochrome.ctb",)
OPEN_POLL_S = 0.2


class LayoutMixin:
    """Methods added to AutoCadProvider (see provider.py)."""

    # these come from AutoCadProvider
    settings: Any

    # ------------------------------------------------------------------ helpers

    def _find_layout(self, doc: Any, name: str, deadline: float) -> Any | None:
        def find() -> Any | None:
            layouts = doc.Layouts
            for index in range(int(layouts.Count)):
                layout = layouts.call("Item", index)
                if str(layout.Name).casefold() == name.casefold():
                    return layout
            return None

        return self._idempotent(f"find layout {name!r}", find, deadline)  # type: ignore[attr-defined]

    def _ensure_text_style(self, doc: Any, deadline: float) -> None:
        style = self._idempotent("add text style", lambda: doc.TextStyles.call("Add", TEXT_STYLE), deadline)  # type: ignore[attr-defined]
        try:
            style.call("SetFont", "Arial", False, False, 0, 34)
        except Exception as exc:  # keeps AutoCAD's default font
            logger.warning("layout: could not set the Arial font: %s", com.describe(exc) if com.is_com_error(exc) else exc)

    def _ensure_layers(self, doc: Any, layers: Mapping[str, tuple[str, int]], non_plotting: str | None,
                       deadline: float) -> list[str]:
        created: list[str] = []
        for std, (office, color) in layers.items():
            existed = self._find_layer(doc, office, deadline) is not None  # type: ignore[attr-defined]
            layer = self._idempotent(f"add layer {office}", lambda n=office: doc.Layers.call("Add", n), deadline)  # type: ignore[attr-defined]
            if not existed:
                self._idempotent("set layer colour", lambda lay=layer, c=color: setattr(lay, "color", c), deadline)  # type: ignore[attr-defined]
                created.append(office)
            if non_plotting is not None and std == non_plotting:
                try:
                    self._idempotent("make the viewport layer non-plotting",  # type: ignore[attr-defined]
                                     lambda lay=layer: setattr(lay, "Plottable", False), deadline)
                except Exception as exc:
                    logger.warning("layout: could not set %s non-plotting: %s", office, exc)
        return created

    @staticmethod
    def _media_for(names: Sequence[str], candidates: Sequence[str]) -> str | None:
        have = set(names)
        for cand in candidates:
            if cand in have:
                return cand
        return None

    def _paper_info(self, layout: Any, deadline: float) -> dict[str, Any]:
        media = str(self._idempotent("read paper", lambda: layout.CanonicalMediaName, deadline))  # type: ignore[attr-defined]
        rotation = int(self._idempotent("read rotation", lambda: layout.PlotRotation, deadline))  # type: ignore[attr-defined]
        w, h = self._idempotent("read paper size", lambda: com_extra.byref_double_pair(layout, "GetPaperSize"), deadline)  # type: ignore[attr-defined]
        if rotation in (1, 3):
            w, h = h, w
        return {"media": media, "rotation": rotation, "paper_mm": [round(w, 3), round(h, 3)],
                "orientation": "landscape" if w >= h else "portrait"}

    # ------------------------------------------------------------------ layout creation

    def create_layout(self, spec: Mapping[str, Any], *, deadline: float) -> dict[str, Any]:
        """One paper-space layout: paper, plot settings, frame + title block, one locked viewport at an exact scale.

        spec: name, device?, media_candidates, orientation, rotation, frame_prims (houseviews.Prim in paper mm),
        layers {standard: (office, colour)}, viewport_layer (standard name), viewport_mm [x0,y0,x1,y1],
        view_window [xmin,ymin,xmax,ymax] (model space that fills the viewport at the scale), scale_denominator,
        unit_mm, marker, if_exists (skip|replace|error), style_sheet.
        """
        return self._guard(f"create layout {spec['name']}",  # type: ignore[attr-defined]
                           lambda: self._create_layout(spec, deadline))

    def _create_layout(self, spec: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        app, doc = self._writable_document(deadline)  # type: ignore[attr-defined]
        name = str(spec["name"])
        marker = str(spec["marker"])
        if_exists = str(spec.get("if_exists", "skip"))
        existing = self._find_layout(doc, name, deadline)
        replaced = False
        if existing is not None:
            self._idempotent("register marker application", lambda: com_extra.register_app(doc), deadline)  # type: ignore[attr-defined]
            block = self._idempotent("open layout block", lambda: existing.Block, deadline)  # type: ignore[attr-defined]
            ours = self._find_marked(block, marker, deadline)  # type: ignore[attr-defined]
            if not ours and if_exists == "skip" and self._find_marked(block, None, deadline):  # type: ignore[attr-defined]
                raise ToolError(ErrorCode.CONFLICT, f"layout {name!r} was made by an earlier call with different content "
                                "(paper, scale, window or title block); pass if_exists=replace to rebuild it")
            if not ours and if_exists != "error" and self._has_only_viewports(block, deadline):
                if_exists = "replace"  # an empty layout of that name: what an earlier call cut short left behind
                ours = [None]
            if not ours and if_exists != "error":
                raise ToolError(ErrorCode.CONFLICT, f"a layout named {name!r} exists and was not created by this call "
                                "(no marker on its entities); it is left alone. Choose another sheet code.")
            if if_exists == "error":
                raise ToolError(ErrorCode.CONFLICT, f"layout {name!r} already exists")
            expected = spec.get("expected_marked")
            if if_exists == "skip" and (expected is None or len(ours) == int(expected)):
                info = self._layout_info(doc, existing, deadline)
                info.update({"reused": True, "replaced": False})
                return info
            # replace (or skip of a layout left half built by an earlier call cut short): rebuild it
            self._idempotent("delete the earlier layout", lambda: existing.call("Delete"), deadline)  # type: ignore[attr-defined]
            replaced = True
        layouts = self._idempotent("open layouts", lambda: doc.Layouts, deadline)  # type: ignore[attr-defined]
        previous = self._idempotent("read active layout", lambda: doc.ActiveLayout, deadline)  # type: ignore[attr-defined]
        layout = self._idempotent("add layout", lambda: layouts.call("Add", name), deadline)  # type: ignore[attr-defined]
        self._idempotent("register marker application", lambda: com_extra.register_app(doc), deadline)  # type: ignore[attr-defined]
        built = False
        try:
            self._idempotent("activate layout", lambda: setattr(doc, "ActiveLayout", layout), deadline)  # type: ignore[attr-defined]
            device = str(spec.get("device") or self.settings.pdf_plot_device)
            step = lambda op, fn: self._idempotent(op, fn, deadline)  # type: ignore[attr-defined]  # noqa: E731
            step("select plot device", lambda: setattr(layout, "ConfigName", device))
            step("refresh plot device", lambda: layout.call("RefreshPlotDeviceInfo"))
            names = [str(n) for n in (step("list paper names", lambda: layout.call("GetCanonicalMediaNames")) or ())]
            media = self._media_for(names, list(spec["media_candidates"]))
            if media is None:
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                f"plot device {device} offers none of the papers {list(spec['media_candidates'])}")
            step("paper units mm", lambda: setattr(layout, "PaperUnits", AC_MILLIMETRES))
            step("select paper", lambda: setattr(layout, "CanonicalMediaName", media))
            step("rotation", lambda: setattr(layout, "PlotRotation", int(spec.get("rotation", 0))))
            step("plot the layout", lambda: setattr(layout, "PlotType", AC_PLOT_TYPE_LAYOUT))
            step("standard scale", lambda: setattr(layout, "UseStandardScale", True))
            step("scale 1:1", lambda: setattr(layout, "StandardScale", AC_SCALE_1_TO_1))
            step("hide viewport borders", lambda: setattr(layout, "PlotViewportBorders", False))
            step("plot lineweights", lambda: setattr(layout, "PlotWithLineweights", True))
            style = spec.get("style_sheet")
            style_set = None
            if style:
                try:
                    styles = [str(n) for n in (layout.call("GetPlotStyleTableNames") or ())]
                    pick = next((n for n in styles if n.casefold() == str(style).casefold()), None)
                    if pick:
                        step("plot style table", lambda: setattr(layout, "StyleSheet", pick))
                        style_set = pick
                except Exception as exc:
                    logger.warning("layout %s: plot style table not set: %s", name, exc)
            layers = spec["layers"]
            non_plotting = spec.get("viewport_layer")
            created_layers = self._ensure_layers(doc, layers, non_plotting, deadline)
            self._ensure_text_style(doc, deadline)
            ps = self._idempotent("open paper space", lambda: doc.PaperSpace, deadline)  # type: ignore[attr-defined]
            count_before = int(self._idempotent("count paper space", lambda: ps.Count, deadline))  # type: ignore[attr-defined]
            drawn = 0
            removed = self._remove_default_viewports(ps, deadline)
            doc.call("StartUndoMark")
            try:
                drawn = self._emit_paper(ps, spec["frame_prims"], layers, marker)
                vp_info = self._add_viewport(app, doc, ps, spec, layers, marker, deadline)
            finally:
                doc.call("EndUndoMark")
            count_after = int(self._idempotent("count paper space", lambda: ps.Count, deadline))  # type: ignore[attr-defined]
            info = self._layout_info(doc, layout, deadline)
            info.update({"reused": False, "replaced": replaced, "frame_entities_drawn": drawn,
                         "paper_space_added": count_after - count_before, "layers_created": created_layers,
                         "style_sheet": style_set, "viewport_creation": vp_info, "device": device,
                         "default_viewports_removed": removed,
                         "media_requested": media})
            built = True
            return info
        finally:
            try:
                self._idempotent("restore the active layout", lambda: setattr(doc, "ActiveLayout", previous), deadline)  # type: ignore[attr-defined]
            except Exception as exc:  # reported in the log; the layout itself is complete
                logger.warning("layout %s: could not restore the active layout: %s", name, exc)
            if not built:  # the call failed half way (AutoCAD busy, ...): do not leave a half built layout behind
                try:
                    self._idempotent("remove the unfinished layout", lambda: layout.call("Delete"), deadline)  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.warning("layout %s: unfinished layout could not be removed: %s", name, exc)

    def _has_only_viewports(self, block: Any, deadline: float) -> bool:
        def check() -> bool:
            return all(str(block.call("Item", i).ObjectName) == "AcDbViewport" for i in range(int(block.Count)))

        return bool(self._idempotent("inspect the layout", check, deadline))  # type: ignore[attr-defined]

    def _remove_default_viewports(self, ps: Any, deadline: float) -> int:
        """A new layout may come with a viewport of AutoCAD's own (page setup default); it would plot over the sheet.
        Deleted here, in the layout this call just created. The paper-space overall viewport cannot be deleted and stays."""
        removed = 0
        entities = [ps.call("Item", i) for i in range(int(ps.Count))]
        for ent in entities:
            if str(ent.ObjectName) != "AcDbViewport":
                continue
            try:
                self._idempotent("delete the default viewport", lambda e=ent: e.call("Delete"), deadline)  # type: ignore[attr-defined]
                removed += 1
            except Exception as exc:  # the overall viewport refuses
                logger.debug("viewport kept: %s", exc)
        return removed

    def _emit_paper(self, ps: Any, prims: Sequence[Any], layers: Mapping[str, tuple[str, int]], marker: str) -> int:
        n = 0
        for prim in prims:
            v = prim.pts
            if prim.kind == "line":
                entity = ps.call("AddLine", com.double_array((v[0], v[1], 0.0)), com.double_array((v[2], v[3], 0.0)))
            elif prim.kind == "poly":
                flat: list[float] = []
                for i in range(0, len(v) - 1, 2):
                    flat += [v[i], v[i + 1]]
                entity = ps.call("AddLightWeightPolyline", com.double_array(flat))
                entity.Closed = bool(prim.closed)
            elif prim.kind == "text":
                entity = ps.call("AddMText", com.double_array((v[0], v[1], 0.0)), float((prim.data or {}).get("width", 0.0)),
                                 prim.text)
                entity.StyleName = TEXT_STYLE
                entity.Height = float(prim.height)
                entity.AttachmentPoint = int(prim.attach)
                entity.InsertionPoint = com.double_array((v[0], v[1], 0.0))
            else:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"paper-space primitive {prim.kind!r} is not supported")
            entity.Layer = layers[prim.layer][0]
            com_extra.set_xdata(entity, [marker, prim.semantic or prim.kind])
            n += 1
        return n

    def _add_viewport(self, app: Any, doc: Any, ps: Any, spec: Mapping[str, Any], layers: Mapping[str, tuple[str, int]],
                      marker: str, deadline: float) -> dict[str, Any]:
        idem = lambda op, fn: self._idempotent(op, fn, deadline)  # type: ignore[attr-defined]  # noqa: E731
        vx0, vy0, vx1, vy1 = spec["viewport_mm"]
        center = com.double_array(((vx0 + vx1) / 2, (vy0 + vy1) / 2, 0.0))
        vp = ps.call("AddPViewport", center, float(vx1 - vx0), float(vy1 - vy0))
        std_layer = spec.get("viewport_layer")
        if std_layer in layers:
            vp.Layer = layers[std_layer][0]
        com_extra.set_xdata(vp, [marker, "viewport"])
        idem("viewport on", lambda: setattr(vp, "ViewportOn", True))
        wx0, wy0, wx1, wy1 = spec["view_window"]
        idem("display the viewport", lambda: vp.call("Display", True))
        idem("work in paper space", lambda: setattr(doc, "ActiveSpace", 0))  # acPaperSpace
        idem("enter model space through the viewport", lambda: setattr(doc, "MSpace", True))
        idem("make the viewport active", lambda: setattr(doc, "ActivePViewport", vp))
        try:
            idem("zoom the viewport", lambda: app.call("ZoomWindow", com.double_array((wx0, wy0, 0.0)),
                                                        com.double_array((wx1, wy1, 0.0))))
            custom = float(spec["unit_mm"]) / float(spec["scale_denominator"])
            idem("set the viewport scale", lambda: setattr(vp, "CustomScale", custom))
            centre = idem("read the view centre", lambda: doc.call("GetVariable", "VIEWCTR"))
            size = idem("read the view size", lambda: doc.call("GetVariable", "VIEWSIZE"))
        finally:
            idem("leave the viewport", lambda: setattr(doc, "MSpace", False))
        if spec.get("lock", True):
            idem("lock the viewport", lambda: setattr(vp, "DisplayLocked", True))
        return {"view_centre": [round(float(c), 4) for c in centre[:2]], "view_height": round(float(size), 4),
                "wanted_centre": [round((wx0 + wx1) / 2, 4), round((wy0 + wy1) / 2, 4)]}

    # ------------------------------------------------------------------ reading layouts back

    def read_layouts(self, names: Sequence[str] | None = None, *, deadline: float) -> list[dict[str, Any]]:
        """Every paper-space layout of the active drawing, measured: paper, rotation, viewports, title block texts."""

        def run() -> list[dict[str, Any]]:
            _, doc = self._writable_document(deadline)  # type: ignore[attr-defined]
            layouts = self._idempotent("open layouts", lambda: doc.Layouts, deadline)  # type: ignore[attr-defined]
            wanted = {n.casefold() for n in names} if names else None
            out = []
            for index in range(int(layouts.Count)):
                layout = layouts.call("Item", index)
                lname = str(layout.Name)
                if lname.casefold() == "model" or (wanted is not None and lname.casefold() not in wanted):
                    continue
                out.append(self._layout_info(doc, layout, deadline))
            return out

        return self._guard("read layouts", run)  # type: ignore[attr-defined]

    def _layout_info(self, doc: Any, layout: Any, deadline: float) -> dict[str, Any]:
        def collect() -> dict[str, Any]:
            block = layout.Block
            name = str(layout.Name)
            viewports: list[dict[str, Any]] = []
            fields: dict[str, dict[str, str]] = {}
            n_ent = 0
            marked = 0
            by_object: Counter[str] = Counter()
            for index in range(int(block.Count)):
                ent = block.call("Item", index)
                n_ent += 1
                obj = str(ent.ObjectName)
                by_object[obj] += 1
                role = None
                try:
                    data = com_extra.get_xdata(ent)
                except Exception:  # an entity that refuses XData reads is simply not ours
                    data = None
                if data and len(data) >= 2:
                    marked += 1
                    role = data[1]
                if obj == "AcDbViewport":
                    vp: dict[str, Any] = {"marked": bool(data), "role": role}
                    for key, prop in (("scale_custom", "CustomScale"), ("locked", "DisplayLocked"), ("on", "ViewportOn"),
                                      ("layer", "Layer")):
                        try:
                            value = getattr(ent, prop)
                            vp[key] = bool(value) if key in ("locked", "on") else (str(value) if key == "layer" else float(value))
                        except Exception:  # the overall paper-space viewport has no custom scale
                            vp[key] = None
                    try:
                        vp["width_mm"], vp["height_mm"] = float(ent.Width), float(ent.Height)
                    except Exception:
                        pass
                    viewports.append(vp)
                elif role and role.startswith("tb_") and obj in ("AcDbMText", "AcDbText"):
                    kind, _, key = role.partition(":")
                    entry = fields.setdefault(key, {})
                    entry["label" if kind == "tb_label" else "value"] = str(ent.TextString)
            return {"name": name, "entities": n_ent, "marked_entities": marked, "by_object": dict(by_object),
                    "viewports": viewports, "title_block": fields}

        info = self._idempotent(f"read layout {layout.Name}", collect, deadline)  # type: ignore[attr-defined]
        info.update(self._paper_info(layout, deadline))
        for vp in info["viewports"]:
            custom = vp.get("scale_custom")
            vp["scale_denominator"] = round(1000.0 / custom, 4) if custom else None
        return info

    # ------------------------------------------------------------------ plotting

    def plot_layouts(self, names: Sequence[str], path: Path, *, deadline: float) -> dict[str, Any]:
        """Plot ONE layout (`names` has a single entry) to a one-page PDF with the PDF plot device: the layout is made
        active, plotted in the foreground with quiet errors, and the previously active layout is restored.

        Plotting several layouts in one PlotToFile call writes no file with this device (verified live), so a
        multi-page PDF is built by the caller from one PDF per sheet."""
        if len(names) != 1:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "plot_layouts plots exactly one layout per call")
        return self._guard(f"plot layout {names[0]} to {path}",  # type: ignore[attr-defined]
                           lambda: self._plot_layout(str(names[0]), path, deadline))

    def _plot_layout(self, name: str, path: Path, deadline: float) -> dict[str, Any]:
        _, doc = self._writable_document(deadline)  # type: ignore[attr-defined]
        device = self.settings.pdf_plot_device
        layout = self._find_layout(doc, name, deadline)
        if layout is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"layout {name!r} does not exist; call autocad.create_layout first")
        idem = lambda op, fn: self._idempotent(op, fn, deadline)  # type: ignore[attr-defined]  # noqa: E731
        plot = idem("open the plot object", lambda: doc.Plot)
        previous = idem("read active layout", lambda: doc.ActiveLayout)
        background = idem("read BACKGROUNDPLOT", lambda: doc.call("GetVariable", "BACKGROUNDPLOT"))
        quiet = idem("read plot QuietErrorMode", lambda: plot.QuietErrorMode)
        path.unlink(missing_ok=True)
        plotted = False
        errors: list[str] = []
        try:
            idem("set BACKGROUNDPLOT=0", lambda: doc.call("SetVariable", "BACKGROUNDPLOT", 0))
            idem("set plot QuietErrorMode", lambda: setattr(plot, "QuietErrorMode", True))
            idem("activate the layout", lambda: setattr(doc, "ActiveLayout", layout))
            plotted = bool(idem("plot to file", lambda: plot.call("PlotToFile", str(path), device)))
        finally:
            for label, fn in (("active layout", lambda: setattr(doc, "ActiveLayout", previous)),
                              ("BACKGROUNDPLOT", lambda: doc.call("SetVariable", "BACKGROUNDPLOT", background)),
                              ("plot QuietErrorMode", lambda: setattr(plot, "QuietErrorMode", quiet))):
                try:
                    idem(f"restore {label}", fn)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
        self._wait_for_file(path, deadline)  # type: ignore[attr-defined]
        return {"path": str(path), "plotted": plotted, "device": device, "layout": name, "restore_errors": errors}

    # ------------------------------------------------------------------ counts and the re-open check

    def drawing_counts(self, *, deadline: float) -> dict[str, Any]:
        """Entity counts of the active drawing: model space by object type, layouts with viewports."""
        return self._guard("count drawing entities",  # type: ignore[attr-defined]
                           lambda: self._counts(self._writable_document(deadline)[1], deadline))  # type: ignore[attr-defined]

    def _counts(self, doc: Any, deadline: float) -> dict[str, Any]:
        def model() -> dict[str, Any]:
            ms = doc.ModelSpace
            by_object: Counter[str] = Counter()
            marked = 0
            total = int(ms.Count)
            for index in range(total):
                ent = ms.call("Item", index)
                by_object[str(ent.ObjectName)] += 1
                try:
                    if com_extra.get_xdata(ent):
                        marked += 1
                except Exception:
                    pass
            return {"model_space": total, "model_by_object": dict(by_object), "model_marked": marked}

        out = self._idempotent("count model space entities", model, deadline)  # type: ignore[attr-defined]
        layouts = self._idempotent("open layouts", lambda: doc.Layouts, deadline)  # type: ignore[attr-defined]
        infos = []
        for index in range(int(layouts.Count)):
            layout = layouts.call("Item", index)
            if str(layout.Name).casefold() == "model":
                continue
            infos.append(self._layout_info(doc, layout, deadline))
        out["layouts"] = {i["name"]: {"entities": i["entities"], "marked": i["marked_entities"], "paper_mm": i["paper_mm"],
                                      "media": i["media"],
                                      "viewports": [{"scale_denominator": v.get("scale_denominator"),
                                                     "locked": v.get("locked")} for v in i["viewports"]]}
                          for i in infos}
        return out

    def reopen_check(self, path: Path, keep_open: bool, *, deadline: float) -> dict[str, Any]:
        """Close the saved drawing at `path` (refused when it has unsaved changes) and open the DWG from disk again,
        counting entities before and after. Returns both counts, the differences and whether the file opened."""
        return self._guard(f"re-open {path}", lambda: self._reopen_check(path, keep_open, deadline))  # type: ignore[attr-defined]

    def _documents(self, app: Any, deadline: float) -> list[tuple[Any, str, str]]:
        def collect() -> list[tuple[Any, str, str]]:
            docs = app.Documents
            return [(d, str(d.Name), str(d.FullName)) for d in (docs.call("Item", i) for i in range(int(docs.Count)))]

        return self._idempotent("list drawings", collect, deadline)  # type: ignore[attr-defined]

    def _reopen_check(self, path: Path, keep_open: bool, deadline: float) -> dict[str, Any]:
        from lga_worker.autocad.provider import same_file_path  # noqa: PLC0415

        if not path.is_file():
            raise ToolError(ErrorCode.NOT_FOUND, f"{path} does not exist; call autocad.save first")
        app = self._application(deadline)  # type: ignore[attr-defined]
        before: dict[str, Any] | None = None
        was_open = False
        for doc, _name, full in self._documents(app, deadline):
            if full and same_file_path(full, path):
                was_open = True
                if not bool(self._idempotent("read the saved flag", lambda d=doc: d.Saved, deadline)):  # type: ignore[attr-defined]
                    raise ToolError(ErrorCode.CONFLICT, f"{path} is open with unsaved changes; save it first "
                                    "(nothing was closed)")
                before = self._counts(doc, deadline)
                self._idempotent("close the saved drawing", lambda d=doc: d.call("Close", False), deadline)  # type: ignore[attr-defined]
                break
        stop = min(deadline, time.monotonic() + self.settings.com_call_timeout_s)
        while any(same_file_path(full, path) for _d, _n, full in self._documents(app, deadline)):
            if time.monotonic() > stop:
                raise ToolError(ErrorCode.BUSY, "the drawing did not close in time")
            com.pump_sleep(OPEN_POLL_S)
        docs = self._idempotent("open Documents", lambda: app.Documents, deadline)  # type: ignore[attr-defined]
        opened = docs.call("Open", str(path), not keep_open)
        stop = min(deadline, time.monotonic() + self.settings.com_call_timeout_s)
        while not any(same_file_path(full, path) for _d, _n, full in self._documents(app, deadline)):
            if time.monotonic() > stop:
                raise ToolError(ErrorCode.BUSY, "the drawing did not finish opening in time")
            com.pump_sleep(OPEN_POLL_S)
        after = self._counts(opened, deadline)
        read_only = bool(self._idempotent("read ReadOnly", lambda: opened.ReadOnly, deadline))  # type: ignore[attr-defined]
        name = str(self._idempotent("read name", lambda: opened.Name, deadline))  # type: ignore[attr-defined]
        if not keep_open:
            self._idempotent("close the re-opened drawing", lambda: opened.call("Close", False), deadline)  # type: ignore[attr-defined]
        return {"path": str(path), "was_open_before": was_open, "before": before, "after": after, "opened_name": name,
                "opened_read_only": read_only, "kept_open": bool(keep_open)}
