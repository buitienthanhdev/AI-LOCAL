# Paint drawing engine

`paint.draw` draws a whole picture in Microsoft Paint (Windows 11 app, English UI) and proves it did. The model
never sees pixels or drags a mouse: it names a template or lists objects in composition units, and the engine
plans, executes, verifies and repairs.

```
paint.draw {template | objects}
   geometry.plan_scene      units -> canvas px, margin / kind / overlap rules, snapped stroke widths, layer order
   driver.PaintDriver       fresh Paint window, calibrate, tools by UI Automation, mouse input, commit
   engine.run_plan          draw in layer order, verify every ~6 objects, redraw only what is wrong (+ what it covered)
   verify.compare           expected raster (PIL) vs canvas screenshot, per object; spill = unexplained ink
```

Code: `services/python-agent/lga_worker/paint/` (pure part is unit-tested: `tests/test_paint_unit.py`), tool glue
`lga_worker/tools/paint.py`, contract `tools/paint.json`.

## Coordinates

* Composition units: x 0..1000 across the canvas, y 0..`1000*H/W` (562 on the default 1152x648).
* Canvas px = units x `W/1000`. Screen px = canvas px x zoom + page origin (`calibration.CanvasTransform`).
* Page origin comes from a pixel scan of the blank page when the window is opened; Paint's `image` element
  rectangle is 1 px above the real page, and it moves 15 px when the Brush/Fill side panel opens, so it is read
  again before every object.
* Stroke widths: shapes only offer 1/3/5/8 px; `detail/structure/accent` = 1x/1.75x/2.5x of `H/324`, snapped.

## What was measured on Paint 11.2605 (the driver relies on these)

| Fact | How it is used |
|---|---|
| Tools are ToggleButtons; `image` element Name says "Using Rectangle tool on Canvas" | tool selection is read back |
| Outline colour = selected colour slot, fill = the other slot | slot 1 stays selected: outline = colour 1, fill = colour 2 |
| Shape fill menu: No fill / Solid fill; outline: No outline / Solid outline | filled-only and outline-only shapes without a bucket fill |
| Size menu for shapes: `Size1/3/5/8`; Brush Size slider value = width - 1 | |
| A shape stays live until a click elsewhere; Enter does not commit, Esc cancels | commit = two clicks on a neutral point, far from the canvas |
| A click 30 px beside the canvas lands on a resize handle and widens the canvas by 30 px | neutral point is a viewport corner |
| Changing a colour while a shape is live repaints that shape | commit before any colour change |
| Edit colours dialog is modal: UIA Invoke on its button never returns | open it with a real click; hex field `HexTextBox`, OK = `PrimaryButton` |
| First press after that dialog is swallowed | a neutral click after it closes |
| Polygon: drag first edge, click each vertex, click first vertex to close | |
| Brush strokes need real mouse-move events, `SetCursorPos` alone yields a straight line | `mouse_event` with `MoveNoCoalesce`, 12 ms per sample |
| Paint remembers the last canvas size for new windows | the canvas must never be resized by accident |

## Verification and repair

* Outline: share of the object's visible outline (not covered by anything drawn later) present within +-2 px.
* Fill: colour sampled well inside, where the plan shows that fill.
* Spill: non-white pixels the plan does not explain. Above 5 % nothing is redrawn (`engine.REPAIR_SPILL_MAX`).
* A failed object is drawn again together with every later object that overlaps it, so z-order survives.
* The call fails with `VERIFICATION_FAILED` if the canvas still does not match; `paint.inspect` re-compares the
  canvas with the last plan and is the read-only check for `task.complete`.

## Redrawing a picture (`paint.draw_image`)

A photo is not a handful of rectangles. `paint.draw_image {image_path, detail}` traces the picture itself
(`lga_worker/paint/vectorize.py`) and draws the result with the same executor and verifier:

