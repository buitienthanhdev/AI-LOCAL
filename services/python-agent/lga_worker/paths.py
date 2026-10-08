"""Path arguments of tools: environment variables and ~ expanded, must be absolute, normalised."""

from __future__ import annotations

import os
from pathlib import Path

from lga_worker.errors import ErrorCode, ToolError


def absolute_path(raw: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"path must be absolute: {raw!r}")
    return Path(os.path.normpath(expanded))
