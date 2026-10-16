---
name: autocad-ban-ve-nha
triggers: autocad, dwg, ban ve nha, ban ve ky thuat, mat bang, mat dung, mat cat, ban ve kien truc, kien truc nha, ho so thiet ke, ban ve cad
---
Residential drawings in AutoCAD, part 1: data and drawing (spec: docs/specs/SPEC_VE_BAN_VE_KY_THUAT_NHA_AUTOCAD.txt).
- Never invent dimensions, structure, legal/code facts or survey data. Missing = UNKNOWN or ASSUMED, never shown as fact. Each datum has source_status PROVIDED, MEASURED, IMPORTED, DERIVED, ASSUMED, UNKNOWN or CONFLICTED (+ file/entity/page). Conflicting sources: keep both, ask which wins.
- First get the Project Standards Profile (locality, house type, floors, units, paper, scale, layer/CTB convention, area rule). If key info is missing ask with options + "Khac" + "Giu UNKNOWN". Do not call a number a legal requirement unless verified.
- Work on a COPY; the source stays read-only. Model space 1:1 in real units (set INSUNITS); never scale to fit the screen. ByLayer colour/linetype/lineweight. Layers like A-WALL-EXT/INT, A-DOOR, A-WINDOW, A-GRID, A-DIMS, A-ANNO-TEXT, A-ROOM-TAG (map to the office template if given); separate geometry from dimensions/text/hatch.
- Walls need thickness or a stated centreline rule; doors/windows linked to a wall opening, not just a symbol on top; room boundary closed before any area, and say how area is measured; dimensions measure real geometry (an override that differs = CONFLICT, never silently fixed or rounded).
- Plan/elevation/section share one datum and one data model; door/window/room schedules come from the same data as the symbols; stairs: check total height / number of risers; limits only from a verified profile.
- Layout: right paper, locked viewport scale, title block; never fill in names, signatures or certificates.
