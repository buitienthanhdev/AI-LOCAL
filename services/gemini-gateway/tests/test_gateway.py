"""Gateway routing tests against fake backends that mimic the real upstream response shapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lga_gateway.attachments import AttachmentStore
from lga_gateway.backends.web2api_go import Web2ApiGoBackend
from lga_gateway.backends.webai_to_api import WebAiToApiBackend
from lga_gateway.errors import ErrorKind, GatewayError
from lga_gateway.gateway import GeminiGateway, Route
from lga_gateway.resilience import BackoffPolicy, BreakerSettings, BreakerState, CircuitBreaker
from lga_gateway.server import build_app
from lga_gateway.types import ChatRequest, ImagePart, Message, TextPart

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@dataclass
class FakeBehaviour:
    reply: str = "pong"
    stream_done: bool = True
    # Error response returned for the first `fail_times` calls (None = every call).
    fail_status: int | None = None
    fail_body: dict[str, Any] | None = None
    fail_times: int | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def next_failure(self) -> tuple[int, dict[str, Any]] | None:
        if self.fail_status is None:
            return None
        if self.fail_times is not None:
            if self.fail_times <= 0:
                return None
            self.fail_times -= 1
        return self.fail_status, self.fail_body or {}


def _openai_reply(text: str) -> dict[str, Any]:
    return {"model": "fake", "choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


async def _fake_server(kind: str, behaviour: FakeBehaviour) -> TestServer:
    async def chat(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        behaviour.calls.append(payload)
        failure = behaviour.next_failure()
        if failure is not None:
            return web.json_response(failure[1], status=failure[0])
        if payload.get("stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for token in [t for t in behaviour.reply.split(" ") if t]:
                chunk = {"choices": [{"delta": {"content": token + " "}}]}
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            if behaviour.stream_done:
                await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        return web.json_response(_openai_reply(behaviour.reply))

    async def primary_root(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": "test"})

    async def primary_models(_: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "gemini-3.6-flash"}]})

    async def fallback_health(_: web.Request) -> web.Response:
        return web.Response(status=200)

    async def fallback_models(_: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "gemini-3-flash"}]})

    app = web.Application()
    if kind == "primary":
        app.router.add_get("/", primary_root)
        app.router.add_get("/v1/models", primary_models)
        app.router.add_post("/v1/chat/completions", chat)
    else:
        app.router.add_get("/health", fallback_health)
        app.router.add_get("/v1/stateless/models", fallback_models)
        app.router.add_post("/v1/stateless/chat/completions", chat)
    server = TestServer(app)
    await server.start_server()
    return server


def _gateway(primary_url: str, fallback_url: str, store: AttachmentStore, threshold: int = 2) -> GeminiGateway:
    settings = BreakerSettings(failure_threshold=threshold, open_duration_s=60, max_open_duration_s=600)
    primary = Web2ApiGoBackend("primary", primary_url, "gemini-3.6-flash", store, 2.0, "k", None)
    fallback = WebAiToApiBackend("fallback", fallback_url, "", store, 2.0)
    return GeminiGateway(
        [Route(primary, CircuitBreaker(settings), None, 5), Route(fallback, CircuitBreaker(settings), None, 5)],
        store, BackoffPolicy(2, 0.01, 0.02, 2.0, 0.0), request_timeout_s=10, health_interval_s=3600,
    )


def _request(*parts) -> ChatRequest:
    return ChatRequest(messages=(Message("system", (TextPart("be brief"),)), Message("user", tuple(parts))))


@pytest.fixture
def store() -> AttachmentStore:
    return AttachmentStore(1_000_000, 5_000_000, 600)


async def test_primary_serves_when_healthy(store: AttachmentStore) -> None:
    p, f = FakeBehaviour(reply="from primary"), FakeBehaviour(reply="from fallback")
    ps, fs = await _fake_server("primary", p), await _fake_server("fallback", f)
    gw = _gateway(str(ps.make_url("")), str(fs.make_url("")), store)
    await gw.start()
    try:
        result = await gw.chat(_request(TextPart("ping")))
        assert result.backend == "primary" and result.content == "from primary"
        assert f.calls == []
    finally:
        await gw.close()
        await ps.close()
        await fs.close()


async def test_failover_when_primary_down_and_breaker_opens(store: AttachmentStore) -> None:
    f = FakeBehaviour(reply="from fallback")
    fs = await _fake_server("fallback", f)
    gw = _gateway("http://127.0.0.1:9", str(fs.make_url("")), store, threshold=2)  # port 9: nothing listens
    await gw.start()
    try:
        for _ in range(3):
            result = await gw.chat(_request(TextPart("ping")))
            assert result.backend == "fallback"
        assert gw.routes[0].breaker.state is BreakerState.OPEN
        # With the breaker open the primary is skipped without a connection attempt.
        result = await gw.chat(_request(TextPart("ping")))
        assert result.attempts[0].error_kind == "breaker_open"
    finally:
        await gw.close()
        await fs.close()


async def test_client_error_is_not_failed_over(store: AttachmentStore) -> None:
    p = FakeBehaviour(fail_status=400, fail_body={"error": {"message": "too long", "type": "invalid_request_error",
                                                            "code": "context_length_exceeded"}})
    f = FakeBehaviour()
    ps, fs = await _fake_server("primary", p), await _fake_server("fallback", f)
    gw = _gateway(str(ps.make_url("")), str(fs.make_url("")), store)
    await gw.start()
    try:
        with pytest.raises(GatewayError) as exc:
            await gw.chat(_request(TextPart("ping")))
        assert exc.value.kind is ErrorKind.CLIENT_ERROR
        assert f.calls == []
    finally:
        await gw.close()
        await ps.close()
        await fs.close()


async def test_rate_limit_is_retried_on_same_backend(store: AttachmentStore) -> None:
    p = FakeBehaviour(fail_status=429, fail_body={"error": {"message": "ip_slot_full", "type": "rate_limit_exceeded"}},
                      fail_times=1, reply="after retry")
    f = FakeBehaviour()
    ps, fs = await _fake_server("primary", p), await _fake_server("fallback", f)
    gw = _gateway(str(ps.make_url("")), str(fs.make_url("")), store)
    await gw.start()
    try:
        result = await gw.chat(_request(TextPart("ping")))
        assert result.backend == "primary" and result.content == "after retry"
        assert len(p.calls) == 2 and f.calls == []
    finally:
        await gw.close()
        await ps.close()
        await fs.close()


async def test_capability_error_fails_over_with_image_parts_converted(store: AttachmentStore) -> None:
    p = FakeBehaviour(fail_status=400, fail_body={"error": {"message": "image input needs a Google account cookie",
                                                            "type": "invalid_request_error"}})
    f = FakeBehaviour(reply="saw image")
    ps, fs = await _fake_server("primary", p), await _fake_server("fallback", f)
    gw = _gateway(str(ps.make_url("")), str(fs.make_url("")), store)
    await gw.start()
    try:
        attachment = await gw.upload(data=PNG, filename="s.png")
        result = await gw.chat(_request(TextPart("what is this"), ImagePart(attachment.attachment_id)))
        assert result.backend == "fallback"
        primary_parts = p.calls[0]["messages"][1]["content"]
        fallback_parts = f.calls[0]["messages"][1]["content"]
        assert primary_parts[1]["type"] == "image_url"
        assert fallback_parts[1]["type"] == "file"
        assert fallback_parts[1]["file"]["file_data"].startswith("data:image/png;base64,")
        assert "temperature" not in f.calls[0]
        # Capability mismatches do not count against the primary's breaker.
        assert gw.routes[0].breaker.state is BreakerState.CLOSED
    finally:
        await gw.close()
        await ps.close()
        await fs.close()


async def test_stream_without_done_fails_over_before_first_token(store: AttachmentStore) -> None:
    p = FakeBehaviour(reply="x", stream_done=False)
    f = FakeBehaviour(reply="hello there")
    ps, fs = await _fake_server("primary", p), await _fake_server("fallback", f)
    gw = _gateway(str(ps.make_url("")), str(fs.make_url("")), store)
    await gw.start()
    try:
        with pytest.raises(GatewayError):
            # The primary emits a token and then breaks: that cannot be spliced, so the error surfaces.
            async for _ in gw.stream(_request(TextPart("hi"))):
                pass
        p.reply = ""  # now the primary breaks before emitting anything -> failover is safe
        chunks = [(b, d.content) async for b, d in gw.stream(_request(TextPart("hi")))]
        assert {b for b, _ in chunks} == {"fallback"}
        assert "".join(c for _, c in chunks).strip() == "hello there"
    finally:
        await gw.close()
        await ps.close()
        await fs.close()


async def test_http_api_requires_key_and_serves_chat(store: AttachmentStore, aiohttp_client) -> None:
    p = FakeBehaviour(reply="api ok")
    ps = await _fake_server("primary", p)
    gw = _gateway(str(ps.make_url("")), "http://127.0.0.1:9", store)
    client = await aiohttp_client(build_app(gw, "secret-key-123", 10_000_000))
    try:
        assert (await client.get("/health")).status == 200
        denied = await client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        assert denied.status == 401
        headers = {"Authorization": "Bearer secret-key-123"}
        ok = await client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=headers)
        assert ok.status == 200
        body = await ok.json()
        assert body["content"] == "api ok" and body["backend"] == "primary"
        bad = await client.post("/v1/chat", json={"messages": []}, headers=headers)
        assert bad.status == 400
    finally:
        await ps.close()


def test_breaker_half_open_probe_and_backoff_growth() -> None:
    now = [0.0]
    breaker = CircuitBreaker(BreakerSettings(1, 10, 25), clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allow()
    now[0] = 10.0
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow() and not breaker.allow()  # only one probe at a time
    breaker.record_failure()  # failed probe -> open again for 20s
    now[0] = 25.0
    assert breaker.state is BreakerState.OPEN
    now[0] = 30.0
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_backoff_is_bounded() -> None:
    policy = BackoffPolicy(5, 0.5, 4.0, 2.0, 0.0)
    assert [policy.delay(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 4.0, 4.0]
