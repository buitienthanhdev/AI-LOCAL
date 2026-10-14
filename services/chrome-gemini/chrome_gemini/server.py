"""Loopback HTTP face of the adapter, called only by the gateway.

  GET  /health   -> adapter health (read-only)
  POST /shutdown -> close the Chrome this adapter owns, then exit (the gateway calls it before killing)
  POST /v1/chat  -> {"prompt", "transaction_id"?, "timeout_s"?, "attachments"?}  => TransactionResult.to_dict()

HTTP 200 is returned for every outcome that has a transaction status; the status, not the HTTP code,
is what the gateway acts on. Only a malformed request gets 400.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
from pathlib import Path

from aiohttp import web

from . import singleton
from .adapter import ChromeGeminiAdapter
from .cdp_page import GEMINI_URL, CdpGeminiPage, validate_gemini_url
from .contract import ChatRequest

MAX_PROMPT_CHARS = 200_000
DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 600.0
DEFAULT_SHUTDOWN_GRACE_S = 10.0
TOKEN_ENV = "LGA_ADAPTER_SHUTDOWN_TOKEN"
CHAT_TOKEN_ENV = "LGA_ADAPTER_CHAT_TOKEN"


def _exit() -> None:
    raise web.GracefulExit()


def _authorized(request: web.Request, token: str) -> bool:
    """Bearer check in constant time. An unset token authorises nobody: the endpoint is disabled, not open."""
    supplied = request.headers.get("Authorization", "")
    return bool(token) and hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())


def build_app(adapter: ChromeGeminiAdapter, shutdown_token: str = "", grace_s: float = DEFAULT_SHUTDOWN_GRACE_S,
              chat_token: str = "") -> web.Application:
    """/health is read-only and open on loopback. /v1/chat needs `chat_token`, /shutdown needs
    `shutdown_token`; the two are independent, so a caller that may ask questions cannot stop the adapter."""
    async def health(_: web.Request) -> web.Response:
        return web.json_response(await adapter.health())

    async def chat(request: web.Request) -> web.Response:
        if not _authorized(request, chat_token):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
            prompt = body["prompt"]
            if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
                raise ValueError("prompt must be a string of at most %d characters" % MAX_PROMPT_CHARS)
            timeout_s = float(body.get("timeout_s", DEFAULT_TIMEOUT_S))
            attachments = body.get("attachments") or []
            if not isinstance(attachments, list):
                raise ValueError("attachments must be a list")
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)
        result = await adapter.chat(ChatRequest(
            prompt=prompt,
            transaction_id=str(body.get("transaction_id") or ""),
            timeout_s=min(max(timeout_s, 1.0), MAX_TIMEOUT_S),
            attachments=[str(a) for a in attachments],
        ))
        return web.json_response(result.to_dict())

    async def transaction(request: web.Request) -> web.Response:
        if not _authorized(request, chat_token):
            return web.json_response({"error": "forbidden"}, status=403)
        record = await adapter.reconcile(request.match_info["tid"])
        if record is None:
            return web.json_response({"error": "unknown transaction"}, status=404)
        return web.json_response(record.to_dict())

    async def shutdown(request: web.Request) -> web.Response:
        # Loopback is not authentication: the gateway that started this process holds a random token.
        # No token configured means the endpoint is disabled, not open.
        if not _authorized(request, shutdown_token):
            return web.json_response({"error": "forbidden"}, status=403)
        outcome = await adapter.shutdown(grace_s)
        done = bool(outcome.get("done"))
        if done:
            asyncio.get_running_loop().call_later(0.2, _exit)  # only a completed shutdown ends the process
        # 200 = the adapter stopped (or will within a moment); 503 = it did NOT, so the caller can act at once.
        return web.json_response({"shutdown": done, **outcome, "pid": os.getpid()}, status=200 if done else 503)

    app = web.Application(client_max_size=MAX_PROMPT_CHARS * 4)
    app.add_routes([web.get("/health", health), web.post("/v1/chat", chat), web.post("/shutdown", shutdown),
                    web.get("/v1/transactions/{tid}", transaction)])
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Chrome Gemini adapter service (loopback only)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--selectors", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, default=None)
    parser.add_argument("--require-model", default=None,
                        help="send only when the page's model label equals this exactly (e.g. 'Gemini Pro')")
    parser.add_argument("--gemini-url", default=GEMINI_URL,
                        help="page to drive: https://gemini.google.com/app or a loopback http page (tests only)")
    parser.add_argument("--shutdown-grace-s", type=float, default=DEFAULT_SHUTDOWN_GRACE_S,
                        help="how long an exchange in flight may finish after /shutdown")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        singleton.acquire(args.profile_dir)  # one adapter per profile, whoever started it
    except singleton.ProfileBusy as exc:
        logging.getLogger(__name__).error("%s; exiting", exc)
        raise SystemExit(singleton.EXIT_CODE_PROFILE_BUSY) from None
    try:
        validate_gemini_url(args.gemini_url)
    except ValueError as exc:
        parser.error(str(exc))
    page = CdpGeminiPage(args.chrome, args.profile_dir, args.log_dir, args.selectors, gemini_url=args.gemini_url)
    # The token comes through the environment (never argv or a URL) and is removed so that the Chrome
    # this process launches does not inherit it.
    token = os.environ.pop(TOKEN_ENV, "")
    chat_token = os.environ.pop(CHAT_TOKEN_ENV, "")
    app = build_app(ChromeGeminiAdapter(page, require_model=args.require_model), token, args.shutdown_grace_s, chat_token)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)
