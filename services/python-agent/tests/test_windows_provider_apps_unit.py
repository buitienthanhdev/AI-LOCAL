from __future__ import annotations

import time

from lga_worker.windows.windows_use_provider import WindowsUseProvider


def test_list_start_apps_filters_sorts_and_limits_cached_entries() -> None:
    provider = object.__new__(WindowsUseProvider)
    provider._start_apps = {
        "Zoom Workplace": "zoom.app",
        "Microsoft Paint": "paint.app",
        "Paint 3D": "paint3d.app",
        "Calculator": "calculator.app",
    }
    provider._start_apps_loaded_at = time.monotonic()

    apps, count = provider.list_start_apps("paint", 1)

    assert apps == ["Microsoft Paint"]
    assert count == 2


def test_list_start_apps_returns_a_stable_complete_name_list() -> None:
    provider = object.__new__(WindowsUseProvider)
    provider._start_apps = {
        "Zoom Workplace": "zoom.app",
        "Microsoft Paint": "paint.app",
        "Calculator": "calculator.app",
    }
    provider._start_apps_loaded_at = time.monotonic()

    apps, count = provider.list_start_apps(None, 100)

    assert apps == ["Calculator", "Microsoft Paint", "Zoom Workplace"]
    assert count == 3
