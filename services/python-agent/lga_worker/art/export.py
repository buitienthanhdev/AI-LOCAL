"""Scene -> files: an SVG every vector program opens, and a PNG for everything else."""

from __future__ import annotations

from pathlib import Path

from lga_worker.art.scene import Scene, hex_of, render


def to_svg(scene: Scene) -> str:
    w, h = scene.width, scene.height
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}">']
    bg = scene.background or (255, 255, 255)
    out.append(f'<rect width="{w:g}" height="{h:g}" fill="{hex_of(bg)}"/>')
    for s in scene.shapes:
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in s.points)
        fill = hex_of(s.fill) if s.fill is not None else "none"
        stroke = f' stroke="{hex_of(s.stroke)}" stroke-width="{s.width:.2f}" stroke-linejoin="round"' if s.stroke else ""
        tag = "polygon" if s.closed and len(s.points) >= 3 else "polyline"
        out.append(f'<{tag} points="{pts}" fill="{fill if tag == "polygon" else "none"}"{stroke}/>')
    out.append("</svg>")
    return "\n".join(out)


def write_files(scene: Scene, directory: Path, stem: str, png_width: int = 2400) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    svg_path = directory / f"{stem}.svg"
    png_path = directory / f"{stem}.png"
    svg_path.write_text(to_svg(scene), encoding="utf-8")
    render(scene, png_width, supersample=2).save(png_path, format="PNG")
    return svg_path, png_path
