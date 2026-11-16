"""Authenticated message channel between the host worker and the worker inside the agent VM.

Wire format: one JSON object per line (UTF-8), at most MAX_FRAME_BYTES.

Handshake (mutual, pre-shared 32-byte key, never sent):
  guest -> host  {"type": "hello", "version": 1, "nonce": Ng}
  host  -> guest {"type": "auth", "nonce": Nh, "mac": HMAC(K, "host" | Ng | Nh)}
  guest -> host  {"type": "auth_ok", "mac": HMAC(K, "guest" | Nh | Ng)}
Session key S = HMAC(K, "session" | Ng | Nh). Every later frame is
  {"seq": n, "body": {...}, "mac": HMAC(S, direction | n | canonical(body))}
with a strictly increasing per-direction sequence number, so frames cannot be forged, replayed, reordered
or reflected even over the TCP fallback transport. (Hyper-V sockets never leave the machine; the MAC still
proves the peer holds the key.) Confidentiality is not provided: nothing secret is meant to cross this
channel, and the hv transport has no network path an eavesdropper could sit on.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

PROTOCOL = 1
MAX_FRAME_BYTES = 48 * 1024 * 1024  # a screenshot or a workspace file, base64-encoded
HANDSHAKE_TIMEOUT_S = 10.0
KEY_BYTES = 32


class ChannelError(ConnectionError):
    """The peer is gone, misbehaved or failed authentication."""


class AuthError(ChannelError):
    pass


def new_key(path: Path) -> bytes:
    """Create the pre-shared key file (hex) if missing; return the key."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(KEY_BYTES), encoding="ascii")
    return load_key(path)


def load_key(path: Path) -> bytes:
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise AuthError(f"cannot read the VM channel key {path}: {exc}") from exc
    if len(key) < KEY_BYTES:
        raise AuthError(f"VM channel key {path} is shorter than {KEY_BYTES} bytes")
    return key


def _mac(key: bytes, *parts: str) -> str:
    return hmac.new(key, "|".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SecureChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, role: str) -> None:
        if role not in ("host", "guest"):
            raise ValueError(role)
        self._reader, self._writer, self.role = reader, writer, role
        self._peer = "guest" if role == "host" else "host"
        self._session = b""
        self._send_seq = 0
        self._recv_seq = 0
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ raw lines

    async def _write_raw(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise ChannelError(f"frame of {len(data)} bytes exceeds the {MAX_FRAME_BYTES} limit")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, RuntimeError) as exc:
            raise ChannelError(f"write failed: {exc}") from exc

    async def _read_raw(self) -> dict[str, Any]:
        try:
            line = await self._reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raise ChannelError("peer closed the connection") from exc
        except asyncio.LimitOverrunError as exc:
            raise ChannelError("frame too large") from exc
        except OSError as exc:
            raise ChannelError(f"read failed: {exc}") from exc
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ChannelError("malformed frame") from exc
        if not isinstance(obj, dict):
            raise ChannelError("malformed frame")
        return obj

    # ------------------------------------------------------------------ handshake

    async def handshake(self, key: bytes) -> None:
        try:
            await asyncio.wait_for(self._guest_side(key) if self.role == "guest" else self._host_side(key),
                                   HANDSHAKE_TIMEOUT_S)
        except TimeoutError as exc:
            raise AuthError("handshake timed out") from exc

    async def _guest_side(self, key: bytes) -> None:
        ng = secrets.token_hex(16)
        await self._write_raw({"type": "hello", "version": PROTOCOL, "nonce": ng})
        auth = await self._read_raw()
        nh = str(auth.get("nonce") or "")
        if auth.get("type") != "auth" or len(nh) != 32 or not hmac.compare_digest(
                str(auth.get("mac") or ""), _mac(key, "host", ng, nh)):
            await self._write_raw({"type": "auth_failed"})
            raise AuthError("host failed authentication")
        await self._write_raw({"type": "auth_ok", "mac": _mac(key, "guest", nh, ng)})
        self._session = bytes.fromhex(_mac(key, "session", ng, nh))

    async def _host_side(self, key: bytes) -> None:
        hello = await self._read_raw()
        ng = str(hello.get("nonce") or "")
        if hello.get("type") != "hello" or hello.get("version") != PROTOCOL or len(ng) != 32:
            raise AuthError(f"unexpected hello from the VM: {str(hello)[:200]}")
        nh = secrets.token_hex(16)
        await self._write_raw({"type": "auth", "nonce": nh, "mac": _mac(key, "host", ng, nh)})
        reply = await self._read_raw()
        if reply.get("type") != "auth_ok" or not hmac.compare_digest(
                str(reply.get("mac") or ""), _mac(key, "guest", nh, ng)):
            raise AuthError("the VM failed authentication (wrong key?)")
        self._session = bytes.fromhex(_mac(key, "session", ng, nh))

    # ------------------------------------------------------------------ authenticated frames

    async def send(self, body: dict[str, Any]) -> None:
        if not self._session:
            raise ChannelError("channel is not authenticated")
        async with self._write_lock:
            self._send_seq += 1
            seq = self._send_seq
            await self._write_raw({"seq": seq, "body": body,
                                   "mac": _mac(self._session, self.role, str(seq), _canonical(body))})

    async def recv(self) -> dict[str, Any]:
        if not self._session:
            raise ChannelError("channel is not authenticated")
        frame = await self._read_raw()
        body, seq = frame.get("body"), frame.get("seq")
        if not isinstance(body, dict) or not isinstance(seq, int) or seq != self._recv_seq + 1:
            raise ChannelError("frame out of sequence or malformed")
        if not hmac.compare_digest(str(frame.get("mac") or ""),
                                   _mac(self._session, self._peer, str(seq), _canonical(body))):
            raise AuthError("frame MAC mismatch")
        self._recv_seq = seq
        return body

    async def close(self) -> None:
        try:
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        except Exception:  # noqa: BLE001 - closing a dead socket
            pass
