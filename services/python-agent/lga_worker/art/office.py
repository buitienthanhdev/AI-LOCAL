"""Scene -> freeform shapes in PowerPoint, Word or Excel (COM; runs on an STA thread).

A shape is built node by node (`BuildFreeform` / `AddNodes` / `ConvertToShape`), so any polygon is drawn exactly, in
any RGB colour, as a real editable shape; rectangles, ellipses and lines use the application's own primitives.
PowerPoint is verified by exporting the slide to a PNG and comparing it with the scene's reference raster; Word and
Excel by reading the shapes back (count, position, colour).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from lga_worker.art.scene import RGB, Scene, Shape, render
from lga_worker.errors import ErrorCode, ToolError

MSO_SHAPE_RECTANGLE = 1
MSO_SHAPE_OVAL = 9
PP_LAYOUT_BLANK = 12
WD_WRAP_FRONT = 3
WD_RELATIVE_PAGE = 1
WD_ORIENT_LANDSCAPE = 1
SSIM_OK = 0.90
PROG_IDS = {"powerpoint": "PowerPoint.Application", "word": "Word.Application", "excel": "Excel.Application"}
MARGIN_PT = 18.0


def _ole(rgb: RGB) -> int:
    return rgb[0] + 256 * rgb[1] + 65536 * rgb[2]


def _app(key: str) -> tuple[Any, bool]:
    import win32com.client

    prog_id = PROG_IDS[key]
    try:
        return win32com.client.GetActiveObject(prog_id), False
    except Exception:
        return win32com.client.DispatchEx(prog_id), True


def _com_message(exc: Exception) -> str:
    try:
        return exc.excepinfo[2] or str(exc)  # type: ignore[attr-defined]
    except Exception:
        return str(exc)


def _style(shp: Any, shape: Shape, weight_pt: float) -> None:
    if shape.fill is not None and shape.closed:
        shp.Fill.Visible = -1
        shp.Fill.Solid()
        shp.Fill.ForeColor.RGB = _ole(shape.fill)
        shp.Fill.Transparency = 0
    else:
        shp.Fill.Visible = 0
    if shape.stroke is not None:
        shp.Line.Visible = -1
        shp.Line.ForeColor.RGB = _ole(shape.stroke)
        shp.Line.Weight = max(0.25, weight_pt)
    else:
        shp.Line.Visible = 0


def _build(container: Any, shape: Shape, k: float, ox: float, oy: float, *, word: bool) -> Any:
    pts = [(ox + x * k, oy + y * k) for x, y in shape.points]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if shape.hint in ("rect", "ellipse") and shape.closed:
        kind = MSO_SHAPE_RECTANGLE if shape.hint == "rect" else MSO_SHAPE_OVAL
        shp = container.AddShape(kind, min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    elif len(pts) == 2:
        shp = container.AddLine(pts[0][0], pts[0][1], pts[1][0], pts[1][1])
    else:
        builder = container.BuildFreeform(0, pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            builder.AddNodes(0, 0, x, y)
        if shape.closed:
            builder.AddNodes(0, 0, pts[0][0], pts[0][1])
        shp = builder.ConvertToShape()
    if word:
        try:
            shp.WrapFormat.Type = WD_WRAP_FRONT
            shp.RelativeHorizontalPosition = WD_RELATIVE_PAGE
            shp.RelativeVerticalPosition = WD_RELATIVE_PAGE
            shp.Left, shp.Top = min(xs), min(ys)
        except Exception:
            pass
    _style(shp, shape, shape.width * k)
    return shp


def _fit(scene: Scene, width_pt: float, height_pt: float, margin: float = MARGIN_PT) -> tuple[float, float, float]:
    k = min((width_pt - 2 * margin) / scene.width, (height_pt - 2 * margin) / scene.height)
    return k, (width_pt - scene.width * k) / 2, (height_pt - scene.height * k) / 2


def reference_raster(scene: Scene, width_pt: float, height_pt: float, px_width: int, margin: float = 0.0) -> Image.Image:
    """What the page should look like: the scene fitted into a `width_pt` x `height_pt` page, as pixels."""
    k, ox, oy = _fit(scene, width_pt, height_pt, margin)
    s = px_width / width_pt
    page = Image.new("RGB", (px_width, round(height_pt * s)), scene.background or (255, 255, 255))
    body = render(scene, max(1, round(scene.width * k * s)), supersample=2)
    page.paste(body, (round(ox * s), round(oy * s)))
    return page


def _ssim(a: Image.Image, b: Image.Image) -> float:
    import numpy as np
    from skimage.metrics import structural_similarity

    size = (min(a.width, b.width), min(a.height, b.height))
    ga = np.asarray(a.convert("L").resize(size))
    gb = np.asarray(b.convert("L").resize(size))
    return float(structural_similarity(ga, gb, data_range=255))


def draw_scene(app_key: str, scene: Scene, *, shots_dir: Path, stem: str, save_path: str | None = None,
               should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Draw the scene in a new document of the application. Raises ToolError on COM failure."""
    if app_key not in PROG_IDS:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"not an Office drawing application: {app_key}")
    started_at = time.monotonic()
    try:
        app, _started = _app(app_key)
        try:
            app.Visible = True
        except Exception:
            pass
        if app_key == "powerpoint":
            return _powerpoint(app, scene, shots_dir, stem, save_path, should_stop, started_at)
        if app_key == "word":
            return _word(app, scene, save_path, should_stop, started_at)
        return _excel(app, scene, save_path, should_stop, started_at)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(ErrorCode.EXECUTION_FAILED, f"{PROG_IDS[app_key]}: {_com_message(exc)}") from exc


