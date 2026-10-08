"""Import shim for the Windows-Use primitive layer.

`windows_use/__init__.py` and `windows_use/agent/__init__.py` eagerly import the upstream LLM agent,
which pulls in every LLM SDK, enables PostHog telemetry, calls `load_dotenv()` on the current working
directory and creates `~/.windows-use/memory`. We only need the OS primitives in
`windows_use.agent.desktop`, `windows_use.agent.tree`, `windows_use.uia` and `windows_use.vdm`, none of
which depend on those package initialisers.

To use the primitives without the side effects, the two parent packages are registered in `sys.modules`
as plain namespace modules whose `__path__` points at the installed package directories. Python then
resolves submodules normally but never executes the two `__init__.py` files. Upstream files are not
modified. If a future upstream version moves these modules, the import fails loudly here.

Side effect that remains, by design: importing `windows_use.uia.core` makes the whole process
Per-Monitor-V2 DPI aware. All coordinates in this worker are therefore physical pixels.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_PARENT_PACKAGES = ("windows_use", "windows_use.agent")


class UpstreamImportError(ImportError):
    """Windows-Use is missing or its layout no longer matches what this shim expects."""


def _register_namespace_parents() -> None:
    if all(getattr(sys.modules.get(name), "__lga_shim__", False) for name in _PARENT_PACKAGES):
        return  # already registered by an earlier provider in this process
    spec = importlib.util.find_spec("windows_use")
    if spec is None or not spec.submodule_search_locations:
        raise UpstreamImportError(
            "windows-use is not installed in this environment; run `uv sync` in services/python-agent"
        )
    root = Path(next(iter(spec.submodule_search_locations)))
    for name in _PARENT_PACKAGES:
        existing = sys.modules.get(name)
        if existing is not None:
            if getattr(existing, "__lga_shim__", False):
                continue
            raise UpstreamImportError(
                f"{name} was already imported normally; the import shim must run before any other "
                "windows_use import"
            )
        relative = name.split(".")[1:]
        package_dir = root.joinpath(*relative)
        if not (package_dir / "__init__.py").is_file():
            raise UpstreamImportError(f"unexpected windows-use layout: {package_dir} is not a package")
        module = types.ModuleType(name)
        module.__path__ = [str(package_dir)]
        module.__package__ = name
        module.__lga_shim__ = True  # type: ignore[attr-defined]
        sys.modules[name] = module


def load() -> tuple[ModuleType, ModuleType]:
    """Return `(desktop_service_module, uia_module)` from Windows-Use without its package side effects."""
    _register_namespace_parents()
    with warnings.catch_warnings():
        # Windows-Use imports fuzzywuzzy, which warns when python-Levenshtein (GPL) is absent. We do not
        # install it; this worker's own fuzzy matching uses rapidfuzz.
        warnings.filterwarnings("ignore", message="Using slow pure-python SequenceMatcher")
        import windows_use.agent.desktop.service as desktop_service
        import windows_use.uia as uia

    return desktop_service, uia
