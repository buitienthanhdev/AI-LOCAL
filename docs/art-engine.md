# Art brain: one picture, any drawing application

`art.draw` draws a picture in whichever application can draw, from one description of the picture. The planner
does not choose between paint / autocad / office tools by picture type any more: it says **what** (a photo, an SVG
it wrote, a template, objects) and **where** (`app`), and the brain does the rest.

```
source                          Scene (1000 x 562.5 sheet units)            backend
image_path  --trace (SLIC, free palette)-->  filled regions        -->  paint     mouse, filled polygons, 30 colours
svg / svg_path --parse_svg------------->  shapes, true colours      -->  autocad   pencil strokes, hidden lines removed
template / objects --scene_from_objects->  (hint: rect/ellipse/line) -->  powerpoint | word | excel   COM freeforms
                                                                    -->  file      SVG + PNG
```

Code: `services/python-agent/lga_worker/art/` (`scene`, `svg`, `convert`, `office`, `capabilities`, `export`), tool glue
`lga_worker/tools/art.py`, contract `tools/art.json`, tests `tests/test_art_unit.py`.

## What the application can do decides how it is drawn

| `app` | fills regions | colours | what is drawn | verification |
|---|---|---|---|---|
| `paint` | yes | 20 swatches + 10 fitted custom | objects: rect/ellipse natively, polygons by mouse, paths by brush | canvas screenshot compared with the plan per object + likeness (SSIM) to the picture |
| `autocad` | no | any (true colour, ACI fallback) | coloured pencil sketch: visible outlines + 45° hatching in each fill's colour, denser where darker | entities added = strokes planned |
| `powerpoint` | yes | any | editable freeform shapes (own primitives for rect/ellipse/line) | slide exported to PNG, SSIM against the scene render (>= 0.90) |
| `word`, `excel` | yes | any | the same shapes on a page / sheet | shapes read back (count, position, fill) |
| `file` | - | any | `.svg` and `.png` for Inkscape, Illustrator, Photoshop, GIMP, Krita, Figma, a browser | files exist and are not empty |

`style: auto` = filled where the application fills, pencil sketch where it only draws lines; `sketch` is honoured
anywhere. An application the brain cannot drive fails with `UNSUPPORTED` and points at `file`.

## Sources

* **image_path**: SLIC superpixels -> Lab colours -> palette -> Potts smoothing -> regions -> traced polygons,
  painted back to front. For applications without a mouse limit (`powerpoint`/`word`/`excel`/`file`) the free
  palette (64 colours) and detail `ultra` (about 800 regions) are used and no outlines: on the same photo the
  structural similarity is 0.81 against 0.72 for `max` and 0.49 with outlines. Paint keeps its own pipeline
  (`paint.draw_image`: Paint palette, 26 vertices per polygon, `max`).
* **svg / svg_path**: path (all commands, arcs, relative forms), rect, circle, ellipse, line, polyline, polygon,
  groups, transforms, fill/stroke/opacity/style, CSS colours, gradients (mean stop colour). Text, filters, masks and
  images are skipped and reported. What sticks out of the viewBox is cut at the frame; a subject that spans less
  than 60 % of the frame is enlarged and centred; a full-frame first rectangle becomes the sheet background.
* **template / objects**: the Paint engine's own geometry-first description (`paint.draw`).

The planner writes the SVG itself when there is no source picture. Because a language model tends to write a
handful of shapes, `art.draw` **refuses an inline SVG with fewer than 40 shapes** at the default (maximum) detail,
saying which parts to add (outlines, highlights, shading, background); `detail: low` is only for plain figures
the user asked for.

Floating parts: filled shapes (and strokes at least 6 units wide, such as a tail) that hang just off the main body
(gap below 35 % of the group's size, at most 140 units; a sun or tree far from the rest is a separate object and is
left alone) are refused once per 15 minutes with their boxes and gaps; the next call moves each such group until it
touches the body (`convert.attach_detached`) and says so in `notes`. The result also carries a screenshot (the
slide export; for Word, Excel and AutoCAD the scene as planned) so the planner can look at it and correct the SVG.
Documents are left open and unsaved: `art.draw` never writes a file except `app: file` (`output_path`).

## Scene -> Paint

`convert.to_paint_objects`: sheet -> canvas (`fit_box`, 5 % margin), colours snapped with `build_palette` weighted by
area, closed shapes simplified (Douglas-Peucker with growing tolerance) to <= 26 vertices at least 5 px apart,
shapes with no edge >= 16 px dropped (Paint ignores a shorter first edge) and counted, layers by real overlap
(`1 + deepest earlier overlapping shape`) so objects of one layer never overlap and layer order reproduces
painter's order, stroke width -> weight class, fill-only shapes get an outline in their own colour.

## Scene -> AutoCAD

`convert.to_pencil_strokes`: from the top shape down, what a later filled shape covers is cut away
(`shapely` difference); a filled shape is its visible outline plus hatching of its visible region
(spacing 2.6 / 3.4 / 5 / 7 units by luminance, cross-hatched when very dark); stroke-only shapes are drawn as
they are; hatching spacing grows until the stroke count fits 12 000.

## Verified live

