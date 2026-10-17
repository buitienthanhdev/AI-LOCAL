---
name: ve-doraemon-paint
triggers: doraemon, ve doraemon, nhan vat hoat hinh
---
Doraemon in Paint, V2, part 2: driving Paint and judging the result.
- No hard-coded screen_rect/zoom. Read the real canvas size, visible canvas rect, zoom and DPI; use ONE function: screen = canvas_screen_origin + canvas_xy * displayed_size / canvas_pixels; calibrate with safe points first and stop if the error is large. Before each stage check Paint is foreground, tool/colour/width are set and no dialog is open; never click blindly to close a dialog.
- Preview from the same scene as the drawing; fix a failing constraint BEFORE touching Paint; screenshot after every stage and compare with the preview; repair only failed objects (never wipe the canvas); bounded repair loops, list what stays wrong.
- Report four separate groups: execution_conformance, character_fidelity, visual_quality, canvas_safety. DRAW_EXECUTION_PASS never implies VISUAL_QUALITY_PASS; "completed/verified" only means executed. Do not claim anti-aliasing or similarity without checking the screenshot; open the final screenshot and give its real path.
- Learn only from observed screenshots or user feedback; save a template version with parameters, reference source, screenshot and metrics; a new version is a candidate and replaces the best one only after passing the quality gates; keep the offline template working.
- Tools: paint.draw template doraemon (add reference_image + reference_url when you found a front-view picture), paint.inspect, paint.save (read-back verified). Report DRAW_EXECUTION_PASS and VISUAL_QUALITY_PASS separately.
- Do not change how other drawings are made; keep Doraemon parameters separate from shared drawing code.
