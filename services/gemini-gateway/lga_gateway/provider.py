"""`ChatProvider`: the only LLM interface the agent core depends on.

`GeminiGateway` implements it in-process; the Rust core reaches the same five operations over the
gateway's HTTP API (`lga_gateway.server`). Future providers (Gemini API, Claude API, OpenAI API,
Ollama, vLLM) implement the same interface; see docs/architecture.md, "Model routing".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from lga_gateway.attachments import Attachment
from lga_gateway.types import BackendHealth, ChatRequest, ChatResult, ModelInfo, StreamDelta


class ChatProvider(ABC):
    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResult: ...

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[tuple[str, StreamDelta]]:
        """Yield (backend_name, delta) pairs."""

    @abstractmethod
    async def upload(self, *, path: Path | None = None, data: bytes | None = None,
                     filename: str = "image.png", mime_type: str | None = None) -> Attachment: ...

    @abstractmethod
    async def health(self, refresh: bool = False) -> list[BackendHealth]: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...
