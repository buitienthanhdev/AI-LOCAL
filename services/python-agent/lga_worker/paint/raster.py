"""Reference raster of a plan: what the canvas must look like, drawn with PIL.

This image is never the output (the picture is drawn in Paint with the mouse); it is the ground truth the
verifier compares the screenshot against, and the model on which a bucket fill is proven safe.
"""

from __future__ import annotations

from collections.abc import Iterable

from PIL import Image, ImageChops, ImageDraw

from lga_worker.paint.model import RGB, Canvas, Plan, PlannedObject

WHITE: RGB = (255, 255, 255)
SEED_MARK: RGB = (1, 2, 3)  # unlikely to occur in a drawing; marks the area reached by a flood fill


def _draw_object(draw: ImageDraw.ImageDraw, obj: PlannedObject) -> None:
    pts = list(obj.outline)
    if obj.fill is not None and obj.closed:
        draw.polygon(pts, fill=obj.fill)
    if obj.stroke is not None:
        path = [*pts, pts[0]] if obj.closed else pts
        draw.line(path, fill=obj.stroke, width=obj.stroke_px, joint="curve")
        if obj.stroke_px > 2:  # round the ends/corners that PIL leaves square
            r = obj.stroke_px / 2
            ends = pts if obj.closed else (pts[0], pts[-1])
            for x, y in ends:
                draw.ellipse((x - r, y - r, x + r, y + r), fill=obj.stroke)


def render(canvas: Canvas, objects: Iterable[PlannedObject]) -> Image.Image:
    """White canvas with `objects` drawn in the given order."""
    image = Image.new("RGB", (canvas.width, canvas.height), WHITE)
    draw = ImageDraw.Draw(image)
    for obj in objects:
        if obj.kind == "fill":  # what a bucket click would do to the picture so far
            if obj.fill is not None:
                seed = (round(obj.points[0][0]), round(obj.points[0][1]))
                if 0 <= seed[0] < canvas.width and 0 <= seed[1] < canvas.height:
                    ImageDraw.floodfill(image, seed, obj.fill, thresh=0)
            continue
        _draw_object(draw, obj)
    return image


def render_plan(plan: Plan, *, up_to_layer: int | None = None) -> Image.Image:
    objects = [o for o in plan.objects if up_to_layer is None or o.layer <= up_to_layer]
    return render(plan.canvas, objects)


def stroke_mask(canvas: Canvas, obj: PlannedObject) -> Image.Image:
    """'L' mask (255 where this object's own outline would be drawn)."""
    mask = Image.new("L", (canvas.width, canvas.height), 0)
    if obj.kind == "fill":
        return mask
    draw = ImageDraw.Draw(mask)
    pts = list(obj.outline)
    path = [*pts, pts[0]] if obj.closed else pts
    draw.line(path, fill=255, width=obj.stroke_px, joint="curve")
    return mask


def fill_mask(canvas: Canvas, obj: PlannedObject) -> Image.Image:
    mask = Image.new("L", (canvas.width, canvas.height), 0)
    if obj.closed:
        ImageDraw.Draw(mask).polygon(list(obj.outline), fill=255)
    return mask


def bucket_fill_region(image: Image.Image, seed: tuple[int, int], *, tolerance: int = 0) -> tuple[bool, int]:
    """What a bucket fill clicked at `seed` would cover on `image`: (enclosed, area in pixels).

    `enclosed` is False when the filled area reaches the image border, i.e. the region is not closed and the
    colour would flood the whole page (the failure of the Doraemon run: Fill clicked on an unclosed outline).
    """
    probe = image.convert("RGB")
    width, height = probe.size
    if not (0 <= seed[0] < width and 0 <= seed[1] < height):
        return False, 0
    ImageDraw.floodfill(probe, seed, SEED_MARK, thresh=tolerance)
    hit = color_mask(probe, SEED_MARK, 0)
    area = hit.histogram()[255]
    if area == 0:
        return False, 0
    edges = (hit.crop((0, 0, width, 1)), hit.crop((0, height - 1, width, height)),
             hit.crop((0, 0, 1, height)), hit.crop((width - 1, 0, width, height)))
    return all(edge.getbbox() is None for edge in edges), area


def color_mask(image: Image.Image, color: RGB, tolerance: int) -> Image.Image:
    """'L' mask, 255 where every channel is within `tolerance` of `color`."""
    mask: Image.Image | None = None
    for channel, target in zip(image.convert("RGB").split(), color, strict=True):
        near = channel.point(lambda v, t=target: 255 if abs(v - t) <= tolerance else 0)
        mask = near if mask is None else ImageChops.multiply(mask, near)
    assert mask is not None
    return mask
