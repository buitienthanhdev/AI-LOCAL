"""In-memory attachment store backing `ChatProvider.upload()`.

Attachments are referenced by ID in chat messages and encoded per backend when a request is sent.
They live in memory only, are bounded in count and total bytes, and expire after a TTL.
"""

from __future__ import annotations

import base64
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_EXTENSION_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                   ".gif": "image/gif"}
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"))


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class Attachment:
    attachment_id: str
    mime_type: str
    data: bytes
    filename: str
    created_at: float

    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"


def sniff_mime(data: bytes, filename: str) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    mime = _EXTENSION_MIME.get(Path(filename).suffix.lower())
    if mime is None:
        raise AttachmentError(f"unsupported attachment type for {filename!r}")
    return mime


class AttachmentStore:
    def __init__(self, max_bytes_each: int, max_total_bytes: int, ttl_s: float, clock=time.monotonic) -> None:
        self._max_each = max_bytes_each
        self._max_total = max_total_bytes
        self._ttl = ttl_s
        self._clock = clock
        self._items: OrderedDict[str, Attachment] = OrderedDict()
        self._total = 0

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, a in self._items.items() if now - a.created_at > self._ttl]:
            self._total -= len(self._items.pop(key).data)

    def put(self, data: bytes, filename: str, mime_type: str | None = None) -> Attachment:
        if not data:
            raise AttachmentError("attachment is empty")
        if len(data) > self._max_each:
            raise AttachmentError(f"attachment is {len(data)} bytes; limit is {self._max_each}")
        mime = mime_type or sniff_mime(data, filename)
        if mime not in ALLOWED_MIME_TYPES:
            raise AttachmentError(f"mime type {mime} is not allowed")
        self._expire()
        while self._items and self._total + len(data) > self._max_total:
            _, evicted = self._items.popitem(last=False)
            self._total -= len(evicted.data)
        attachment = Attachment(f"att_{secrets.token_hex(8)}", mime, data, Path(filename).name or "image",
                                self._clock())
        self._items[attachment.attachment_id] = attachment
        self._total += len(data)
        return attachment

    def put_file(self, path: Path) -> Attachment:
        if not path.is_file():
            raise AttachmentError(f"{path} is not a file")
        size = path.stat().st_size
        if size > self._max_each:
            raise AttachmentError(f"{path} is {size} bytes; limit is {self._max_each}")
        return self.put(path.read_bytes(), path.name)

    def get(self, attachment_id: str) -> Attachment:
        self._expire()
        attachment = self._items.get(attachment_id)
        if attachment is None:
            raise AttachmentError(f"unknown or expired attachment {attachment_id!r}")
        return attachment