1. the picture is cut into edge-following superpixels (SLIC) whose mean colours are matched to a palette of
   Paint's 20 ribbon swatches plus up to 10 custom colours fitted to the picture (k-means where only the custom
   centres move); a Potts term over the superpixel graph makes neighbours agree, so gradients do not blotch;
2. same-colour neighbours merge into regions; regions below a minimum area are absorbed; each outline is
   smoothed, simplified to at most 26 vertices (one click each) and layered by nesting depth, behind a full-size
   backing polygon so no canvas shows through a seam;
3. polygons are drawn depth by depth, grouped by colour (a colour change is one swatch click), each with a thin
   dark outline (`outline=false` for flat regions): the outline is what makes the silhouette readable;
4. the canvas is compared with the plan; up to 10 % of the regions may stay unmatched (`TRACE_TOLERANCE`), the
   output reports the likeness as structural similarity between the picture and the canvas (`fidelity.ssim`).

`detail` low / normal / high = about 40 / 60 / 90 polygons, 2-4 minutes. The result is an illustration-like
polygon portrait of the photo (SSIM 0.57-0.65 for a busy photo, 0.98 for flat artwork), not a pixel copy; the
outlines lower the numeric score and raise the readability.

## More measured facts (image drawing)

| Fact | Consequence |
|---|---|
| A polygon whose first dragged edge is shorter than ~12 px is silently not drawn | the ring is rotated to start at its longest edge; traced polygons need an edge of 16 px |
| A RIGHT click on a palette swatch sets colour 2 without selecting it; a radio selection costs 0.5 s each way | colour 1 stays selected; fills are set by right-clicking swatches (about 0.3 s) |
| The first press after any real click on the ribbon (or the Edit colours dialog) only re-activates the canvas | a neutral click on the grey margin hands the focus back before drawing |
| Vertices closer than ~4 px in a row read as a double click and end the polygon | traced vertices are at least 5 px apart |
| Only 10 custom colours show in the ribbon; the dialog is 2.5 s each | at most 10 custom colours per picture |

## Sketch mode: applications that cannot fill

The way a picture is drawn follows what the application can do:

| Application | Tool | How |
|---|---|---|
| Can fill with colour (Paint) | `paint.draw_image` (style `fill`, default) | filled colour regions, layered back to front |
| Lines only (AutoCAD) | `autocad.draw_image` | coloured pencil sketch: region contours + shading hatch lines, one layer per colour |
| Fill-capable app, but a sketch was asked for | `paint.draw_image` style `sketch` | the same strokes, drawn with the brush |

`lga_worker/paint/sketch.py`: the picture is cut into colour regions as for the filled drawing (free palette of up
to 24 colours in CAD); every region gets its contour in its own colour, and every region that is not light gets
parallel hatching, 45 degrees, cross-hatched when very dark, denser as the colour gets darker; light regions are
left to the paper. "The thing is red, the pencil is red." The result is verified by counting entities (AutoCAD)
or by ink agreement with the plan (Paint: coverage and stray ink).

AutoCAD specifics (`AutoCadProvider.draw_strokes`): each colour is a layer `SKETCH-RRGGBB` carrying a true colour
(ACI nearest as a fallback); one undo step; the model-space background is set to white (`paper='keep'` leaves
it) because dark pencil lines vanish on AutoCAD's dark background, and the previous value is reported.
Measured: 2025 strokes in 3.8 s. In Paint a stroke is a brush path (about 0.25 s), capped at ~1100 strokes
(883 strokes, 232 s for a busy photo).

## Limits

English Paint UI only; a canvas that does not fit the window at its zoom is refused (use Fit to window);
single monitor; one Paint window per call (never one that may hold the user's work).

## Repair only touches what is already drawn

`engine.repair_set(plan, failed, drawn)` is limited to the objects already on the canvas. Before 2026-10-12 a
failed early stage redrew the whole plan, including objects of later layers, and the stage then reported 57 %
unexplained ink. `art.draw` (docs/art-engine.md) drives this same engine for Paint.
