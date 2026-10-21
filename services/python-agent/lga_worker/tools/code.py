"""code.run (contract: tools/code.json): hand a programming task to the opencode coding agent.

opencode (https://github.com/anomalyco/opencode, cloned next to this repository in ../opencode) runs headless
(`opencode run --standalone --auto`) inside the folder the planner names, with OpenRouter as its model provider.
It edits files and runs commands there; the final answer and the list of changed files come back to the planner.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.registry import Check, ToolContext, ToolOutcome, ToolRegistry

BACKEND = "opencode"
DEFAULT_MODEL = "ollama/gemma4:12b"
OLLAMA_URL = "http://127.0.0.1:11434/v1"
LIMIT_MARKERS = ("Rate limit exceeded", "monthly usage limit", "insufficient credits")
KEY_SECRET = "LGA_OPENROUTER_API_KEY"
NO_WINDOW = 0x08000000
MAX_ANSWER = 6000
MAX_RUN_S = 600.0
# opencode finishes when it believes it is done; without this it may leave a failing test it wrote itself.
SUFFIX = ("\n\nWhen you finish: run the code and every test you wrote, read the output, and fix failures (a failing test may "
          "be a wrong expectation: recount by hand). Stop only when everything passes, then say exactly what you ran and the result.")


def _opencode_dir(ctx: ToolContext) -> Path:
    path = Path(os.environ.get("LGA_OPENCODE_DIR") or ctx.config.root.parent / "opencode")
    if not (path / "packages" / "cli" / "src" / "index.ts").is_file():
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        f"opencode is not installed at {path}: git clone https://github.com/anomalyco/opencode and run `bun install` there "
                        "(or set LGA_OPENCODE_DIR)", retryable=False)
    return path


def _bun() -> str:
    found = shutil.which("bun") or str(Path.home() / ".bun" / "bin" / "bun.exe")
    if not Path(found).is_file():
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "bun is not installed (https://bun.sh)", retryable=False)
    return found


def _api_key(ctx: ToolContext) -> str:
    key = os.environ.get(KEY_SECRET, "")
    env_file = ctx.config.root / ".env"
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == KEY_SECRET:
                key = value.strip().strip('"')
    if not key:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"{KEY_SECRET} is not set in .env (OpenRouter key)", retryable=False)
    return key


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    skip = {".git", "node_modules", "__pycache__", ".venv", "target", "dist"}
    seen: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            seen[str(path.relative_to(folder))] = (stat.st_size, stat.st_mtime_ns)
            if len(seen) > 20000:
                return seen
    return seen


def register(registry: ToolRegistry) -> None:
    @registry.handler("code.run")
    async def run(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        workdir = Path(str(args["workdir"]))
        if not workdir.is_dir():
            raise ToolError(ErrorCode.NOT_FOUND, f"workdir is not an existing folder: {workdir}")
        opencode, bun = _opencode_dir(ctx), _bun()
        model = str(args.get("model") or os.environ.get("LGA_CODE_MODEL") or DEFAULT_MODEL)
        env = {**os.environ, "NO_COLOR": "1"}
        if model.startswith("openrouter/"):
            env["OPENROUTER_API_KEY"] = _api_key(ctx)
        elif model.startswith("ollama/"):
            # Ollama is not a built-in opencode provider: declare it (OpenAI-compatible endpoint) for this run only.
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"provider": {"ollama": {
                "npm": "@ai-sdk/openai-compatible", "name": "Ollama", "options": {"baseURL": OLLAMA_URL},
                "models": {model.split("/", 1)[1]: {"name": model.split("/", 1)[1]}}}}})
        argv = [bun, "run", "--cwd", str(opencode / "packages" / "cli"), "src/index.ts", "run", "--standalone", "--auto",
                "--model", model, str(args["task"]) + SUFFIX]
        timeout = min(MAX_RUN_S, max(30.0, ctx.remaining_s() - 10))
        before = await asyncio.to_thread(_snapshot, workdir)

        def work() -> subprocess.CompletedProcess[str]:
            return subprocess.run(argv, cwd=str(workdir), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, creationflags=NO_WINDOW,
                                  env=env)

        try:
            done = await asyncio.to_thread(work)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(ErrorCode.TIMEOUT, f"opencode did not finish within {timeout:.0f} s: split the task into smaller steps",
                            retryable=False) from exc
        after = await asyncio.to_thread(_snapshot, workdir)
        changed = sorted(p for p, sig in after.items() if before.get(p) != sig)
        answer = (done.stdout or "").strip()
        if any(m in answer or m in (done.stderr or "") for m in LIMIT_MARKERS):
            raise ToolError(ErrorCode.EXECUTION_FAILED, "the model provider refused (quota / rate limit): " + answer[-300:], retryable=False)
        if done.returncode != 0 or not answer:
            tail = ((done.stderr or "") + "\n" + answer).strip()[-1500:]
            raise ToolError(ErrorCode.EXECUTION_FAILED, f"opencode failed (exit {done.returncode}): {tail}", retryable=False)
        text = answer[-MAX_ANSWER:]
        if changed:
            text += "\n\nChanged files:\n" + "\n".join(changed[:60])
        return ToolOutcome(data={"workdir": str(workdir), "model": model, "changed_files": changed[:200], "answer": answer[-MAX_ANSWER:]},
                           checks=[Check("opencode_finished", True, f"exit 0, {len(changed)} file(s) changed")],
                           observation=text, backend=BACKEND)
