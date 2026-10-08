"""Secret redaction applied to everything the worker logs or returns as free text."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# (pattern, keep_prefix_group): when keep_prefix_group is set, that group is kept and the rest of the
# match is replaced; otherwise the whole match is replaced.
_RULES: tuple[tuple[re.Pattern[str], int | None], ...] = (
    # Google session cookies.
    (re.compile(r"(\b(?:__Secure-[0-9A-Za-z]+PSID[A-Z]*|SAPISID|APISID|HSID|SSID|SID|NID)=)[^;\s\"']+"), 1),
    # Whole Cookie / Set-Cookie header values.
    (re.compile(r"(?i)(\b(?:set-)?cookie\s*[:=]\s*)[^\r\n]+"), 1),
    # Authorization headers and bearer tokens.
    (re.compile(r"(?i)(\bauthorization\s*[:=]\s*)[^\r\n\"',]+"), 1),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"), 1),
    # key=value and "key": "value" secrets.
    (
        re.compile(
            r"(?i)([\"']?\b(?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|admin[_-]?token|"
            r"client[_-]?secret|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"
        ),
        1,
    ),
    # Well-known key formats.
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,})\b"), None),
)

MIN_LITERAL_LENGTH = 6


class Redactor:
    def __init__(self, extra_env_names: Iterable[str] = ()) -> None:
        self._literals: list[str] = []
        for name in extra_env_names:
            self.add_literal(os.environ.get(name))

    def add_literal(self, value: str | None) -> None:
        """Always redact this exact value (e.g. a resolved secret)."""
        if value and len(value) >= MIN_LITERAL_LENGTH and value not in self._literals:
            self._literals.append(value)

    def __call__(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, REDACTED)
        for pattern, keep_group in _RULES:
            if keep_group is None:
                text = pattern.sub(REDACTED, text)
            else:
                text = pattern.sub(lambda m, g=keep_group: m.group(g) + REDACTED, text)
        return text


DEFAULT_REDACTOR = Redactor()


def redact(text: str) -> str:
    return DEFAULT_REDACTOR(text)
