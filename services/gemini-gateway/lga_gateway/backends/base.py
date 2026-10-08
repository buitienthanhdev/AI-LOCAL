"""Backend contract and shared OpenAI-compatible HTTP client logic."""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

from lga_gateway.attachments import AttachmentStore
from lga_gateway.errors import BackendError, ErrorKind
from lga_gateway.types import BackendState, ChatRequest, ImagePart, ModelInfo, StreamDelta, TextPart

# Upstream error bodies are truncated before they are logged or returned.
MAX_ERROR_BODY_CHARS = 500


@dataclass(frozen=True)
class ProbeResult:
    state: BackendState
    detail: str
    capabilities: dict[str, bool]


@dataclass
class BackendReply:
    content: str
    reasoning: str | None
    model: str
    usage: dict[str, Any] | None


class Backend(ABC):
    """One Gemini Web implementation reachable over local HTTP."""

    name: str
    kind: str

    def __init__(self, name: str, base_url: str, default_model: str, attachments: AttachmentStore,
                 connect_timeout_s: float) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self._attachments = attachments
        self._connect_timeout_s = connect_timeout_s
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            # trust_env=False: never route localhost traffic through HTTP(S)_PROXY.
            self._session = aiohttp.ClientSession(trust_env=False)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(f"backend {self.name} used before start()")
        return self._session

    def _timeout(self, total_s: float) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=total_s, sock_connect=self._connect_timeout_s)

    @abstractmethod
    def _headers(self) -> dict[str, str]: ...

    @abstractmethod
    def _chat_path(self) -> str: ...

    @abstractmethod
    def _image_part(self, attachment_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def _classify(self, status: int, body: str) -> BackendError: ...

    @abstractmethod
    async def probe(self) -> ProbeResult: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    def resolve_model(self, requested: str) -> str:
        return self.default_model if requested in ("", "auto") else requested

    def _payload(self, request: ChatRequest, model: str, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if not message.has_images:
                messages.append({"role": message.role, "content": message.text})
                continue
            content: list[dict[str, Any]] = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    content.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append(self._image_part(part.attachment_id))
            messages.append({"role": message.role, "content": content})
        payload: dict[str, Any] = {"messages": messages, "stream": stream}
        if model:
            payload["model"] = model
        return payload

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> None:
        if response.status < 400:
            return
        body = (await response.text(errors="replace"))[:MAX_ERROR_BODY_CHARS]
        raise self._classify(response.status, body)

    def _transport_error(self, exc: BaseException) -> BackendError:
        if isinstance(exc, asyncio.TimeoutError):
            return BackendError(ErrorKind.BACKEND_DOWN, f"{self.name}: request timed out")
        return BackendError(ErrorKind.BACKEND_DOWN, f"{self.name}: {type(exc).__name__}: {exc}")

    async def chat(self, request: ChatRequest, timeout_s: float) -> BackendReply:
        model = self.resolve_model(request.model)
        url = self.base_url + self._chat_path()
        try:
            async with self.session.post(url, json=self._payload(request, model, stream=False),
                                         headers=self._headers(), timeout=self._timeout(timeout_s)) as response:
                await self._raise_for_status(response)
                data = await response.json(content_type=None)
        except BackendError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise self._transport_error(exc) from exc
        except json.JSONDecodeError as exc:
            raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: response is not JSON") from exc
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: unexpected response shape") from exc
        content = choice.get("content") or ""
        if not content.strip():
            raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: empty reply from Gemini")
        return BackendReply(content=content, reasoning=choice.get("reasoning_content"),
                            model=str(data.get("model") or model), usage=data.get("usage"))

    async def stream(self, request: ChatRequest, timeout_s: float) -> AsyncIterator[StreamDelta]:
        model = self.resolve_model(request.model)
        url = self.base_url + self._chat_path()
        saw_done = False
        emitted = False
        try:
            async with self.session.post(url, json=self._payload(request, model, stream=True),
                                         headers=self._headers(), timeout=self._timeout(timeout_s)) as response:
                await self._raise_for_status(response)
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: malformed SSE event") from exc
                    if "error" in event:
                        raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: {str(event['error'])[:MAX_ERROR_BODY_CHARS]}")
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content") or ""
                        reasoning = delta.get("reasoning_content")
                        if text or reasoning:
                            emitted = True
                            yield StreamDelta(content=text, reasoning=reasoning)
        except BackendError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise self._transport_error(exc) from exc
        if not saw_done:
            raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: stream ended without [DONE]")
        if not emitted:
            raise BackendError(ErrorKind.TRANSIENT, f"{self.name}: empty streamed reply")

    async def _get_json(self, path: str, timeout_s: float) -> tuple[int, Any]:
        try:
            async with self.session.get(self.base_url + path, headers=self._headers(),
                                        timeout=self._timeout(timeout_s)) as response:
                text = await response.text(errors="replace")
                try:
                    return response.status, json.loads(text) if text else None
                except json.JSONDecodeError:
                    return response.status, text[:MAX_ERROR_BODY_CHARS]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise self._transport_error(exc) from exc


def now() -> float:
    return time.time()
