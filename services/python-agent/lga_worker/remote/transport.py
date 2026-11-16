"""Transports for the VM channel.

  hv:<vm-guid>:<service-guid>   Hyper-V socket (AF_HYPERV). Needs no network adapter in the VM: the agent VM
                                has none, so it cannot reach the host's LAN or the internet. The guest listens
                                on the wildcard VM id; the host connects to the VM's id. The service GUID must
                                be registered once on the host (scripts/vm/register-hvsocket.ps1, admin).
  tcp:<host>:<port>             Loopback/test transport (integration tests run a real guest worker on it).

The connected socket is handed to asyncio streams; the blocking accept/connect run in a thread because the
Proactor loop has no native AF_HYPERV connect.
"""
from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass

STREAM_LIMIT = 64 * 1024 * 1024
HV_GUID_WILDCARD = "00000000-0000-0000-0000-000000000000"
CONNECT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Endpoint:
    kind: str  # "hv" | "tcp"
    a: str     # vm guid | host
    b: str     # service guid | port

    @classmethod
    def parse(cls, text: str) -> Endpoint:
        kind, _, rest = text.partition(":")
        a, _, b = rest.rpartition(":")
        if kind == "tcp" and a and b.isdigit():
            return cls(kind, a, b)
        if kind == "hv":
            uuid.UUID(a)
            uuid.UUID(b)
            return cls(kind, a, b)
        raise ValueError(f"bad VM endpoint {text!r} (hv:<vm-guid>:<service-guid> or tcp:<host>:<port>)")

    def __str__(self) -> str:
        return f"{self.kind}:{self.a}:{self.b}"


def _hv_socket() -> socket.socket:
    if not hasattr(socket, "AF_HYPERV"):
        raise OSError("this Python has no AF_HYPERV support (needs CPython 3.12+ on Windows)")
    return socket.socket(socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW)


async def connect(endpoint: Endpoint) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if endpoint.kind == "tcp":
        return await asyncio.wait_for(
            asyncio.open_connection(endpoint.a, int(endpoint.b), limit=STREAM_LIMIT), CONNECT_TIMEOUT_S)

    def blocking() -> socket.socket:
        sock = _hv_socket()
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect((endpoint.a, endpoint.b))
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        return sock

    sock = await asyncio.to_thread(blocking)
    return await asyncio.open_connection(sock=sock, limit=STREAM_LIMIT)


class Listener:
    """Accepts one connection at a time (the guest serves a single host)."""

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        if endpoint.kind == "tcp":
            self._sock = socket.create_server((endpoint.a, int(endpoint.b)))
        else:
            self._sock = _hv_socket()
            self._sock.bind((HV_GUID_WILDCARD, endpoint.b))
            self._sock.listen(1)

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1] if self.endpoint.kind == "tcp" else 0

    async def accept(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        conn, _ = await asyncio.to_thread(self._sock.accept)
        conn.setblocking(True)
        return await asyncio.open_connection(sock=conn, limit=STREAM_LIMIT)

    def close(self) -> None:
        self._sock.close()
