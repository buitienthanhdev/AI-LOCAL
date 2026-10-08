from __future__ import annotations

from lga_common.config import Config
from lga_worker.knowledge.provider import KnowledgeHit, KnowledgeProvider
from lga_worker.server import CORE_NATIVE_TOOLS, build_registry


def test_knowledge_tools_bound_in_registry(config: Config) -> None:
    registry = build_registry(config.path("paths.tools_dir"))
    assert set(registry.unbound_tools()) == CORE_NATIVE_TOOLS
    for tool in ("knowledge.search", "knowledge.get_context", "knowledge.read_document", "knowledge.status"):
        assert tool in registry.available_tools()


def test_search_parses_and_limits(config: Config, monkeypatch) -> None:
    provider = KnowledgeProvider(config)
    monkeypatch.setattr(provider, "ensure_running", lambda: None)
    payload = {"hits": [
        {"score": 0.9, "file_id": "f1", "name": "TDS.png", "path": "C:/x/TDS.png", "loc": "p1",
         "text": "spec body", "why": "vector#1", "author": "a", "modified_at": "t"},
        {"score": 0.8, "file_id": "f2", "name": "B.docx", "path": "C:/B.docx", "loc": "",
         "text": "other", "why": "", "author": "", "modified_at": ""},
        {"score": 0.7, "file_id": "f3", "name": "C.pdf", "path": "C:/C.pdf", "loc": "",
         "text": "third", "why": "", "author": "", "modified_at": ""},
    ]}
    monkeypatch.setattr(provider, "_request", lambda method, path, body, timeout_s: payload)
    hits = provider.search("spec", limit=2)
    assert len(hits) == 2
    assert isinstance(hits[0], KnowledgeHit) and hits[0].file_id == "f1"
    assert hits[0].citation() == "TDS.png (p1)"
    assert hits[1].citation() == "B.docx"


def test_source_dir_resolves_under_repo_root(config: Config) -> None:
    provider = KnowledgeProvider(config)
    # default source_dir "..": Company Brain is the parent of this repo (resolved against the repo root).
    assert provider._source_dir.resolve() == config.root.parent.resolve()