def _draw_all(container: Any, scene: Scene, k: float, ox: float, oy: float, should_stop: Callable[[], bool], *,
              word: bool = False) -> tuple[int, list[str]]:
    drawn = 0
    problems: list[str] = []
    for index, shape in enumerate(scene.shapes):
        if should_stop():
            raise ToolError(ErrorCode.CANCELLED, f"stopped after {drawn} of {len(scene.shapes)} shapes")
        if len(shape.points) < 2 or not shape.visible:
            continue
        try:
            _build(container, shape, k, ox, oy, word=word)
            drawn += 1
        except Exception as exc:
            problems.append(f"shape {index}: {_com_message(exc)}")
            if len(problems) > 25:
                raise ToolError(ErrorCode.EXECUTION_FAILED, "too many shapes were refused: " + "; ".join(problems[:3])) from exc
    return drawn, problems


def _powerpoint(app: Any, scene: Scene, shots_dir: Path, stem: str, save_path: str | None,
                should_stop: Callable[[], bool], started_at: float) -> dict[str, Any]:
    pres = app.Presentations.Add(-1)
    slide = pres.Slides.Add(1, PP_LAYOUT_BLANK)
    width_pt, height_pt = float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)
    k, ox, oy = _fit(scene, width_pt, height_pt, 0.0 if abs(width_pt / height_pt - scene.width / scene.height) < 0.02 else MARGIN_PT)
    if scene.background is not None:
        slide.FollowMasterBackground = 0
        slide.Background.Fill.Solid()
        slide.Background.Fill.ForeColor.RGB = _ole(scene.background)
    drawn, problems = _draw_all(slide.Shapes, scene, k, ox, oy, should_stop)
    count = int(slide.Shapes.Count)
    shots_dir.mkdir(parents=True, exist_ok=True)
    exported = shots_dir / f"{stem}-slide.png"
    px_width = 1600
    slide.Export(str(exported), "PNG", px_width, round(px_width * height_pt / width_pt))
    with Image.open(exported) as opened:
        actual = opened.convert("RGB")
    margin = 0.0 if abs(width_pt / height_pt - scene.width / scene.height) < 0.02 else MARGIN_PT
    expected = reference_raster(scene, width_pt, height_pt, actual.width, margin)
    ssim = _ssim(expected, actual)
    saved = None
    if save_path:
        pres.SaveAs(str(Path(save_path)))
        saved = str(Path(save_path))
    return {"app": "powerpoint", "document": str(pres.Name), "shapes": count, "drawn": drawn, "expected": len(scene.shapes),
            "refused": problems, "ssim": round(ssim, 3), "ok": count >= drawn > 0 and ssim >= SSIM_OK and not problems,
            "screenshot": str(exported), "page": [width_pt, height_pt], "saved": saved,
            "seconds": round(time.monotonic() - started_at, 1)}


