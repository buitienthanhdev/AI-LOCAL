"""Provider-neutral chat types. These are what the agent core sees; no Gemini specifics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    attachment_id: str


Part = TextPart | ImagePart


@dataclass(frozen=True)
class Message:
    role: Role
    parts: tuple[Part, ...]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def has_images(self) -> bool:
        return any(isinstance(p, ImagePart) for p in self.parts)


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[Message, ...]
    # "auto" lets each backend use its configured default model.
    model: str = "auto"
    timeout_s: float | None = None
    # Free-form correlation data (task_id, step_id) recorded in logs; never sent upstream.
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def has_images(self) -> bool:
        return any(m.has_images for m in self.messages)


@dataclass
class Attempt:
    backend: str
    ok: bool
    error_kind: str | None
    message: str | None
    duration_ms: int
    status: int | None = None


@dataclass
class ChatResult:
    content: str
    reasoning: str | None
    model: str
    backend: str
    usage: dict[str, Any] | None
    latency_ms: int
    attempts: list[Attempt]


@dataclass(frozen=True)
class StreamDelta:
    content: str
    reasoning: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    backend: str
    description: str = ""


class BackendState(StrEnum):
    READY = "ready"            # reachable and able to serve chats
    DEGRADED = "degraded"      # reachable but missing a capability (e.g. no account for images)
    UNAVAILABLE = "unavailable"


@dataclass
class BackendHealth:
    name: str
    kind: str
    state: BackendState
    detail: str
    capabilities: dict[str, bool]
    breaker: str
    checked_at: float
    process: dict[str, Any] | None = None
