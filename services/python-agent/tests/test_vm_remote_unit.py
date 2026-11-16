"""Unit tests of the agent-VM channel, workspace guard and router (no VM, real sockets on loopback)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.remote import channel as ch
from lga_worker.remote.host import VmRouter, VmSettings
from lga_worker.remote.transport import Endpoint
from lga_worker.remote.workspace import Workspace


async def _pair(host_key: bytes, guest_key: bytes) -> tuple[ch.SecureChannel, ch.SecureChannel, list[BaseException]]:
    errors: list[BaseException] = []
    accepted: asyncio.Future[ch.SecureChannel] = asyncio.get_running_loop().create_future()

    async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        guest = ch.SecureChannel(reader, writer, "guest")
        try:
            await guest.handshake(guest_key)
        except ch.ChannelError as exc:
            errors.append(exc)
        accepted.set_result(guest)

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    host = ch.SecureChannel(reader, writer, "host")
    try:
        await host.handshake(host_key)
    except ch.ChannelError as exc:
        errors.append(exc)
    guest = await accepted
    server.close()
    return host, guest, errors


def test_handshake_and_round_trip() -> None:
    async def run() -> None:
        key = bytes(range(32))
        host, guest, errors = await _pair(key, key)
        assert not errors
        await host.send({"type": "ping", "request_id": "1", "text": "xin chào"})
        assert await guest.recv() == {"type": "ping", "request_id": "1", "text": "xin chào"}
        await guest.send({"type": "pong", "request_id": "1"})
        assert (await host.recv())["type"] == "pong"
        await host.close()
        await guest.close()

    asyncio.run(run())


def test_wrong_key_is_rejected_both_ways() -> None:
    async def run() -> None:
        host, guest, errors = await _pair(b"a" * 32, b"b" * 32)
        assert errors and all(isinstance(e, ch.AuthError) for e in errors)
        with pytest.raises(ch.ChannelError):
            await host.send({"type": "ping"})

    asyncio.run(run())


def test_tampered_replayed_and_reordered_frames_are_rejected() -> None:
    async def run() -> None:
        key = b"k" * 32
        host, guest, _ = await _pair(key, key)
        # forge a frame by writing raw JSON with a bad MAC
        await host._write_raw({"seq": 1, "body": {"type": "request", "tool": "desktop.click"}, "mac": "00" * 32})
        with pytest.raises(ch.AuthError):
            await guest.recv()

        host2, guest2, _ = await _pair(key, key)
        await host2.send({"type": "ping", "request_id": "a"})
        frame_seq = host2._send_seq
        assert (await guest2.recv())["request_id"] == "a"
        # replay the same sequence number
        await host2._write_raw({"seq": frame_seq, "body": {"type": "ping", "request_id": "a"}, "mac": "x"})
        with pytest.raises(ch.ChannelError):
            await guest2.recv()

    asyncio.run(run())


def test_reflection_is_rejected() -> None:
    """A frame the guest signed cannot be sent back to it as if the host wrote it."""
    async def run() -> None:
        key = b"r" * 32
        host, guest, _ = await _pair(key, key)
        await guest.send({"type": "pong", "request_id": "1"})
        raw = await host._read_raw()  # capture the guest's frame without verifying
        await host._write_raw(raw)
        with pytest.raises(ch.AuthError):
            await guest.recv()

    asyncio.run(run())


def test_key_file_creation_and_short_key(tmp_path: Path) -> None:
    key = ch.new_key(tmp_path / "k" / "vm.key")
    assert len(key) == 32 and ch.load_key(tmp_path / "k" / "vm.key") == key
    (tmp_path / "short.key").write_text("abcd", encoding="ascii")
    with pytest.raises(ch.AuthError):
        ch.load_key(tmp_path / "short.key")


@pytest.mark.parametrize("bad", ["C:\\Windows\\win.ini", "\\\\server\\share\\x", "/etc/passwd", "..\\x", "a/../../x",
                                 "a:stream", "CON", "nul.txt", "dir/trail. ", ""])
def test_workspace_refuses_escapes(tmp_path: Path, bad: str) -> None:
    ws = Workspace(tmp_path / "ws", tmp_path / "audit.jsonl", "host")
    with pytest.raises(ToolError) as exc:
        ws.resolve(bad)
    assert exc.value.code == ErrorCode.POLICY_DENIED
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["verdict"] == "denied"


def test_workspace_write_read_checksum_and_audit(tmp_path: Path) -> None:
    import hashlib

    ws = Workspace(tmp_path / "ws", tmp_path / "audit.jsonl", "guest")
    data = "nội dung".encode()
    ws.write("sub/a.txt", data, hashlib.sha256(data).hexdigest(), "t1", "host")
    assert ws.read("sub/a.txt", "t1")[0] == data
    with pytest.raises(ToolError):
        ws.write("b.txt", data, "0" * 64, "t1")
    verdicts = [json.loads(line)["verdict"] for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert verdicts == ["ok", "ok", "denied"]


def test_workspace_refuses_junction_out(tmp_path: Path) -> None:
    import subprocess

    ws = Workspace(tmp_path / "ws", tmp_path / "audit.jsonl", "guest")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(ws.root / "j"), str(outside)], check=True, capture_output=True)
    with pytest.raises(ToolError):
        ws.read("j/secret.txt")


def _settings(tmp_path: Path) -> VmSettings:
    return VmSettings(enabled=True, endpoint="tcp:127.0.0.1:1", key_file=tmp_path / "k", tools=["desktop.*", "paint.draw"],
                      host_workspace=tmp_path / "hw", host_artifacts=tmp_path / "art", audit_path=tmp_path / "a.jsonl")


def test_router_routes_by_pattern(tmp_path: Path) -> None:
    router = VmRouter(_settings(tmp_path))
    assert router.handles("desktop.click") and router.handles("paint.draw")
    assert not router.handles("browser.click") and not router.handles("paint.inspect")


def test_router_refuses_host_paths_outside_the_workspace(tmp_path: Path) -> None:
    router = VmRouter(_settings(tmp_path))

    async def run() -> None:
        with pytest.raises(ToolError) as exc:
            await router._translate_args({"image_path": "C:\\Users\\someone\\secret.png"}, "", "t")
        assert exc.value.code == ErrorCode.POLICY_DENIED
        assert await router._translate_args({"text": "C:\\not-a-path-key"}, "", "t") == {"text": "C:\\not-a-path-key"}
        # a not-yet-existing output inside the workspace is rewritten without a transfer
        out = await router._translate_args({"path": str(tmp_path / "hw" / "out" / "a.png")}, "", "t")
        assert out == {"path": "vm-workspace:out/a.png"}

    asyncio.run(run())


def test_endpoint_parsing() -> None:
    assert Endpoint.parse("tcp:127.0.0.1:5000") == Endpoint("tcp", "127.0.0.1", "5000")
    e = Endpoint.parse("hv:11111111-2222-3333-4444-555555555555:6c6761c1-0a4e-4f5d-9a1b-7a6d0c2b9e01")
    assert e.kind == "hv"
    with pytest.raises(ValueError):
        Endpoint.parse("hv:nope:nope")
