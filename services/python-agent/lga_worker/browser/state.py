"""Pure helpers for the browser tools: URL host matching, page criteria and compact state serialisation.

Nothing here imports browser_use; DOM nodes are duck-typed (`tag_name`, `attributes`, `ax_node`,
`get_meaningful_text_for_llm()`), which keeps this module cheap to import and unit-testable with fakes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

ELEMENT_TEXT_CHARS = 120
ATTRIBUTE_VALUE_CHARS = 120
# Attributes worth showing to the planner (identify the element or its state); everything else is dropped.
ELEMENT_ATTRIBUTES = (
    "id", "name", "type", "role", "href", "placeholder", "aria-label", "title", "alt", "value", "for",
    "checked", "selected", "disabled", "aria-expanded", "aria-selected", "aria-checked", "contenteditable",
)
SENSITIVE_INPUT_TYPES = frozenset({"password"})
MASK = "***"
WWW_PREFIX = "www."
HTTP_SCHEMES = frozenset({"http", "https"})
ERROR_PAGE_PREFIXES = ("chrome-error://",)


class DomNode(Protocol):
    tag_name: str
    attributes: dict[str, str]
    ax_node: Any

    def get_meaningful_text_for_llm(self) -> str: ...


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def normalized_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[len(WWW_PREFIX):] if host.startswith(WWW_PREFIX) else host


def hosts_match(requested: str, final: str) -> bool:
    """True when both URLs point at the same site (case-insensitive, `www.` ignored).

    Non-http URLs (about:, data:, file:) match when the scheme and the rest are equal.
    """
    req, fin = urlsplit(requested), urlsplit(final)
    if req.scheme.lower() in HTTP_SCHEMES and fin.scheme.lower() in HTTP_SCHEMES:
        return bool(normalized_host(requested)) and normalized_host(requested) == normalized_host(final)
    return requested.rstrip("/") == final.rstrip("/")


def is_error_page(url: str) -> bool:
    return url.startswith(ERROR_PAGE_PREFIXES)


@dataclass(frozen=True)
class PageCriteria:
    """Conditions for browser.wait_for / browser.page_matches; all given ones must hold (case-insensitive)."""

    text_contains: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None

    @classmethod
    def from_args(cls, args: Mapping[str, Any]) -> PageCriteria:
        return cls(args.get("text_contains"), args.get("url_contains"), args.get("title_contains"))

    @property
    def empty(self) -> bool:
        return self.text_contains is None and self.url_contains is None and self.title_contains is None

    @property
    def needs_text(self) -> bool:
        return self.text_contains is not None

    def evaluate(self, url: str, title: str, text: str | None) -> tuple[bool, list[str]]:
        """(all criteria hold, one line per criterion explaining the outcome)."""
        results: list[str] = []
        ok = True
        for name, needle, haystack in (("url_contains", self.url_contains, url),
                                       ("title_contains", self.title_contains, title),
                                       ("text_contains", self.text_contains, text)):
            if needle is None:
                continue
            hit = haystack is not None and needle.lower() in haystack.lower()
            ok = ok and hit
            shown = truncate(haystack or "", ATTRIBUTE_VALUE_CHARS) if name != "text_contains" else "page text"
            results.append(f"{name} {needle!r}: {'yes' if hit else 'no'} ({shown})")
        return ok, results


def is_sensitive(node: DomNode) -> bool:
    return (node.attributes or {}).get("type", "").lower() in SENSITIVE_INPUT_TYPES


def element_role(node: DomNode) -> str | None:
    ax = getattr(node, "ax_node", None)
    role = getattr(ax, "role", None) if ax is not None else None
    return role or (node.attributes or {}).get("role") or None


def serialize_element(index: int, node: DomNode) -> dict[str, Any]:
    attributes = node.attributes or {}
    kept: dict[str, str] = {}
    for key in ELEMENT_ATTRIBUTES:
        if key in attributes:
            value = attributes[key]
            kept[key] = MASK if key == "value" and is_sensitive(node) else truncate(str(value), ATTRIBUTE_VALUE_CHARS)
    text = "" if is_sensitive(node) else truncate(node.get_meaningful_text_for_llm() or "", ELEMENT_TEXT_CHARS)
    return {"index": int(index), "tag": (node.tag_name or "").lower(), "text": text, "role": element_role(node),
            "attributes": kept}


# Identity of an element across state refreshes. Indices are CDP backend node ids, which are unique within a
# renderer but can repeat after a cross-site navigation; text is excluded because counters/values change.
FINGERPRINT_ATTRIBUTES = ("id", "name", "type", "href")


def element_fingerprint(node: DomNode) -> tuple[str, ...]:
    attributes = node.attributes or {}
    return ((node.tag_name or "").lower(), *(str(attributes.get(k, "")) for k in FINGERPRINT_ATTRIBUTES))


def describe_fingerprint(fingerprint: tuple[str, ...]) -> str:
    tag, *values = fingerprint
    shown = " ".join(f"{k}={truncate(v, ATTRIBUTE_VALUE_CHARS)!r}" for k, v in zip(FINGERPRINT_ATTRIBUTES, values) if v)
    return f"<{tag}{' ' + shown if shown else ''}>"


def serialize_elements(selector_map: Mapping[int, DomNode], max_elements: int) -> tuple[list[dict[str, Any]], bool]:
    items = list(selector_map.items())
    shown = [serialize_element(index, node) for index, node in items[:max_elements]]
    return shown, len(items) > max_elements


def serialize_tab(tab: Any, active_id: str | None) -> dict[str, Any]:
    return {"tab_id": tab.target_id, "url": tab.url, "title": tab.title or "", "active": tab.target_id == active_id}


def tab_line(tab: Mapping[str, Any]) -> str:
    marker = "*" if tab["active"] else " "
    return f"{marker} {tab['tab_id']} {truncate(tab['title'], ELEMENT_TEXT_CHARS)!r} {tab['url']}"


def resolve_tab_id(requested: str, tab_ids: Iterable[str]) -> str | None:
    """Exact target id, or a unique case-insensitive suffix of one (the planner may shorten ids)."""
    ids = list(tab_ids)
    if requested in ids:
        return requested
    lowered = requested.lower()
    matches = [tab_id for tab_id in ids if tab_id.lower().endswith(lowered)]
    return matches[0] if len(matches) == 1 else None


def state_observation(url: str, title: str, tabs: list[dict[str, Any]], dom_text: str, *, element_count: int,
                      shown: int, notes: Iterable[str], max_chars: int) -> str:
    header = [f"URL: {url}", f"Title: {title}"]
    if len(tabs) > 1:
        header.append(f"Tabs ({len(tabs)}, * = active):")
        header.extend(tab_line(t) for t in tabs)
    header.extend(notes)
    header.append(f"Interactive elements: {element_count}" + (f" (first {shown} in data)" if shown < element_count
                                                               else "") + " - [index]<tag> text; use the index:")
    head = "\n".join(header)
    budget = max(0, max_chars - len(head) - 1)
    body = dom_text if len(dom_text) <= budget else dom_text[:budget] + "\n... (truncated; scroll or narrow down)"
    return f"{head}\n{body}"
