# Unification: one agent (Local Gemini Agent ⊕ Company Brain)

Goal: the user talks to **one** AI through **one** chat. Underneath there is one orchestrator, one task
state, one tool bus, one memory, one policy, one verification loop, one Gemini brain. Company Brain stops
being a second agent and becomes the **knowledge subsystem**.

## What each system is today (audit, Phase A–D)

**Local Gemini Agent** (this repo) — already the shape we want:
one Rust orchestrator (task lifecycle, planner loop, tool routing, policy, verification, audit, memory
index), one tool contract (`tools/*.json`, ~60 tools), one Python worker hosting the providers
(Windows/UIA, shell, filesystem, browser/CDP, vision/OCR+UI-TARS, AutoCAD/COM), one Gemini Web gateway
(primary gemini-web2api-go + fallback WebAI-to-API, circuit breaker), one UI (popup `● Agent` + web).
Brain = Gemini Web. Verified, `agent doctor` mostly PASS.

**Company Brain** (`../brain`, Python 3.14, Ollama) — two halves:
- **Valuable, unique: knowledge/RAG.** Qdrant `chunks` (COSINE, ~1024-d, 1.1 GB) + SQLite `catalog.db`
  (~960 MB: 878k files, 140k chunks, 51k relations, 26k entities, facts, people, versions), hybrid
  vector+BM25 (RRF) retrieval with ACL, FTS5, knowledge graph, version history, Vietnamese text
  normalization (`vn.py` + Rust `brain_core`). Served by `python -m brain serve` on `127.0.0.1:8765`.
- **Duplicate: its own computer-use agent.** `agent.py` (multi-step tool loop + self-verify), `computer.py`,
  `gui.py` (UIA), `apps.py`, `llm.py` (Ollama), `actions.py`, `skills.py`, `memory.py`, `convo.py`. These
  map one-to-one onto Local Gemini Agent's orchestrator / tool bus / Windows provider / gateway.

## Duplication map → decision

| Capability | Company Brain | Local Gemini Agent | Decision |
|---|---|---|---|
| Orchestration loop + planner | `agent.py` `_ask()` | Rust core `agent.rs` | **keep LGA**, retire brain's |
| Tool registry | `agent.TOOLS` + merged admin tools | `tools/*.json` + registry | **keep LGA**, add `knowledge.*` |
| LLM brain | Ollama `gemma4:12b` (`llm.py`) | Gemini Web gateway | **keep LGA** (Gemini); Ollama stays only for embeddings |
| Verification | `agent.verify()` | core verify + completion verifier | **keep LGA** |
| Windows/computer use | `computer.py`,`gui.py`,`apps.py` | Windows/desktop provider | **keep LGA**, retire brain's |
| Memory (conversation) | `convo.py`, session-less | task state + SQLite memory | **keep LGA** |
| Knowledge / RAG | `index.py`,`sqlbox.py`,`graph.py`,`extract.py`,`scanner.py`,`facts.py`,`vn.py`,Qdrant,`catalog.db` | — (none) | **keep Company Brain** → KnowledgeProvider |
| Experience memory (lessons) | `memory.py` (vector recall) | — | optional future MemoryProvider |
| UI | pywebview panel + Tkinter widget on `:8765` | popup `● Agent` + web | **keep LGA** single UI; brain UI deprecated |

## Target architecture

```
User ─ one chat (● Agent popup / web) ─► Rust UnifiedAgent (plan·route·policy·verify·recover·audit)
                                            │ Gemini Web gateway = brain (plans, picks tools, judges)
                                            │ one tool bus (tools/*.json)
          ┌───────────────┬────────────────┼───────────────┬───────────────┬───────────────┐
      knowledge.*      desktop.*         browser.*       vision.*        autocad.*       shell./fs.
          │            (Windows-Use)       (CDP)        (OCR/UI-TARS)      (COM)
   KnowledgeProvider  ── HTTP 127.0.0.1:8765 ──►  Company Brain `serve` (knowledge only)
   (worker)                                        Qdrant + catalog.db + hybrid retrieval + ACL
```

The planner (Gemini) sees `knowledge.*` alongside the computer tools and chooses them itself — no separate
intent router, no second loop. A hybrid request ("tìm M500 rồi vẽ trong AutoCAD") is one task: `knowledge.search`
→ reason → `autocad.*` → verify.

## Integration decision: HTTP adapter, not in-process import

The KnowledgeProvider calls Company Brain's existing `serve` over `http://127.0.0.1:8765`, rather than
importing its Python modules. Reasons (audit risks): Company Brain runs on Python **3.14**, the worker on
**3.12**; its Rust `brain_core` is built per-interpreter and stored keys depend on byte-identical output;
`catalog.db` is single-writer WAL and Qdrant is a process it owns; its LLM GPU-gating assumes it owns
Ollama. Calling it over HTTP reuses all of that untouched — matching "reuse → adapter → merge, don't
rewrite what works, keep a backend compatibility layer." We use its **knowledge primitives only**
(`/api/search`, `/api/file/<id>`), never `/api/ask` (that would run brain's own agent loop/LLM).

`/api/search` (POST, localhost auto-admin → sees all): `{query}` → `{hits:[{score,file_id,name,path,loc,
author,modified_at,text,why}]}`. `/api/file/<id>` → document bytes/text.

## Tools added (one registry)

- `knowledge.search {query, limit?}` → ranked hits with citations (file, loc, why).
- `knowledge.read_document {file_id|path, max_bytes?}` → document text.
- `knowledge.get_context {query}` → composite: top hits formatted as evidence for the planner.
- (later) `knowledge.sql`, `knowledge.graph`, `knowledge.list_sources` if the brain `serve` exposes them.

Policy: new category `knowledge` = read-only, `allow` in every mode (no secrets, no side effects).

## Migration phases (status)

- **A–D audit / maps / duplicates** — done (this document).
- **E design UnifiedAgent** — done (above). LGA core is the unified orchestrator; no new agent, loop, UI,
  planner, memory or registry is created.
- **F/G state+memory / tool registry** — nothing to merge structurally: LGA already has one of each;
  knowledge is added as tools, not a second system.
- **H Company Brain → KnowledgeProvider** — implement the HTTP adapter + `knowledge.*` tools (first code
  phase).
- **I Gemini gateway** — unchanged (already the brain).
- **J Windows/Browser/Vision/AutoCAD** — unchanged (already under the one agent).
- **K single UI** — the `● Agent` popup/web is the one chat; Company Brain's pywebview/Tkinter UI is
  deprecated (kept runnable as a knowledge admin panel only; not a chatbot).
- **L migration / back-compat** — Company Brain `serve` keeps running as the knowledge backend (its
  `/api/*` stay for its own admin tabs); its chat popup is removed from startup. `Company Brain.bat`
  (widget) is superseded by the unified startup.
- **M/N/O tests + doctor + acceptance** — run after H.

## Won't break (Phase 18 rule)

Adding `knowledge.*` is additive: new provider, new tool file, new policy category, new config section.
It does not touch the Windows/browser/vision/AutoCAD/shell/filesystem providers or the Rust core's
existing paths. `agent doctor` is run after each phase; the only expected non-PASS stays the Gemini
`WARN` until a Google account is added.
