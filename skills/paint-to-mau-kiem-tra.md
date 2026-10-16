---
name: paint-to-mau-kiem-tra
triggers: microsoft paint, mspaint, ve bang paint, ve trong paint, ve tren paint, to mau trong paint, canvas paint, ve tu dong, bang paint, trong paint, tren paint, to mau, fill leak
---
Drawing in Microsoft Paint, part 2: fill, check, repair, save.
- Fill: first check the contour is closed, no gaps/overlap and not connected to the background; fill so it can be undone; check after; stop on any leak (zoom in, close the gap, redo locally).
- Logical layers (background, big colour areas, back objects, main shapes, outline, inner detail, accents); do not paint over finished detail.
- After every stage take the canvas image and compare with the plan (position, proportion, broken lines, leaks, missing or hidden objects, colour). Classify errors (Coordinate, Geometry, Proportion, Stroke, Color, FillLeak, Occlusion, ToolSelection, CanvasState, Save), fix locally with the least-impact method, re-check the area and the whole; full redraw only if the structure is broken. Never repeat the same failed action unchanged.
- Safety: Paint must be foreground; never click or type into another window; do not close unsaved work or overwrite the original without permission; bounded retries; undo and checkpoints; stop and inspect when the state is unknown.
- Save: Save/SaveAs/export is done only when the file exists, reopens, has the right format and pixel size; an open Save dialog is not a save. Respect the overwrite policy.
- Learn after each task (request type, canvas, tools, plan, errors, causes, fixes, check result, time, user feedback), but change rules only with evidence and tests. Simulated tests do not count as real Paint control tests; report what was not run.
