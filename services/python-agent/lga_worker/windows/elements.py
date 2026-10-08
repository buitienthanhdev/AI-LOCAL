"""Element references handed to the planner.

The model sees short IDs (`e17`) instead of raw UIA runtime IDs. The same live element keeps the same
ID across snapshots (keyed by runtime ID). When the live COM reference goes stale (the element was
re-created, e.g. after a re-render), the stored locator is used to find it again; if that is
ambiguous the caller gets STALE_ELEMENT and must re-inspect the UI.
"""

from __future__ import annotations

import secrets
import string
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from lga_worker.windows.provider import ElementLocator, ElementQuery


@dataclass
class CacheEntry:
    element_id: str
    control: Any  # windows_use.uia.Control (live COM proxy)
    runtime_id: tuple[int, ...]
    locator: ElementLocator


# Letters in the per-process id prefix. Ids from an earlier worker process (e.g. before a restart or a
# resumed task) then never collide with fresh ones and fail as unknown instead of hitting another element.
ID_PREFIX_LENGTH = 2


def new_id_prefix() -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(ID_PREFIX_LENGTH))


class ElementCache:
    def __init__(self, max_entries: int, prefix: str | None = None) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._prefix = prefix if prefix is not None else new_id_prefix()
        self._max_entries = max_entries
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._by_runtime_id: dict[tuple[int, ...], str] = {}
        self._counter = 0

    def register(self, control: Any, runtime_id: tuple[int, ...], locator: ElementLocator) -> str:
        existing = self._by_runtime_id.get(runtime_id) if runtime_id else None
        if existing is not None and existing in self._entries:
            entry = self._entries[existing]
            entry.control = control
            entry.locator = locator
            self._entries.move_to_end(existing)
            return existing
        self._counter += 1
        element_id = f"e{self._prefix}{self._counter}"
        self._entries[element_id] = CacheEntry(element_id, control, runtime_id, locator)
        if runtime_id:
            self._by_runtime_id[runtime_id] = element_id
        while len(self._entries) > self._max_entries:
            _, evicted = self._entries.popitem(last=False)
            if self._by_runtime_id.get(evicted.runtime_id) == evicted.element_id:
                del self._by_runtime_id[evicted.runtime_id]
        return element_id

    def get(self, element_id: str) -> CacheEntry | None:
        return self._entries.get(element_id)

    def rebind(self, element_id: str, control: Any, runtime_id: tuple[int, ...]) -> None:
        entry = self._entries[element_id]
        if entry.runtime_id and self._by_runtime_id.get(entry.runtime_id) == element_id:
            del self._by_runtime_id[entry.runtime_id]
        entry.control = control
        entry.runtime_id = runtime_id
        if runtime_id:
            self._by_runtime_id[runtime_id] = element_id
        self._entries.move_to_end(element_id)


def normalize_control_type(value: str) -> str:
    lowered = value.strip().lower().replace(" ", "")
    return lowered[: -len("control")] if lowered.endswith("control") else lowered


def matches_query(
    query: ElementQuery, *, name: str, control_type: str, automation_id: str, class_name: str
) -> bool:
    if query.name is not None and name.strip().lower() != query.name.strip().lower():
        return False
    if query.name_contains is not None and query.name_contains.strip().lower() not in name.lower():
        return False
    if query.control_type is not None and normalize_control_type(control_type) != normalize_control_type(
        query.control_type
    ):
        return False
    if query.automation_id is not None and automation_id != query.automation_id:
        return False
    return query.class_name is None or class_name == query.class_name
