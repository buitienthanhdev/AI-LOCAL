from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from lga_common.config import Config, load_config  # noqa: F401

# Real-desktop tests open windows and move the mouse. They run only when explicitly requested:
#   uv run pytest -m desktop --run-desktop
SLOW_MARKERS = ("desktop", "autocad", "browser", "gateway")


def pytest_addoption(parser: pytest.Parser) -> None:
    for marker in SLOW_MARKERS:
        parser.addoption(f"--run-{marker}", action="store_true", help=f"run tests marked @{marker}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for marker in SLOW_MARKERS:
        if config.getoption(f"--run-{marker}") or os.environ.get(f"LGA_RUN_{marker.upper()}") == "1":
            continue
        skip = pytest.mark.skip(reason=f"needs --run-{marker}")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config()


@pytest.fixture
def scratch_dir(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
