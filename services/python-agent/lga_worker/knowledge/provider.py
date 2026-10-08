"""KnowledgeProvider: the agent's access to the Company Brain knowledge subsystem.

Company Brain (../brain, Python 3.14, Qdrant + catalog.db hybrid RAG over company documents) runs as a
separate local process. This provider talks to its existing HTTP API on 127.0.0.1:8765 and uses only its
knowledge-retrieval endpoints (`/api/search`, `/api/file/<id>`) — never `/api/ask`, which would run
Company Brain's own agent loop and local LLM. Reasoning stays with the unified agent's Gemini brain.

We call it over HTTP (not by importing its modules) on purpose: it targets a different Python runtime,
owns its Qdrant process and single-writer database, and gates the GPU for its own background learning.
The adapter reuses all of that untouched. See docs/unification.md.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
HEALTH_PATH = "/api/health"
POLL_INTERVAL_S = 1.0


@dataclass(frozen=True)
class KnowledgeHit:
    score: float
    file_id: str
    name: str
    path: str
    loc: str
    text: str
    why: str
    author: str
    modified_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "file_id": self.file_id, "name": self.name, "path": self.path,
                "loc": self.loc, "text": self.text, "why": self.why, "author": self.author,
                "modified_at": self.modified_at}

    def citation(self) -> str:
        where = f"{self.name}" + (f" ({self.loc})" if self.loc else "")
        return where


class KnowledgeProvider:
    def __init__(self, config: Config) -> None:
        section = config.section("knowledge")
        self._config = config
        self._base = str(section["base_url"]).rstrip("/")
        self._autostart = bool(section.get("autostart", True))
        self._request_timeout_s = config.seconds("knowledge.request_timeout_ms")
        self._startup_timeout_s = config.seconds("knowledge.startup_timeout_ms")
        self._search_limit = int(section.get("search_limit", 10))
        self._python = str(section.get("python", "python"))
        self._source_dir = config.path("knowledge.source_dir")
        # The knowledge backend is loopback and must never go through a proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._started = False

    def _request(self, method: str, path: str, body: dict[str, Any] | None, timeout_s: float) -> Any:
        request = urllib.request.Request(
            self._base + path, method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("error", "")
            except (json.JSONDecodeError, ValueError):
                pass
            code = ErrorCode.POLICY_DENIED if exc.code in (401, 403) else ErrorCode.BACKEND_UNAVAILABLE
            raise ToolError(code, f"knowledge backend: HTTP {exc.code} {detail}".strip()) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"knowledge backend unreachable at {self._base}: {exc}") from exc

    def is_up(self) -> bool:
        try:
            self._request("GET", HEALTH_PATH, None, 3.0)
            return True
        except ToolError:
            return False

    def ensure_running(self) -> None:
        if self.is_up():
            return
        if not self._autostart:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"Company Brain knowledge backend is not running at {self._base} "
                            "(autostart disabled)")
        if not (self._source_dir / "brain" / "__main__.py").is_file():
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"Company Brain not found at {self._source_dir} (set [knowledge] source_dir)")
        logger.info("starting Company Brain knowledge backend: %s -m brain serve (cwd=%s)",
                    self._python, self._source_dir)
        subprocess.Popen([self._python, "-m", "brain", "serve"], cwd=str(self._source_dir),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, close_fds=True)
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if self.is_up():
                self._started = True
                logger.info("Company Brain knowledge backend is up")
                return
            time.sleep(POLL_INTERVAL_S)
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                        f"Company Brain did not become reachable within {self._startup_timeout_s:.0f}s")

    def search(self, query: str, limit: int | None) -> list[KnowledgeHit]:
        self.ensure_running()
        data = self._request("POST", "/api/search", {"query": query[:500]}, self._request_timeout_s)
        hits = (data or {}).get("hits", []) if isinstance(data, dict) else []
        results = [KnowledgeHit(
            score=float(h.get("score", 0.0)), file_id=str(h.get("file_id", "")), name=str(h.get("name", "")),
            path=str(h.get("path", "")), loc=str(h.get("loc", "")), text=str(h.get("text", "")),
            why=str(h.get("why", "")), author=str(h.get("author", "")), modified_at=str(h.get("modified_at", "")),
        ) for h in hits]
        return results[: (limit or self._search_limit)]

    def read_document(self, file_id: str, max_bytes: int) -> tuple[str, str]:
        """Return (text, content_type) for a knowledge document by its file id."""
        self.ensure_running()
        request = urllib.request.Request(f"{self._base}/api/file/{urllib.parse.quote(file_id)}", method="GET")
        try:
            with self._opener.open(request, timeout=self._request_timeout_s) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                raw = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ToolError(ErrorCode.NOT_FOUND, f"no knowledge document with id {file_id!r}") from exc
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"knowledge backend: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"knowledge backend unreachable: {exc}") from exc
        truncated = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        return (text + ("\n…[truncated]" if truncated else ""), content_type)
