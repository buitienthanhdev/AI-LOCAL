---
name: cad-3d
triggers: 3d, mo hinh 3d, step, stl, 3mf, glb, in 3d, gia do, ban le, banh rang, vo hop, truc, tam kim loai, chi tiet co khi, build123d, thiet ke 3d, ve 3d, ngam
---
cad.build: build123d, mm, +Z up. Write `result = <shape>`; no imports (bd and Box, Cylinder, Pos ... are in scope).
1. Name every dimension as a constant first; state assumptions in the answer.
2. Positional args only: bd.Box(x, y, z), bd.Cylinder(r, h), bd.Sphere(r), bd.Cone(r1, r2, h), bd.Torus(R, r), bd.Pos(x, y, z) * shape, bd.Rot(rx, ry, rz) * shape. a + b union, a - b cut.
3. Every primitive is centred on its origin: offsets are from the CENTRE. Open box W,D,H wall T: bd.Box(W,D,H) - bd.Pos(0,0,T) * bd.Box(W-2*T, D-2*T, H). Wall on the +X end of a base of width W: bd.Pos(W/2 - T/2, 0, ...) so width stays W.
4. Holes: cut a Cylinder longer than the wall; loop to subtract patterns.
5. Fillet after the last boolean: bd.fillet(part.edges().filter_by(bd.Axis.Z), radius=2).
6. 2D to 3D: bd.extrude(bd.Rectangle(w, h), amount=t).
7. Pass expected_size_mm; compare measured volume with a hand calculation; exactly 1 solid; fix until it matches.
8. 3D printing: 0.2-0.4 mm clearance, wall >= 1.2 mm.
On an error change only the failing line.
