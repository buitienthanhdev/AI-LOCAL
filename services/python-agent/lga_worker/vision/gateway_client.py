"""Minimal client for the local Gemini gateway (services/gemini-gateway), used for vision requests.

Blocking `urllib` calls are run in worker threads by the caller. Proxies are disabled explicitly: the
gateway is on loopback and must never be reached through a system or environment proxy.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lga_common.config import Config, resolve_secret

from lga_worker.errors import ErrorCode, ToolError

UPLOAD_TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ChatAnswer:
    content: str
    backend: str
    model: str


class GatewayClient:
    def __init__(self, config: Config) -> None:
        self._base = str(config.get("llm.gateway_url")).rstrip("/")
        key = resolve_secret(config, str(config.get("gateway.api_key_secret")))
        if not key:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            "gateway API key is not configured; run scripts/setup-gateway.ps1")
        self._key = key
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read() or b"{}").get("error", {})
            except json.JSONDecodeError:
                error = {}
            message = error.get("message") or f"HTTP {exc.code}"
            code = ErrorCode.INVALID_ARGUMENT if exc.code == 400 else ErrorCode.BACKEND_UNAVAILABLE
            raise ToolError(code, f"gateway: {message}", details={"status": exc.code, "kind": error.get("kind")}) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"gateway unreachable at {self._base}: {exc}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health", None, HEALTH_TIMEOUT_S)

    def upload(self, path: Path) -> str:
        body = self._request("POST", "/v1/upload", {"path": str(path)}, UPLOAD_TIMEOUT_S)
        attachment_id = body.get("attachment_id")
        if not isinstance(attachment_id, str):
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "gateway upload returned no attachment id")
        return attachment_id

    def chat(self, system: str, text: str, attachment_ids: list[str], timeout_s: float) -> ChatAnswer:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend({"type": "image", "attachment_id": a} for a in attachment_ids)
        body = self._request("POST", "/v1/chat", {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "model": "auto", "timeout_ms": int(timeout_s * 1000),
        }, timeout_s + UPLOAD_TIMEOUT_S)
        return ChatAnswer(content=str(body.get("content") or ""), backend=str(body.get("backend") or ""),
                          model=str(body.get("model") or ""))
