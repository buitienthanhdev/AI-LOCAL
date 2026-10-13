---
name: cad-3d
triggers: 3d, mo hinh 3d, step, stl, 3mf, glb, in 3d, gia do, ban le, banh rang, vo hop, truc, tam kim loai, chi tiet co khi, build123d, thiet ke 3d, ve 3d, ngam
---
3D parts with cad.build (build123d, millimetres, +Z up). Write `result = <shape>`; `bd` is build123d; every build123d name (Box, Cylinder, Pos ...) is also in scope. Write no import lines and no function wrapper.
1. Put every dimension in a named constant first (WIDTH = 60, HOLE_D = 8 ...). State assumptions you had to make in your answer.
2. Use positional arguments only (bd.Box(60, 30, 6), bd.Pos(10, 0, 3)); keyword names like x=, y=, z= are not accepted. Primitives: bd.Box(x, y, z), bd.Cylinder(radius, height), bd.Sphere(r), bd.Cone(r1, r2, h), bd.Torus(R, r). Centered at the origin.
3. Place with bd.Pos(x, y, z) * shape, rotate with bd.Rot(rx, ry, rz) * shape. Combine: a + b (union), a - b (cut), a & b (intersect).
4. Holes: cut a Cylinder longer than the wall (height = wall + 2). Pattern: loop over positions and subtract each.
5. Fillet/chamfer: bd.fillet(part.edges().filter_by(bd.Axis.Z), radius=2) (select edges AFTER the last boolean; a fillet larger than half the thinnest wall fails).
6. 2D to 3D: bd.extrude(bd.Rectangle(w, h), amount=t); revolve profiles for shafts and bowls.
7. Check the result: pass expected_size_mm, read size / volume / solids / valid; exactly 1 solid for one part; fix and rebuild until it matches.
8. For 3D printing add 0.2-0.4 mm clearance to mating parts; minimum wall 1.2 mm; say the printing assumptions.
9. Everything is centred on its own origin, so offsets are from the CENTRE: an open box W x D x H with wall T and floor T is outer = bd.Box(W, D, H); inner = bd.Pos(0, 0, T) * bd.Box(W - 2*T, D - 2*T, H) (X and Y stay 0, only Z moves up by T); result = outer - inner. A part attached to a face sits at centre + half the sizes: wall of thickness T on the +X end of a base of width W: bd.Pos(W/2 - T/2, 0, ...) so the overall width stays W.
10. For simple shapes compute the expected volume by hand (W*D*H minus the removed volume) and compare with the measured volume; a mismatch means a misplaced cut or part: fix it before answering.
Errors come back as a traceback: change only the failing line.
