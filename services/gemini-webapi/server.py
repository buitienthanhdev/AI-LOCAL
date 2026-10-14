"""Local OpenAI-compatible server over the Gemini web app, built on HanaokaYuzu/Gemini-API (`gemini_webapi`, AGPL-3.0
library used as an external dependency, not copied).

Why: the Go gateway's anonymous mode is refused by Google ("something went wrong") while this library's anonymous
session works, and with cookies it refreshes them in the background.

  python server.py --port 8084 [--cookies path/to/cookies.json]

cookies.json (optional, git-ignored): {"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}. Without it the
anonymous web session is used (text only). Loopback only. One request at a time (the web app is a single session).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from aiohttp import web

log = logging.getLogger("gemini-webapi")

CANNED = re.compile(
    r"something went wrong|hard time fulfilling your request|encountering an error|please try your request again|"
    r"unable to process your request",
    re.I,
)
MAX_PROMPT_CHARS = 120_000


def messages_to_prompt(messages: list[dict]) -> str:
    """Flatten an OpenAI messages list into one prompt (the web app takes a single text)."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = (content or "").strip()
        if not content:
            continue
        role = m.get("role", "user")
        if role == "system":
            parts.append(f"[Instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    prompt = "\n\n".join(parts)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:20_000] + "\n...\n" + prompt[-(MAX_PROMPT_CHARS - 20_000):]
    return prompt


def clean_reply(text: str) -> str:
    """Drop Gemini's built-in code-interpreter blocks, which are not part of the answer."""
    text = re.sub(r"```[a-z]*\?code_(?:reference|stdout)[^\n]*\n.*?```", "", text, flags=re.S)
    return text.strip()


class Server:
    def __init__(self, cookies_path: Path | None, model_name: str) -> None:
        self.cookies_path = cookies_path
        self.model_name = model_name
        self.client = None
        self.authenticated = False
        self.lock = asyncio.Lock()
        self.last_error = ""

    async def start(self) -> None:
        from gemini_webapi import GeminiClient

        psid = psidts = None
        if self.cookies_path and self.cookies_path.is_file():
            try:
                data = json.loads(self.cookies_path.read_text(encoding="utf-8"))
                psid, psidts = data.get("__Secure-1PSID"), data.get("__Secure-1PSIDTS")
            except (OSError, ValueError) as e:
                log.warning("cannot read cookies file: %s", e)
        self.authenticated = bool(psid)
        self.client = GeminiClient(psid, psidts) if psid else GeminiClient()
        await self.client.init(timeout=60, auto_close=False, auto_refresh=bool(psid))
        log.info("Gemini web client ready (%s)", "signed in" if self.authenticated else "anonymous")

    async def models(self, _request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": self.model_name, "object": "model", "owned_by": "gemini-web"}]})

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "authenticated": self.authenticated, "model": self.model_name, "last_error": self.last_error})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": {"message": "invalid JSON"}}, status=400)
        prompt = messages_to_prompt(body.get("messages") or [])
        if not prompt:
            return web.json_response({"error": {"message": "empty prompt"}}, status=400)
        async with self.lock:
            text = ""
            for attempt in range(2):
                try:
                    reply = await asyncio.wait_for(self.client.generate_content(prompt), timeout=150)
                    text = clean_reply(reply.text or "")
                except Exception as e:  # network, auth, parsing: report as an upstream failure
                    self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                    log.warning("generate failed: %s", self.last_error)
                    text = ""
                if text and not (len(text) < 200 and CANNED.search(text)):
                    break
                if text:
                    self.last_error = f"generic error reply: {text[:100]}"
                    text = ""
                await asyncio.sleep(1.5)
        if not text:
            return web.json_response({"error": {"message": f"Gemini web gave no usable answer ({self.last_error})"}}, status=502)
        self.last_error = ""
        created = int(time.time())
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        usage = {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4, "total_tokens": (len(prompt) + len(text)) // 4}
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
            await resp.prepare(request)
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": self.model_name,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
            end = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": self.model_name,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            await resp.write(f"data: {json.dumps(end)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            return resp
        return web.json_response({
            "id": cid, "object": "chat.completion", "created": created, "model": self.model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        })


async def run(port: int, cookies: Path | None, model: str) -> None:
    server = Server(cookies, model)
    await server.start()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes([
        web.get("/health", server.health), web.get("/v1/health", server.health), web.get("/v1/models", server.models),
        web.post("/v1/chat/completions", server.chat),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("listening on http://127.0.0.1:%d", port)
    await asyncio.Event().wait()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8084)
    ap.add_argument("--cookies", default=os.environ.get("GEMINI_WEBAPI_COOKIES", ""))
    ap.add_argument("--model", default="gemini-web")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(a.port, Path(a.cookies) if a.cookies else None, a.model))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
