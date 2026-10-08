"""Primary backend: gemini-web2api-go (https://github.com/zexadev/gemini-web2api-go, MIT).

Run as a separate process; spoken to over its OpenAI-compatible API. Behaviour references point into
references/gemini-web2api-go at the pinned commit.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from lga_gateway.attachments import AttachmentStore
from lga_gateway.backends.base import Backend, ProbeResult
from lga_gateway.errors import BackendError, ErrorKind
from lga_gateway.types import BackendState, ModelInfo

PROBE_TIMEOUT_S = 5.0
# Phrases in 400 responses that mean "this backend cannot do it", not "your request is wrong".
_CAPABILITY_MARKERS = ("needs a google account cookie", "requires login", "unknown model", "not available",
                       "model not found", "unsupported model")


class Web2ApiGoBackend(Backend):
    kind = "gemini_web2api_go"

    def __init__(self, name: str, base_url: str, default_model: str, attachments: AttachmentStore,
                 connect_timeout_s: float, api_key: str | None, admin_token: str | None) -> None:
        super().__init__(name, base_url, default_model, attachments, connect_timeout_s)
        self._api_key = api_key
        self._admin_token = admin_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _chat_path(self) -> str:
        return "/v1/chat/completions"

    def _image_part(self, attachment_id: str) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": self._attachments.get(attachment_id).data_url()}}

    def _classify(self, status: int, body: str) -> BackendError:
        try:
            error = (json.loads(body) or {}).get("error") or {}
        except (json.JSONDecodeError, AttributeError):
            error = {}
        message = str(error.get("message") or body)
        code = str(error.get("code") or "")
        lowered = message.lower()
        if status == 429:
            return BackendError(ErrorKind.RATE_LIMITED, f"{self.name}: rate limited: {message}", status)
        if status in (401, 403):
            return BackendError(ErrorKind.CONFIG, f"{self.name}: local API key rejected", status)
        if status == 400 and code == "context_length_exceeded":
            return BackendError(ErrorKind.CLIENT_ERROR, f"{self.name}: prompt too long: {message}", status)
        if status == 400 and any(marker in lowered for marker in _CAPABILITY_MARKERS):
            return BackendError(ErrorKind.CAPABILITY, f"{self.name}: {message}", status)
        if 400 <= status < 500:
            return BackendError(ErrorKind.CLIENT_ERROR, f"{self.name}: {message}", status)
        return BackendError(ErrorKind.BACKEND_DOWN, f"{self.name}: upstream error {status}: {message}", status)

    async def _has_account_cookie(self) -> bool | None:
        """Whether an enabled, healthy Google account cookie is configured.

        Uses the admin API (`GET /admin/api/cookies` -> {"items", "total", "enabled"}; see
        internal/app/admin_cookies.go). Returns None when the admin API cannot be asked.
        """
        if not self._admin_token:
            return None
        try:
            async with self.session.get(self.base_url + "/admin/api/cookies",
                                        headers={"Authorization": f"Bearer {self._admin_token}"},
                                        timeout=self._timeout(PROBE_TIMEOUT_S)) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        return any(isinstance(item, dict) and item.get("status") == "enabled" and item.get("health") != "dead"
                   for item in items)

    async def probe(self) -> ProbeResult:
        status, body = await self._get_json("/", PROBE_TIMEOUT_S)
        if status != 200 or not isinstance(body, dict) or body.get("status") != "ok":
            return ProbeResult(BackendState.UNAVAILABLE, f"liveness returned {status}", {"chat": False, "images": False})
        has_cookie = await self._has_account_cookie()
        caps = {"chat": True, "images": bool(has_cookie)}
        version = body.get("version", "?")
        if has_cookie is False:
            return ProbeResult(BackendState.DEGRADED,
                               f"v{version}: anonymous mode (no Google account cookie): text only", caps)
        if has_cookie is None:
            return ProbeResult(BackendState.READY, f"v{version}: account status unknown", caps)
        return ProbeResult(BackendState.READY, f"v{version}: account cookie present", caps)

    async def list_models(self) -> list[ModelInfo]:
        status, body = await self._get_json("/v1/models", PROBE_TIMEOUT_S)
        if status != 200 or not isinstance(body, dict):
            raise self._classify(status, json.dumps(body) if not isinstance(body, str) else body)
        return [ModelInfo(id=str(m.get("id")), backend=self.name, description=str(m.get("description") or ""))
                for m in body.get("data") or [] if isinstance(m, dict) and m.get("id")]