def _word(app: Any, scene: Scene, save_path: str | None, should_stop: Callable[[], bool], started_at: float) -> dict[str, Any]:
    doc = app.Documents.Add()
    try:
        doc.PageSetup.Orientation = WD_ORIENT_LANDSCAPE
    except Exception:
        pass
    width_pt, height_pt = float(doc.PageSetup.PageWidth), float(doc.PageSetup.PageHeight)
    k, ox, oy = _fit(scene, width_pt, height_pt, MARGIN_PT)
    try:
        app.ScreenUpdating = False
    except Exception:
        pass
    try:
        drawn, problems = _draw_all(doc.Shapes, scene, k, ox, oy, should_stop, word=True)
    finally:
        try:
            app.ScreenUpdating = True
        except Exception:
            pass
    count = int(doc.Shapes.Count)
    sampled = _readback(doc.Shapes, scene, k, ox, oy)
    saved = None
    if save_path:
        doc.SaveAs2(str(Path(save_path)))
        saved = str(Path(save_path))
    return {"app": "word", "document": str(doc.Name), "shapes": count, "drawn": drawn, "expected": len(scene.shapes),
            "refused": problems, "readback": sampled, "ok": count >= drawn > 0 and sampled["mismatched"] == 0 and not problems,
            "page": [width_pt, height_pt], "saved": saved, "seconds": round(time.monotonic() - started_at, 1)}


def _excel(app: Any, scene: Scene, save_path: str | None, should_stop: Callable[[], bool], started_at: float) -> dict[str, Any]:
    book = app.Workbooks.Add()
    sheet = book.ActiveSheet
    width_pt, height_pt = 960.0, 540.0
    k, ox, oy = _fit(scene, width_pt, height_pt, 0.0)
    ox += 6.0
    oy += 6.0
    try:
        app.ScreenUpdating = False
    except Exception:
        pass
    try:
        drawn, problems = _draw_all(sheet.Shapes, scene, k, ox, oy, should_stop)
    finally:
        try:
            app.ScreenUpdating = True
        except Exception:
            pass
    count = int(sheet.Shapes.Count)
    sampled = _readback(sheet.Shapes, scene, k, ox, oy)
    saved = None
    if save_path:
        book.SaveAs(str(Path(save_path)))
        saved = str(Path(save_path))
    return {"app": "excel", "document": str(book.Name), "shapes": count, "drawn": drawn, "expected": len(scene.shapes),
            "refused": problems, "readback": sampled, "ok": count >= drawn > 0 and sampled["mismatched"] == 0 and not problems,
            "page": [width_pt, height_pt], "saved": saved, "seconds": round(time.monotonic() - started_at, 1)}


def _readback(shapes: Any, scene: Scene, k: float, ox: float, oy: float, samples: int = 12) -> dict[str, Any]:
    """Compare a spread of the drawn shapes with the scene: fill colour and bounding box."""
    visible = [s for s in scene.shapes if len(s.points) >= 2 and s.visible]
    total = int(shapes.Count)
    if not visible or total == 0:
        return {"checked": 0, "mismatched": 0}
    step = max(1, len(visible) // samples)
    checked = mismatched = 0
    for i in range(0, len(visible), step):
        shape = visible[i]
        if i >= total:
            break
        try:
            item = shapes.Item(i + 1)
            xs = [ox + x * k for x, _ in shape.points]
            ys = [oy + y * k for _, y in shape.points]
            box_ok = (abs(float(item.Left) - min(xs)) < 3.0 and abs(float(item.Top) - min(ys)) < 3.0
                      and abs(float(item.Width) - (max(xs) - min(xs))) < 3.0)
            colour_ok = True
            if shape.fill is not None and shape.closed:
                colour_ok = int(item.Fill.ForeColor.RGB) == _ole(shape.fill)
            checked += 1
            if not (box_ok and colour_ok):
                mismatched += 1
        except Exception:
            mismatched += 1
            checked += 1
    return {"checked": checked, "mismatched": mismatched}
