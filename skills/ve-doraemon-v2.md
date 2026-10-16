---
name: ve-doraemon-v2
triggers: doraemon, ve doraemon, nhan vat hoat hinh
---
Doraemon in Paint, V2, part 1: design (spec: docs/specs/NANG_CAP_LOGIC_VE_DORAEMON.txt).
- "Ve Doraemon" is a known character: reference-based fidelity, never free_interpretation. Default: front, full body, upright, happy, flat colour, no perspective/shadow/props. Online: find a clean front reference (dora-world.com, tv-asahi, bandai) and save URL + time; offline: local template. reference_status = AVAILABLE or OFFLINE_TEMPLATE; never claim a match without a reference.
- Geometry in normalized coordinates, scaled uniformly to the canvas; never stretch X and Y separately. Constraints (a measured reference wins): figure 84-90% of canvas height, centred, nothing cut; silhouette w/h 0.72-0.86; head w/h 1.08-1.20 (not 1.29) and about half the figure; short round body.
- Named objects: head_outer, face_patch, eye_*, pupil_*, nose, nose_to_mouth, smile_curve, whisker_{left,right}_{top,middle,bottom}, torso, arm_*, hand_*, belly, pocket_outline, collar, bell, bell_groove, bell_dot, foot_*; each with fill, outline, width, bbox, depth. Curves = cubic Beziers sampled near-evenly.
- Fixes: collar = capsule; bell above collar, not covered by head; face patch must not cover collar; nose line ends at the upper middle of the smile, not deep inside; pocket = soft U outline with flat top; exactly 6 whiskers inside the head.
- Layer order (occlusion graph, arms behind torso): A background, feet, back arms, torso; B head, face, belly, hands, pocket; C collar, bell, groove, dot; D eye whites, pupils, nose, nose line, smile, whiskers.
- Palette baseline #0096E0 #FFFFFF #000000 #E60012 #FFD400 (change only by colours measured on a trusted reference). One stroke-width profile scaled by canvas (outer silhouette thickest, symmetric sides equal), not random 1/3/5 px.
