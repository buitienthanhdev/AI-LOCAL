"""Handlers for the knowledge.* tools (contract: tools/knowledge.json).

These expose the Company Brain knowledge subsystem as read-only tools of the one unified agent. All work
is HTTP to the knowledge backend and runs off the desktop STA thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lga_worker.knowledge.provider import KnowledgeHit, KnowledgeProvider
from lga_worker.registry import ToolContext, ToolOutcome, ToolRegistry

BACKEND = "company-brain"
CONTEXT_PASSAGE_CHARS = 700
OBSERVATION_HITS = 8


async def _provider(ctx: ToolContext) -> KnowledgeProvider:
    async def create() -> KnowledgeProvider:
        return KnowledgeProvider(ctx.config)

    return await ctx.services.get_or_create("knowledge", create)


def _hit_line(hit: KnowledgeHit) -> str:
    snippet = " ".join(hit.text.split())[:200]
    return f"[{hit.citation()}] {snippet}" + (f"  (why: {hit.why})" if hit.why else "")


def register(registry: ToolRegistry) -> None:
    @registry.handler("knowledge.status")
    async def status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        up = await asyncio.to_thread(provider.is_up)
        return ToolOutcome(data={"ready": up, "base_url": provider._base}, backend=BACKEND,
                           observation=f"knowledge backend {'reachable' if up else 'not running'} at {provider._base}")

    @registry.handler("knowledge.search")
    async def search(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        hits = await asyncio.to_thread(provider.search, args["query"], args.get("limit"))
        listing = "\n".join(_hit_line(h) for h in hits[:OBSERVATION_HITS])
        more = len(hits) - min(len(hits), OBSERVATION_HITS)
        summary = (f"{len(hits)} knowledge hit(s) for {args['query']!r}:\n{listing}"
                   + (f"\n… {more} more in data" if more > 0 else "")) if hits \
            else f"No company-knowledge match for {args['query']!r}."
        return ToolOutcome(data={"hits": [h.to_dict() for h in hits]}, observation=summary, backend=BACKEND)

    @registry.handler("knowledge.get_context")
    async def get_context(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        hits = await asyncio.to_thread(provider.search, args["query"], args.get("limit", 6))
        blocks, citations = [], []
        for i, hit in enumerate(hits, start=1):
            passage = " ".join(hit.text.split())[:CONTEXT_PASSAGE_CHARS]
            blocks.append(f"[{i}] {hit.citation()}\n{passage}")
            citations.append({"file_id": hit.file_id, "name": hit.name, "loc": hit.loc})
        context = "\n\n".join(blocks)
        observation = (f"Evidence from company knowledge ({len(hits)} passage(s)):\n{context}" if hits
                       else f"No company-knowledge evidence for {args['query']!r}.")
        return ToolOutcome(data={"context": context, "found": bool(hits), "citations": citations},
                           observation=observation, backend=BACKEND)

    @registry.handler("knowledge.read_document")
    async def read_document(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
        provider = await _provider(ctx)
        max_bytes = int(args.get("max_bytes") or ctx.config.get("filesystem.max_read_bytes"))
        content, content_type = await asyncio.to_thread(provider.read_document, args["file_id"], max_bytes)
        return ToolOutcome(
            data={"file_id": args["file_id"], "content": content, "content_type": content_type},
            observation=f"Read knowledge document {args['file_id']} ({content_type}, {len(content)} chars).",
            backend=BACKEND)
