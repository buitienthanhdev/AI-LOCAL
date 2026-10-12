"""Brush-stroke rendering of a reference picture (oil, acrylic, painterly).

Coarse to fine, as a painter works: every layer uses a smaller brush and only paints where the picture so far differs
from the reference. A stroke follows the local direction of the picture's structure (perpendicular to the gradient of a
smoothed structure tensor, so swirls, edges and contours are followed), takes the colour of the reference under its start
(snapped to a limited palette, because every colour change costs time on a real canvas) and stops when the reference
colour underneath drifts away. The canvas is simulated in memory so the next layer sees what the previous ones left.

Output is a list of `Shape`s: open polylines with a width (strokes) and one base polygon. Pure computation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from lga_worker.art.scene import Shape
from lga_worker.paint import vectorize


@dataclass(frozen=True)
class BrushStyle:
    widths: tuple[float, ...]  # brush width per layer in sheet units, coarse -> fine
    length: float  # stroke length as a multiple of the width
    threshold: float  # RGB error (0-441) a place must exceed to be painted again, from the second layer on
    colors: int  # palette size
    saturation: float = 1.0
    jitter: float = 0.12  # random variation of width and direction
    stop: float = 46.0  # colour drift (RGB distance) that ends a stroke
    budget: tuple[float, ...] = (0.34, 0.4, 0.26)  # share of the stroke budget per layer


OIL = BrushStyle(widths=(17.0, 8.5, 4.2), length=6.0, threshold=26.0, colors=26, saturation=1.12)
ACRYLIC = BrushStyle(widths=(14.0, 6.5), length=3.0, threshold=34.0, colors=20, saturation=1.05, jitter=0.06, stop=38.0,
                     budget=(0.55, 0.45))
PAINTERLY = BrushStyle(widths=(13.0, 6.5, 3.2), length=4.5, threshold=28.0, colors=24, saturation=1.05)


def _orientation(gray: np.ndarray, cv2: Any, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    sxx = cv2.GaussianBlur(gx * gx, (0, 0), sigma)
    syy = cv2.GaussianBlur(gy * gy, (0, 0), sigma)
    sxy = cv2.GaussianBlur(gx * gy, (0, 0), sigma)
    theta = 0.5 * np.arctan2(2 * sxy, sxx - syy)  # direction of the strongest gradient
    coherence = np.sqrt((sxx - syy) ** 2 + 4 * sxy ** 2) / (sxx + syy + 1e-3)
    return theta + math.pi / 2, coherence  # strokes run along the structure, not across it


def paint(image: Image.Image, style: BrushStyle, *, max_strokes: int = 1000, seed: int = 7,
          work_width: int = 520) -> tuple[list[Shape], dict[str, Any]]:
    import cv2

    rng = np.random.default_rng(seed)
    left, top, box_w, box_h = vectorize.fit_box(image.size, vectorize.Canvas(1152, 648))  # sheet-sized frame (1000 x 562.5)
    work_h = max(8, round(work_width * box_h / box_w))
    ref = np.asarray(image.convert("RGB").resize((work_width, work_h), Image.Resampling.LANCZOS))
    ppu = work_width / box_w

    # a limited palette fitted to the picture (what the brush will pick from)
    sample = ref.reshape(-1, 3)[rng.choice(work_width * work_h, size=min(6000, work_width * work_h), replace=False)]
    lab = vectorize._lab(cv2, np, sample)
    pal_lab = vectorize.free_palette(np, lab, np.ones(len(lab), np.float32), style.colors)
    pal_rgb = cv2.cvtColor(np.clip(pal_lab, 0, 255).astype(np.uint8).reshape(1, -1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    if style.saturation != 1.0:
        hsv = cv2.cvtColor(pal_rgb.reshape(1, -1, 3), cv2.COLOR_RGB2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * style.saturation, 0, 255)
        pal_rgb = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB).reshape(-1, 3)
        pal_lab = vectorize._lab(cv2, np, pal_rgb)

    gray = cv2.cvtColor(ref, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    gray = cv2.GaussianBlur(gray, (0, 0), 1.2)
    theta_fine, coh_fine = _orientation(gray, cv2, 4.0)
    theta_wide, _ = _orientation(gray, cv2, 14.0)
    flow = np.where(coh_fine > 0.35, theta_fine, theta_wide)  # flat areas follow the broad flow

    mean = ref.reshape(-1, 3).mean(axis=0)
    canvas = np.empty_like(ref)
    canvas[:] = mean.astype(np.uint8)
    shapes: list[Shape] = []
    stats: dict[str, Any] = {"layers": []}
    total_budget = max_strokes

    for li, width in enumerate(style.widths):
        wpx = width * ppu
        radius = wpx / 2
        blurred = cv2.GaussianBlur(ref, (0, 0), max(0.8, radius * (0.9 if li == 0 else 0.55)))
        # snapped picture for colour choice
        lab_b = cv2.cvtColor(blurred, cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)
        idx = np.empty(len(lab_b), np.int32)
        for start in range(0, len(lab_b), 60000):
            chunk = lab_b[start:start + 60000]
            idx[start:start + 60000] = ((chunk[:, None, :] - pal_lab[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        idx = idx.reshape(work_h, work_width)
        diff = np.sqrt(((canvas.astype(np.float32) - blurred) ** 2).sum(axis=2))
        layer_budget = max(1, int(total_budget * style.budget[min(li, len(style.budget) - 1)]))
        # a cell for every stroke the budget allows: the coarse layer then tiles the whole picture instead of keeping
        # only the places with the biggest error
        step = max(2.0, wpx * 0.75, math.sqrt(work_width * work_h / layer_budget) * (0.9 if li == 0 else 0.7))
        cells_x, cells_y = max(1, int(work_width / step)), max(1, int(work_h / step))
        err = cv2.resize(diff, (cells_x, cells_y), interpolation=cv2.INTER_AREA)
        cand: list[tuple[float, float, float]] = []
        for cy in range(cells_y):
            for cx in range(cells_x):
                e = float(err[cy, cx])
                if li > 0 and e < style.threshold:
                    continue
                x = (cx + 0.5 + rng.uniform(-0.3, 0.3)) * work_width / cells_x
                y = (cy + 0.5 + rng.uniform(-0.3, 0.3)) * work_h / cells_y
                cand.append((e, x, y))
        if len(cand) > layer_budget:
            cand.sort(key=lambda c: -c[0])
            cand = cand[:layer_budget]
        order = rng.permutation(len(cand))
        painted = 0
        sp = max(1.5, radius * 0.7)
        max_pts = max(3, int(style.length * wpx / sp))
        for k in order:
            _, x, y = cand[int(k)]
            xi, yi = int(min(max(x, 0), work_width - 1)), int(min(max(y, 0), work_h - 1))
            ci = int(idx[yi, xi])
            colour = pal_rgb[ci]
            pts = [(x, y)]
            prev: tuple[float, float] | None = None
            for i in range(max_pts):
                xi, yi = int(min(max(pts[-1][0], 0), work_width - 1)), int(min(max(pts[-1][1], 0), work_h - 1))
                a = float(flow[yi, xi]) + rng.normal(0.0, style.jitter * 0.5)
                d = (math.cos(a), math.sin(a))
                if prev is not None:
                    if d[0] * prev[0] + d[1] * prev[1] < 0:
                        d = (-d[0], -d[1])
                    d = (0.65 * prev[0] + 0.35 * d[0], 0.65 * prev[1] + 0.35 * d[1])
                    n = math.hypot(*d) or 1.0
                    d = (d[0] / n, d[1] / n)
                nx, ny = pts[-1][0] + sp * d[0], pts[-1][1] + sp * d[1]
                if not (0 <= nx < work_width and 0 <= ny < work_h):
                    break
                if i >= 2:
                    here = blurred[int(ny), int(nx)]
                    if float(np.sqrt(((here - colour.astype(np.float32)) ** 2).sum())) > style.stop:
                        break
                pts.append((nx, ny))
                prev = d
            if len(pts) < 2:
                pts.append((x + sp * 0.8, y))
            w = width * (1.0 + rng.uniform(-style.jitter, style.jitter))
            cv2.polylines(canvas, [np.array(pts, np.float32).round().astype(np.int32).reshape(-1, 1, 2)], False,
                          tuple(int(c) for c in colour), thickness=max(1, int(round(w * ppu))), lineType=cv2.LINE_AA)
            shapes.append(Shape(tuple((left + px / ppu, top + py / ppu) for px, py in pts), closed=False, fill=None,
                                stroke=(int(colour[0]), int(colour[1]), int(colour[2])), width=round(w, 2)))
            painted += 1
        stats["layers"].append({"width": width, "candidates": len(cand), "strokes": painted})
    base = Shape(((left, top), (left + box_w, top), (left + box_w, top + box_h), (left, top + box_h)), True,
                 (int(mean[0]), int(mean[1]), int(mean[2])), None, 1.0, "rect")
    out = [base, *shapes]
    stats.update({"strokes": len(shapes), "colors": len({s.stroke for s in shapes}), "box": [round(left, 1), round(top, 1),
                                                                                              round(box_w, 1), round(box_h, 1)]})
    return out, stats
