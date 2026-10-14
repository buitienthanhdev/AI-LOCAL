"""What the adapter needs from a Gemini page, independent of how the page is driven.

The real implementation is cdp_page.CdpGeminiPage; tests use a fake. Nothing here knows about
selectors, CDP or Chrome processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contract import ErrorClass


class PageError(Exception):
    """A page operation failed. `submitted_possible` says whether the prompt may have left the client."""

    def __init__(self, error_class: ErrorClass, message: str, submitted_possible: bool = False) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.submitted_possible = submitted_possible


@dataclass(frozen=True)
class PageState:
    turns: int  # number of model answers currently shown in the conversation
    text: str  # the last answer's text ("" when none yet)
    generating: bool | None  # None: the page gave no usable signal
    error: str | None = None  # visible error banner text, if any
    # The page shows its "waiting for the answer" state (UI evidence that the prompt was accepted). It is
    # NOT a completion signal and never counts towards COMPLETED.
    pending: bool | None = None
    # The user turns shown (count and the last one's text). Needed to attribute a LATE answer to its prompt;
    # None when the page driver has no `user_query` selector.
    user_turns: int | None = None
    user_text: str | None = None


MODEL_VERIFIED = "verified"  # the label was read from the page
MODEL_PENDING = "pending"  # page loading / label not rendered yet (the state while polling)
MODEL_UNKNOWN = "unknown"  # the deadline passed without a label; the model is NOT assumed


@dataclass(frozen=True)
class ModelReading:
    label: str | None
    status: str  # one of MODEL_*


@dataclass(frozen=True)
class SessionInfo:
    logged_in: bool
    model: str | None  # the model label the page shows; None when it cannot be read
    model_status: str = MODEL_UNKNOWN


class GeminiPage(Protocol):
    async def ensure_session(self) -> SessionInfo:
        """Open (or attach to) the dedicated browser. Raises PageError before anything is sent."""

    async def read_model(self) -> ModelReading:
        """Poll (bounded) for the current page's model label. Never clicks, never assumes."""

    async def new_conversation(self) -> int:
        """Start an empty conversation; return the answers it already shows (0 when clean)."""

    async def submit(self, prompt: str) -> None:
        """Type and send the prompt. Raises PageError(submitted_possible=...)."""

    async def read_state(self) -> PageState:
        """Read-only snapshot of the conversation. Raises PageError(CONNECTION_LOST)."""
