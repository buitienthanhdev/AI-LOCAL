"""code.run: argument handling and result shaping with opencode mocked out (no network, no bun)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lga_worker.errors import ToolError
from lga_worker.registry import ToolRegistry, load_specs
from lga_worker.tools import code


def _ctx(root: Path) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(root=root), remaining_s=lambda: 900.0)


def _handler():
    registry = ToolRegistry(load_specs(Path(__file__).resolve().parents[3] / "tools"))
    code.register(registry)
    return registry._handlers["code.run"]  # noqa: SLF001


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (tmp_path / "opencode" / "packages" / "cli" / "src").mkdir(parents=True)
    (tmp_path / "opencode" / "packages" / "cli" / "src" / "index.ts").write_text("", encoding="utf-8")
    repo.mkdir()
    (repo / ".env").write_text("LGA_OPENROUTER_API_KEY=sk-or-v1-test\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(code, "_bun", lambda: "bun")
    monkeypatch.delenv("LGA_OPENCODE_DIR", raising=False)
    monkeypatch.delenv("LGA_OPENROUTER_API_KEY", raising=False)
    return repo, work


def test_reports_changed_files_and_passes_model_and_suffix(fake_env, monkeypatch):
    repo, work = fake_env
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs["env"]
        (work / "a.py").write_text("print(1)\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="done: ran a.py, passes", stderr="")

    monkeypatch.setattr(code.subprocess, "run", fake_run)
    out = asyncio.run(_handler()(_ctx(repo), {"task": "write a.py that prints 1", "workdir": str(work)}))
    assert out.data["changed_files"] == ["a.py"]
    assert "ollama/gemma4:12b" in seen["argv"]
    assert "OPENCODE_CONFIG_CONTENT" in seen["env"]
    assert seen["argv"][-1].endswith(code.SUFFIX)


def test_failure_and_missing_folder_raise(fake_env, monkeypatch):
    repo, work = fake_env
    monkeypatch.setattr(code.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"))
    with pytest.raises(ToolError, match="boom"):
        asyncio.run(_handler()(_ctx(repo), {"task": "write something useful", "workdir": str(work)}))
    with pytest.raises(ToolError, match="not an existing folder"):
        asyncio.run(_handler()(_ctx(repo), {"task": "write something useful", "workdir": str(work / "nope")}))
