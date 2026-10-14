"""Wire contract of the Chrome Gemini adapter.

This is the Python side of the provider contract in services/gemini-gateway/src/provider.rs; the
two must stay in step (the Rust unit tests pin the JSON shape).

`COMPLETED` is a statement about the transaction only: the prompt was sent once and a finished answer
was read back. It says nothing about whether the answer is correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TransactionStatus(str, Enum):
    NOT_SUBMITTED = "NOT_SUBMITTED"  # nothing reached the page; safe to try again or fail over
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"  # the prompt may have been sent; never re-sent
    GENERATING = "GENERATING"  # sent, the answer was not proven complete
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # sent, and the page reported an error


class ErrorClass(str, Enum):
    NONE = "none"
    BUSY = "busy"  # another transaction owns the browser
    BROWSER_UNAVAILABLE = "browser_unavailable"
    NOT_LOGGED_IN = "not_logged_in"
    SELECTORS_UNVERIFIED = "selectors_unverified"
    CONVERSATION_NOT_CLEAN = "conversation_not_clean"
    UNSUPPORTED = "unsupported"  # images / files: refused, never silently dropped
    SUBMIT_FAILED = "submit_failed"
    CONNECTION_LOST = "connection_lost"
    INCOMPLETE = "incomplete"  # timed out while the answer was still generating
    PAGE_ERROR = "page_error"
    DUPLICATE = "duplicate"  # same transaction id seen twice with a different prompt
    MODEL_MISMATCH = "model_mismatch"  # the page shows another (or no readable) model than required; nothing sent
    SHUTTING_DOWN = "shutting_down"  # refused: the adapter is stopping; nothing was sent
    SHUTDOWN_INTERRUPTED = "shutdown_interrupted"  # shutdown grace ran out while a transaction was in flight


@dataclass(frozen=True)
class Capabilities:
    text: bool = True
    images: bool = False
    files: bool = False
    streaming: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"text": self.text, "images": self.images, "files": self.files, "streaming": self.streaming}


@dataclass
class ChatRequest:
    prompt: str
    transaction_id: str = ""
    timeout_s: float = 120.0
    # Attachments are part of the contract so that a caller sending one gets UNSUPPORTED, not silence.
    attachments: list[str] = field(default_factory=list)


@dataclass
class TransactionResult:
    transaction_id: str
    status: TransactionStatus
    content: str = ""
    error_class: ErrorClass = ErrorClass.NONE
    error_message: str = ""
    model_verified: str | None = None  # read from the page, never assumed from configuration
    started_at: float = 0.0
    finished_at: float = 0.0
    signals: dict[str, Any] = field(default_factory=dict)  # completion evidence, no secrets
    # An answer that appeared AFTER this transaction had already ended without one. Recorded next to the
    # terminal result, never instead of it: status, error and finished_at stay exactly as they were reported.
    late_observation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "status": self.status.value,
            "content": self.content,
            "error": None
            if self.error_class is ErrorClass.NONE
            else {"class": self.error_class.value, "message": self.error_message},
            "late_observation": self.late_observation,
            "metadata": {
                "provider": "chrome_gemini",
                "model_verified": self.model_verified,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "signals": self.signals,
            },
        }