PowerPoint: 24-shape SVG in 1.2 s (SSIM 0.994), 842-region photo in 54 s (0.986); Word and Excel read back clean;
Paint: 25-shape SVG in 75 s (canvas matches the plan, SSIM 0.978); AutoCAD: 3059 strokes in 3 s; planner end to end
("vẽ một con mèo trong PowerPoint" -> `art.draw` with its own SVG, verified, completed).

## Adding an application

Add an `AppProfile` in `capabilities.py` and a backend in `tools/art.py` that takes a `Scene`. Anything that can
import SVG already works through `app: "file"`.

# Art intelligence (2026-10-12): from "draw X" to a reasoned, verified painting in Paint

`art.brief` → `art.paint` put analysis in front of the Paint engine.

| Piece | File | Job |
|---|---|---|
| Modes | `art/modes.py` | 14 rendering modes (PENCIL_SKETCH … HYBRID_RENDERING): colour policy, brush preference, what the simulation is *not*. The user's words ("bút chì", "không tô màu", "trắng đen") beat the planner's choice. |
| Knowledge | `art/knowledge.py` | Small curated table (10 works, movements, media) + fetched references with source/licence/hash in `memory/art/references.json`. Built-in knowledge is reported as `BUILTIN_KNOWLEDGE`, never as verified. |
| Brief | `art/brief.py` | Classification A–E, identification (IDENTIFIED / AMBIGUOUS → question / UNKNOWN_ARTWORK), intent (REPRODUCTION / STUDY / INTERPRETATION / ADAPTATION / INSPIRED_BY), medium, mode, reference need, verification state. |
| Reference | `art/reference.py` | Wikimedia Commons search+download; scoring penalises gallery snapshots and posters, prefers Google Art Project files and the work's own aspect ratio. A title match is not proof of identity (state `REFERENCE_FETCHED`). |
| Rendering | `art/stylize.py`, `art/painterly.py` | One function per mode; brush modes paint coarse→fine with strokes that follow the picture's structure tensor and colours snapped to a small palette. |
| State | `art/state.py` | Task state machine; `COMPLETED` is impossible without VERIFYING; a JSON run log per call. |
| Engine | `paint/engine.py`, `paint/driver.py` | Per-object brush type + exact width; bucket fill only where the reference raster shows a closed region (< 45 % of the page), leak check after the fill + Ctrl+Z, shape-fill fallback; recoverable errors (focus, open menu, zoom) are recovered with a limit; `resume` draws only what is not on the canvas; `discover()` reads version, tools and the brush gallery of the running Paint. |

Measured on Paint 11.2605.81: the brush gallery offers Brush, Calligraphy brush, Calligraphy pen, Airbrush, Oil brush, Crayon, Marker,
Natural pencil, Watercolour brush; the canvas names them "Using Oil brush tool", "Calligraphy brush 1/2", "Fill with colour", "Pick colour".

Verification of an art.paint call: engine check against the plan + ink agreement (strokes) + mean colour error against the planned
raster (> 12 % fails: a translucent brush leaves ink in the right place in the wrong colour) + likeness (SSIM) reported, not hidden.
Honest limit: a Paint stroke picture of a busy painting is a likeness (SSIM ≈ 0.4–0.6), not a copy; Paint has no oil, no watercolour, no lacquer.

# Artwork Specification Gate (2026-10-12)

Nothing moves the mouse in Paint without a validated specification. `art/gate.py::run_gated` is the only way the tools
execute a plan (`paint.draw`, `paint.draw_image`, `art.draw` for Paint, `art.paint`), and `PaintDriver.draw` itself refuses
to run unless the `ExecutionGate` handed to it is in state DRAWING (POLICY_DENIED otherwise): a model cannot skip the gate by
calling another tool, because the check is in the code that draws.

```
ANALYZING_REQUEST -> ANALYZING_REFERENCE -> BUILDING_SPECIFICATION -> VALIDATING_SPECIFICATION -> BUILDING_DRAWING_PLAN
 -> CHECKING_READINESS -> READY_TO_DRAW -> DRAWING -> VALIDATING_RESULT -> COMPLETED   (AWAITING_USER_INPUT, RECOVERING, FAILED)
```

* `art/spec.py` builds the specification (groups A-H + `admin`: schema version, status, evidence, validation errors, unresolved
  fields, assumption log, provenance KNOWN / INFERRED / UNKNOWN, `not_applicable` reasons) deterministically from the planned
  objects, the brief, the real capabilities of the running Paint and the reference picture. Null is only legal with a reason.
* `art/gate.py::validate_spec` blocks: missing / mistyped fields, bad enums and `#RRGGBB`, points / boxes outside the canvas,
  contours leaving their box, occlusion that contradicts depth, palette against the mode (no hue in pencil / ink), brushes and tools
  this Paint does not offer, dependency cycles / missing / out-of-order dependencies, budgets (3000 operations, 3600 s), a
  physical-simulation claim, a bucket fill whose region is not closed (warning when the closed object can be shape-filled instead),
  and `verified` without a screenshot and a passing measured result.
* Readiness re-reads the canvas right before drawing: a changed canvas size or zoom stops the run (CONFLICT) with nothing drawn.
* The specification + state history are written to `artifacts/<task>/(paint|art)/spec-<artwork_id>.json`; each tool result carries
  `specification` (path, status, state, warnings, assumptions, unresolved).
* Tests: `tests/test_art_gate_unit.py` (20 required cases, fake canvas) — live: one `paint.draw` through the gate on a real Paint.
