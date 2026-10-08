"""HTTP API of the gateway (loopback only). This is the `ChatProvider` the Rust core talks to.

  GET  /health               no auth; {"status": "ok" | "degraded" | "down"}
  GET  /v1/health            detailed backend health (?refresh=1 probes now)
  GET  /v1/models            models of all reachable backends
  POST /v1/upload            {"path": "..."} or {"data_base64": "...", "filename", "mime_type"}
  POST /v1/chat              ChatRequest JSON -> ChatResult JSON
  POST /v1/chat/stream       same body; Server-Sent Events: meta / delta / done / error
  POST /v1/admin/shutdown    stop the gateway and its managed backends

Chat request body:
  {"messages": [{"role": "system|user|assistant",
                 "content": "text" | [{"type": "text", "text": "..."}, {"type": "image", "attachment_id": "att_..."}]}],
   "model": "auto", "timeout_ms": 120000, "metadata": {"task_id": "...", "step_id": "..."}}
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import web

from lga_gateway.attachments import AttachmentError
from lga_gateway.errors import ErrorKind, GatewayError
from lga_gateway.gateway import GeminiGateway
from lga_gateway.types import BackendState, ChatRequest, ImagePart, Message, TextPart

logger = logging.getLogger(__name__)

GATEWAY_KEY = web.AppKey("gateway", GeminiGateway)
API_KEY = web.AppKey("api_key", str)
STOP_KEY = web.AppKey("stop", asyncio.Event)
# Delay between answering the shutdown request and stopping, so the reply reaches the caller.
SHUTDOWN_DELAY_S = 0.2
_VALID_ROLES = {"system", "user", "assistant"}


class RequestError(ValueError):
    pass


def parse_chat_request(body: Any) -> ChatRequest:
    if not isinstance(body, dict):
        raise RequestError("body must be a JSON object")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise RequestError("messages must be a non-empty list")
    messages: list[Message] = []
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict) or raw.get("role") not in _VALID_ROLES:
            raise RequestError(f"messages[{index}] needs a role in {sorted(_VALID_ROLES)}")
        content = raw.get("content")
        parts: list[TextPart | ImagePart] = []
        if isinstance(content, str):
            parts.append(TextPart(content))
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    raise RequestError(f"messages[{index}].content items must be objects")
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(TextPart(part["text"]))
                elif part.get("type") == "image" and isinstance(part.get("attachment_id"), str):
                    parts.append(ImagePart(part["attachment_id"]))
                else:
                    raise RequestError(f"messages[{index}] has an unsupported content part")
        else:
            raise RequestError(f"messages[{index}].content must be a string or a list")
        messages.append(Message(role=raw["role"], parts=tuple(parts)))
    timeout_ms = body.get("timeout_ms")
    if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms <= 0):
        raise RequestError("timeout_ms must be a positive integer")
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise RequestError("metadata must be an object")
    return ChatRequest(messages=tuple(messages), model=str(body.get("model") or "auto"),
                       timeout_s=timeout_ms / 1000 if timeout_ms else None,
                       metadata={str(k): str(v) for k, v in metadata.items()})


def _error(status: int, kind: str, message: str, attempts: list | None = None) -> web.Response:
    return web.json_response({"error": {"kind": kind, "message": message,
                                        "attempts": [asdict(a) for a in attempts or []]}}, status=status)


def _gateway_error_response(exc: GatewayError) -> web.Response:
    status = 400 if exc.kind is ErrorKind.CLIENT_ERROR else 503
    return _error(status, str(exc.kind), exc.message, exc.attempts)


@web.middleware
async def auth_and_access_log(request: web.Request, handler):
    started = time.monotonic()
    if request.path.startswith("/v1/"):
        expected = request.app[API_KEY]
        supplied = request.headers.get("Authorization", "")
        token = supplied[len("Bearer "):] if supplied.startswith("Bearer ") else ""
        if not hmac.compare_digest(token.encode(), expected.encode()):
            return _error(401, "unauthorized", "missing or invalid gateway API key")
    response = await handler(request)
    logger.info("%s %s -> %s in %dms", request.method, request.path, response.status,
                int((time.monotonic() - started) * 1000))
    return response


async def handle_health(request: web.Request) -> web.Response:
    health = await request.app[GATEWAY_KEY].health()
    states = {h.state for h in health}
    if BackendState.READY in states:
        status = "ok" if all(h.state is BackendState.READY for h in health) else "degraded"
    elif BackendState.DEGRADED in states:
        status = "degraded"
    else:
        status = "down"
    return web.json_response({"status": status})


async def handle_health_detail(request: web.Request) -> web.Response:
    refresh = request.query.get("refresh") in {"1", "true"}
    health = await request.app[GATEWAY_KEY].health(refresh=refresh)
    return web.json_response({"backends": [asdict(h) for h in health]})


async def handle_models(request: web.Request) -> web.Response:
    models = await request.app[GATEWAY_KEY].list_models()
    return web.json_response({"models": [asdict(m) for m in models]})


async def handle_upload(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error(400, "client_error", "body must be JSON")
    gateway = request.app[GATEWAY_KEY]
    try:
        if isinstance(body.get("path"), str):
            attachment = await gateway.upload(path=Path(body["path"]))
        elif isinstance(body.get("data_base64"), str):
            data = base64.b64decode(body["data_base64"], validate=True)
            attachment = await gateway.upload(data=data, filename=str(body.get("filename") or "image.png"),
                                              mime_type=body.get("mime_type"))
        else:
            return _error(400, "client_error", "provide path or data_base64")
    except (AttachmentError, binascii.Error, ValueError) as exc:
        return _error(400, "client_error", str(exc))
    return web.json_response({"attachment_id": attachment.attachment_id, "mime_type": attachment.mime_type,
                              "size": len(attachment.data)})


async def handle_chat(request: web.Request) -> web.Response:
    try:
        chat_request = parse_chat_request(await request.json())
    except (json.JSONDecodeError, RequestError) as exc:
        return _error(400, "client_error", str(exc))
    try:
        result = await request.app[GATEWAY_KEY].chat(chat_request)
    except GatewayError as exc:
        return _gateway_error_response(exc)
    except AttachmentError as exc:
        return _error(400, "client_error", str(exc))
    return web.json_response(asdict(result))


async def handle_chat_stream(request: web.Request) -> web.StreamResponse:
    try:
        chat_request = parse_chat_request(await request.json())
    except (json.JSONDecodeError, RequestError) as exc:
        return _error(400, "client_error", str(exc))
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await response.prepare(request)

    async def send(event: str, data: dict[str, Any]) -> None:
        await response.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())

    current_backend: str | None = None
    try:
        async for backend, delta in request.app[GATEWAY_KEY].stream(chat_request):
            if backend != current_backend:
                current_backend = backend
                await send("meta", {"backend": backend})
            await send("delta", {"content": delta.content, "reasoning": delta.reasoning})
        await send("done", {"backend": current_backend})
    except GatewayError as exc:
        await send("error", {"kind": str(exc.kind), "message": exc.message,
                             "attempts": [asdict(a) for a in exc.attempts]})
    except AttachmentError as exc:
        await send("error", {"kind": "client_error", "message": str(exc), "attempts": []})
    await response.write_eof()
    return response


async def handle_shutdown(request: web.Request) -> web.Response:
    stop = request.app[STOP_KEY]
    asyncio.get_running_loop().call_later(SHUTDOWN_DELAY_S, stop.set)
    return web.json_response({"stopping": True})


def build_app(gateway: GeminiGateway, api_key: str, client_max_size: int) -> web.Application:
    app = web.Application(middlewares=[auth_and_access_log], client_max_size=client_max_size)
    app[GATEWAY_KEY] = gateway
    app[API_KEY] = api_key
    app[STOP_KEY] = asyncio.Event()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health_detail)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/upload", handle_upload)
    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_post("/v1/chat/stream", handle_chat_stream)
    app.router.add_post("/v1/admin/shutdown", handle_shutdown)

    async def on_startup(_: web.Application) -> None:
        await gateway.start()

    async def on_cleanup(_: web.Application) -> None:
        await gateway.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
