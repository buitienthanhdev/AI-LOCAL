"""The Python redactor must agree with the Rust one on the shared security corpus (tests/security/corpus.json).

Policy itself is enforced only in the Rust core; the worker receives calls over the core's pipe and has no shell or
path policy of its own. What both sides share is secret redaction, which is what this checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lga_common.redact import redact

CORPUS = json.loads((Path(__file__).resolve().parents[3] / "tests" / "security" / "corpus.json").read_text(encoding="utf-8"))
CASES = CORPUS["redaction"]["must_redact"] + CORPUS["redaction"]["known_gaps"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["secret"][:24])
def test_corpus_secrets_are_redacted(case):
    out = redact(case["text"])
    assert case["secret"] not in out
    assert "[REDACTED]" in out


def test_ordinary_text_is_left_alone():
    text = "Report 2026-10-14: version 1.2.3, 42 files, path C:\\temp\\a.txt, https://example.org/page?id=7"
    assert redact(text) == text
