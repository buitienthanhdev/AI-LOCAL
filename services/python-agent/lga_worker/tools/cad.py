"""cad.build / cad.inspect (contract: tools/cad.json): parametric 3D parts and assemblies from text, through the
text-to-cad `cadgen` runtime (build123d on Open CASCADE) installed in its own virtual environment (runtime/cad).

The planner writes the shape code; this tool wraps it in a model script, builds STEP (and STL / GLB / 3MF on
request), then reads the STEP back and measures it: bounding box, volume, area, solids, validity, and an isometric
preview picture. The build's own traceback is returned on failure so the planner can repair the code.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "cadgen"
BUILD_TIMEOUT_S = 240
NAME_BAD = re.compile(r"[^A-Za-z0-9_\-]+")
IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+(?:build123d|cadgen)\b|import[ \t]+(?:build123d|cadgen)\b)[^\n]*\n?", re.M)
FORMATS = {"stl": "stl", "glb": "glb", "3mf": "threemf"}
NO_WINDOW = 0x08000000

INSPECT_SCRIPT = r'''
import json, sys
from cadgen import build123d as bd
path = sys.argv[1]
shape = bd.import_step(path)
bb = shape.bounding_box()
solids = shape.solids()
out = {
    "volume_mm3": round(float(shape.volume), 3), "area_mm2": round(float(shape.area), 3), "valid": bool(shape.is_valid),
    "solids": len(solids), "faces": len(shape.faces()), "edges": len(shape.edges()),
    "bbox_min": [round(float(bb.min.X), 3), round(float(bb.min.Y), 3), round(float(bb.min.Z), 3)],
    "bbox_max": [round(float(bb.max.X), 3), round(float(bb.max.Y), 3), round(float(bb.max.Z), 3)],
    "size_mm": [round(float(bb.size.X), 3), round(float(bb.size.Y), 3), round(float(bb.size.Z), 3)],
}
if len(sys.argv) > 2:
    try:
        import numpy as np
        from PIL import Image, ImageDraw
        verts, tris = shape.tessellate(0.2)
        v = np.array([[p.X, p.Y, p.Z] for p in verts], float)
        t = np.array(tris, int)
        tri = v[t]                                   # (n, 3, 3)
        mid = (v.max(axis=0) + v.min(axis=0)) / 2
        span = float((v.max(axis=0) - v.min(axis=0)).max()) or 1.0
        limit = span / 7.0
        for _ in range(5):                           # split big triangles so depth sorting is per small piece
            e = np.stack([np.linalg.norm(tri[:, i] - tri[:, (i + 1) % 3], axis=1) for i in range(3)], axis=1)
            big = e.max(axis=1) > limit
            if not big.any():
                break
            keep, cut = tri[~big], tri[big]
            a, b, c = cut[:, 0], cut[:, 1], cut[:, 2]
            ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
            tri = np.concatenate([keep, np.stack([a, ab, ca], 1), np.stack([ab, b, bc], 1), np.stack([ca, bc, c], 1),
                                  np.stack([ab, bc, ca], 1)])
        az, el = np.radians(-58), np.radians(26)
        rot_z = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
        rot_x = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
        R = rot_x @ rot_z
        pts = (tri - mid) @ R.T                      # x right, y depth, z up
        normal = np.cross(pts[:, 1] - pts[:, 0], pts[:, 2] - pts[:, 0])
        normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-9)
        light = np.array([-0.35, -0.7, 0.6]); light /= np.linalg.norm(light)
        shade = np.abs(normal @ light) * 0.55 + 0.45
        order = np.argsort(-pts[:, :, 1].mean(axis=1))   # far first
        W = H = 900
        scale = W * 0.38 / (span / 2 * 1.2)
        img = Image.new("RGB", (W, H), (245, 247, 250))
        d = ImageDraw.Draw(img)
        for k in order:
            poly = [(W / 2 + p[0] * scale, H / 2 - p[2] * scale) for p in pts[k]]
            c = shade[k]
            col = (int(70 + 110 * c), int(110 + 110 * c), int(165 + 80 * c))
            d.polygon(poly, fill=col, outline=col)
        img = img.resize((600, 600), Image.LANCZOS)
        ImageDraw.Draw(img).text((10, 8), "%.1f x %.1f x %.1f mm" % tuple(out["size_mm"]), fill=(60, 60, 60))
        img.save(sys.argv[2]); out["preview"] = sys.argv[2]
    except Exception as exc:  # the measurements are the answer; a missing picture is not fatal
        out["preview_error"] = repr(exc)[:160]
print("@@RESULT@@" + json.dumps(out))
'''


def _python(ctx: ToolContext) -> Path:
    path = ctx.config.root / "runtime" / "cad" / "Scripts" / "python.exe"
    if not path.is_file():
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        "the CAD runtime is not installed (runtime/cad). Install it: uv venv runtime/cad --python 3.13 && "
                        "uv pip install --python runtime/cad/Scripts/python.exe cadgen==0.7.21", retryable=False)
    return path


def _run(python: Path, args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(python), *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW,
                          env={**__import__("os").environ, "CADGEN_INSTALL_CHANNEL": "illuminati", "PYTHONIOENCODING": "utf-8"})


def _measure(python: Path, step: Path, preview: Path | None, cwd: Path) -> dict[str, Any]:
    script = cwd / "_inspect.py"
    script.write_text(INSPECT_SCRIPT, encoding="utf-8")
    args = [str(script), str(step)] + ([str(preview)] if preview else [])
    done = _run(python, args, cwd, 120)
    for line in done.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            import json

            return json.loads(line[len("@@RESULT@@"):])
    raise ToolError(ErrorCode.EXECUTION_FAILED, "could not measure the model: " + (done.stderr or done.stdout)[-600:])


def _tail(text: str, n: int = 1500) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith('{"model"')]
    return "\n".join(lines)[-n:]


def register(registry: ToolRegistry) -> None:
    @registry.handler("cad.build")
    async def build(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        name = NAME_BAD.sub("_", str(args.get("name") or "part")).strip("_")[:48] or "part"
        if name[0].isdigit():
            name = "part_" + name[:42]
        code = IMPORT_RE.sub("", str(args["code"]))  # bd and every build123d name are already in scope
        if "result" not in code:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "code must assign the finished shape to a variable named result")
        formats = [f for f in (args.get("formats") or ["stl", "glb"]) if f in FORMATS]
        python = _python(ctx)
        folder = ctx.artifact_dir("cad") / f"{name}-{int(time.time())}"
        folder.mkdir(parents=True, exist_ok=True)
        decorators = ['@step(out="out/%s.step")' % name] + ['@%s(out="out/%s.%s")' % (FORMATS[f], name, f) for f in formats]
        script = (
            "from cadgen import build123d as bd\n"
            f"from cadgen import step{''.join(', ' + FORMATS[f] for f in formats)}\n\n\n"
            + "\n".join(decorators) + "\n"
            + f"def {re.sub(r'[^A-Za-z0-9_]', '_', name)}():\n"
            + "    import build123d  # the kernel loads here, inside the model body, as cadgen requires\n"
            + "    globals().update({k: v for k, v in vars(build123d).items() if not k.startswith('_')})\n"
            + textwrap.indent(code.strip("\n"), "    ") + "\n"
            + "    try:\n        result.label = " + repr(name) + "\n    except Exception:\n        pass\n"
            + "    return result\n\n\n"
            + 'if __name__ == "__main__":\n    ' + re.sub(r"[^A-Za-z0-9_]", "_", name) + "()\n")
        model = folder / f"{name}.py"
        model.write_text(script, encoding="utf-8")

        def work() -> dict[str, Any]:
            try:
                done = _run(python, [str(model)], folder, min(BUILD_TIMEOUT_S, max(20.0, ctx.remaining_s() - 5)))
            except subprocess.TimeoutExpired as exc:
                raise ToolError(ErrorCode.TIMEOUT, f"the model took longer than {BUILD_TIMEOUT_S} s to build: simplify it "
                                                   "(fewer boolean operations, fewer fillets)", retryable=False) from exc
            step_path = folder / "out" / f"{name}.step"
            if done.returncode != 0 or not step_path.is_file():
                raise ToolError(ErrorCode.EXECUTION_FAILED,
                                "the model did not build. Fix the code and call cad.build again. Error:\n" + _tail(done.stderr or done.stdout),
                                retryable=False, details={"script": str(model)})
            measured = _measure(python, step_path, folder / "out" / f"{name}-preview.png", folder)
            return {"step": str(step_path), "measured": measured}

        out = await asyncio.to_thread(work)
        m = out["measured"]
        files = {"step": out["step"], **{f: str(folder / "out" / f"{name}.{f}") for f in formats if (folder / "out" / f"{name}.{f}").is_file()}}
        problems = []
        if not m["valid"]:
            problems.append("the solid is not valid (self-intersections or open shells)")
        if m["solids"] != 1 and not args.get("assembly"):
            problems.append(f"{m['solids']} separate solids (1 expected for a single part; pass assembly true for assemblies)")
        if m["volume_mm3"] <= 0:
            problems.append("zero volume: it is not a closed solid")
        want = args.get("expected_size_mm")
        if want and len(want) == 3:
            got = sorted(m["size_mm"])
            exp = sorted(float(x) for x in want)
            if any(abs(g - e) > max(0.5, 0.02 * e) for g, e in zip(got, exp, strict=True)):
                problems.append(f"bounding box {m['size_mm']} mm differs from the expected {want} mm")
        data = {"name": name, "script": str(model), "files": files, "measured": m, "problems": problems,
                "units": "mm", "preview": m.get("preview")}
        ok = not problems
        text = (f"Built {name}: {m['size_mm'][0]} x {m['size_mm'][1]} x {m['size_mm'][2]} mm, volume {m['volume_mm3']:.0f} mm³, "
                f"{m['faces']} faces, {m['solids']} solid(s), {'valid' if m['valid'] else 'INVALID'}. Files: "
                + ", ".join(f"{k} {v}" for k, v in files.items()) + "."
                + (f" Problems: {'; '.join(problems)}." if problems else ""))
        return ToolOutcome(data=data, checks=[Check("model_is_valid_solid", ok, "; ".join(problems) or "closed, valid, measured")],
                           artifacts=[{"kind": "screenshot", "path": m["preview"]}] if m.get("preview") else [], observation=text,
                           backend=BACKEND)

    @registry.handler("cad.inspect")
    async def inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        path = Path(str(args["path"]))
        if not path.is_file() or path.suffix.lower() not in (".step", ".stp"):
            raise ToolError(ErrorCode.NOT_FOUND, f"a .step / .stp file is needed: {path}")
        python = _python(ctx)
        folder = ctx.artifact_dir("cad") / f"inspect-{int(time.time())}"
        folder.mkdir(parents=True, exist_ok=True)
        preview = folder / "preview.png"
        m = await asyncio.to_thread(_measure, python, path, preview, folder)
        text = (f"{path.name}: {m['size_mm'][0]} x {m['size_mm'][1]} x {m['size_mm'][2]} mm, volume {m['volume_mm3']:.0f} mm³, "
                f"area {m['area_mm2']:.0f} mm², {m['faces']} faces, {m['solids']} solid(s), {'valid' if m['valid'] else 'INVALID'}.")
        return ToolOutcome(data=m, observation=text, backend=BACKEND,
                           artifacts=[{"kind": "screenshot", "path": m["preview"]}] if m.get("preview") else [])
