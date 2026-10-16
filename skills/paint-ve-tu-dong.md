---
name: paint-ve-tu-dong
triggers: microsoft paint, mspaint, ve bang paint, ve trong paint, ve tren paint, to mau trong paint, canvas paint, ve tu dong, bang paint, trong paint, tren paint
---
Drawing in Microsoft Paint, part 1: plan and control (spec: docs/specs/SPEC_PAINT.txt; character rules: skills ve-doraemon-*).
- Flow: request > analysis > geometry plan > Paint operation plan > execute > check the image > fix > save > verify the saved file. Never click random coordinates; a finished click sequence is not a finished drawing, only a checked image is.
- Survey Paint first (version, window, maximised, canvas pixels, canvas origin and visible size on screen, zoom, tool/colours/width, dialogs, open file/unsaved). Prefer UI Automation; screen analysis only when needed. Never assume a fixed canvas position.
- Coordinates: canvas (origin top-left, px) is the reference; keep canvas/screen/viewport/source-image coordinates apart; screen = origin + canvas * scale; recompute after zoom/scroll/resize; test a few reference points and stop if the error exceeds the threshold.
- Model objects before drawing (object_id, label, geometry, bbox, outline/fill colour, stroke width, layer, draw order, dependencies); parts relate to each other; scale uniformly with the canvas, never stretch X and Y separately; keep a safe margin.
- Strokes by role (outline, structure, detail); native shape tools for exact shapes; curves as interpolated points without flooding mouse events; colours as HEX; never change a colour the user chose.
- A reference image means copying THAT image: size, silhouette, contour, colours, coarse to fine, compare after.
