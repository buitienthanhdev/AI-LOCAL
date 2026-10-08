"""Fallback backend: WebAI-to-API (https://github.com/Amm1rr/WebAI-to-API, MIT).

Uses the stateless endpoint (the caller owns the history). Differences handled here:
* images must be `{"type": "file", "file": {...}}` parts; `image_url` is rejected with 422;
* sampling parameters are rejected with 400, so none are ever sent;
* errors use FastAPI's `{"detail": ...}` shape; 503 means its Gemini session is not ready.
"""

from __future__ import annotations

import json
from typing import Any

from lga_gateway.attachments import AttachmentStore
from lga_gateway.backends.base import Backend, ProbeResult
from lga_gateway.errors import BackendError, ErrorKind
from lga_gateway.types import BackendState, ModelInfo

PROBE_TIMEOUT_S = 10.0
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


class WebAiToApiBackend(Backend):
    kind = "webai_to_api"

    def __init__(self, name: str, base_url: str, default_model: str, attachments: AttachmentStore,
                 connect_timeout_s: float) -> None:
        super().__init__(name, base_url, default_model, attachments, connect_timeout_s)
        self._discovered_default: str | None = None

    def _headers(self) -> dict[str, str]:
        return {}

    def _chat_path(self) -> str:
        return "/v1/stateless/chat/completions"

    def resolve_model(self, requested: str) -> str:
        if requested not in ("", "auto"):
            return requested
        return self.default_model or self._discovered_default or ""

    def _image_part(self, attachment_id: str) -> dict[str, Any]:
        attachment = self._attachments.get(attachment_id)
        filename = f"{attachment.attachment_id}.{_EXT.get(attachment.mime_type, 'png')}"
        return {"type": "file", "file": {"filename": filename, "file_data": attachment.data_url()}}

    def _classify(self, status: int, body: str) -> BackendError:
        try:
            detail = (json.loads(body) or {}).get("detail", body)
        except (json.JSONDecodeError, AttributeError):
            detail = body
        message = str(detail)
        if status == 429:
            return BackendError(ErrorKind.RATE_LIMITED, f"{self.name}: rate limited: {message}", status)
        if status == 503:
            return BackendError(ErrorKind.BACKEND_DOWN, f"{self.name}: Gemini session not ready: {message}", status)
        if status == 404 and "model" in message.lower():
            return BackendError(ErrorKind.CAPABILITY, f"{self.name}: {message}", status)
        if status in (400, 422):
            return BackendError(ErrorKind.CLIENT_ERROR, f"{self.name}: {message}", status)
        if status in (401, 403):
            return BackendError(ErrorKind.CONFIG, f"{self.name}: {message}", status)
        return BackendError(ErrorKind.BACKEND_DOWN, f"{self.name}: upstream error {status}: {message}", status)

    async def probe(self) -> ProbeResult:
        status, _ = await self._get_json("/health", PROBE_TIMEOUT_S)
        if status != 200:
            return ProbeResult(BackendState.UNAVAILABLE, f"liveness returned {status}", {"chat": False, "images": False})
        try:
            models = await self.list_models()
        except BackendError as exc:
            return ProbeResult(BackendState.DEGRADED, f"alive but not signed in to Gemini: {exc.message}",
                               {"chat": False, "images": False})
        if not models:
            return ProbeResult(BackendState.DEGRADED, "alive but reports no models (sign-in required)",
                               {"chat": False, "images": False})
        return ProbeResult(BackendState.READY, f"{len(models)} models available", {"chat": True, "images": True})

    async def list_models(self) -> list[ModelInfo]:
        status, body = await self._get_json("/v1/stateless/models", PROBE_TIMEOUT_S)
        if status != 200:
            raise self._classify(status, json.dumps(body) if not isinstance(body, str) else body)
        items = body.get("data") if isinstance(body, dict) else body
        models = [ModelInfo(id=str(m.get("id")), backend=self.name, description=str(m.get("description") or ""))
                  for m in items or [] if isinstance(m, dict) and m.get("id")]
        if models and not self._discovered_default:
            self._discovered_default = models[0].id
        return models
