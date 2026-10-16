---
name: autocad-ban-ve-kiem-tra
triggers: autocad, dwg, ban ve nha, ban ve ky thuat, mat bang, mat dung, mat cat, ban ve kien truc, kien truc nha, ho so thiet ke, ban ve cad, kiem tra ban ve
---
Residential drawings in AutoCAD, part 2: running, checking, reporting.
- Automation: check AutoCAD version/API (LT differs) and the active document first; DRY_RUN preview then APPLY; transactions + backup; each operation has an ID so a retry creates no duplicates; detect modal dialogs and timeouts; checkpoint/cancel safely. Confirm before bulk delete, purge, explode, unit/scale change, overwrite. No untrusted LISP/scripts; no uploading the drawing to the cloud without permission.
- Validate by rule (rule_id, severity, PASS/FAIL/WARN/NOT_RUN, evidence): BLOCKER (will not open, wrong units, main geometry untrustworthy) / ERROR / WARNING / INFO. A BLOCKER means no PASS. Check file (xref, fonts, AUDIT), geometry (duplicates, open boundaries, self-intersection, zero length), annotation, cross-view coordination, plot.
- Plot: render the PDF to an image and look at it; a successful PLOT command proves nothing. Never overwrite an issued revision; make a new one with a change log.
- Report sections: INPUT_REVIEW, ASSUMPTIONS, QUESTIONS, DRAWING_PLAN, EXECUTION_LOG, VALIDATION, OPEN_ISSUES, OUTPUTS, FINAL_STATUS, LIMITATIONS.
- FINAL_STATUS is exactly one of DRAFT, CAD_VALID, DRAWING_CHECKED, COORDINATED, CODE_REVIEWED (issue for construction only by a responsible professional). Never write "industry standard" or "ready for construction". State which files exist and open, which tests ran, and what was NOT checked (e.g. no AutoCAD to open or render).
