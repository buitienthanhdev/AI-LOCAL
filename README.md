# Local Gemini Agent

**One agent, one AI.** Company Brain (company RAG knowledge) lives in this repository
(`services/company-brain`) and is the agent's knowledge subsystem: a single chat, a single orchestrator, and
one model for every job: gemma-4-12B (Q8) served by llama.cpp (LlamaApp router), used both by the planner and
by Company Brain's answering, extraction and media reading. Gemini API / Gemini Web / Ollama are only
fallbacks for the planner. Company knowledge is just more tools (`knowledge.*`), searched natively by the Rust
core (hybrid Qdrant vectors + SQLite FTS5, reciprocal rank fusion) with no Python process at query time; the
index is refreshed on demand with `Cập nhật tri thức.bat`. Start/stop everything with
`Chạy Trợ lý.bat` / `Tắt Trợ lý.bat` next to this folder. See [docs/unification.md](docs/unification.md).

A local Windows agent that takes a task in natural language and completes it on this PC, checking its own work at every step:

> look → understand → plan → act → check → fix → continue → complete

- **Planner:** Gemini, used through the Gemini Web UI. No Gemini API key is needed.
- **Everything that touches the machine runs locally:** windows, mouse and keyboard, UI Automation, PowerShell, files, Chrome, screen OCR/vision and AutoCAD.
- **Local tool router:** every action goes through it. The router validates each call against a schema, applies an execution policy, runs the call, and verifies the result.
- **Completion:** a task counts as complete only when the core has re-run read-only checks that prove the goal, and an independent verifier has accepted the evidence.

```
User → chat window (Rust) / agent CLI → Rust core (plan · route · policy · verify · recover · memory · audit)
     → Gemini gateway, Rust (gemini-web2api-go → fallback WebAI-to-API, circuit breakers)
     → Rust native tools: PowerShell · filesystem · system environment
     → Python worker → Windows-Use/UIA · browser-use/Chrome · Windows OCR/vision · AutoCAD COM · knowledge
```

## Quick start

```powershell
pwsh scripts/fetch-references.ps1 -Checkout
cd services/python-agent; uv sync; cd ../..
pwsh scripts/setup-gateway.ps1
cargo build --release
target/release/agent.exe doctor
target/release/agent.exe run "Mở Notepad, viết hello world và lưu vào C:\temp\hello.txt"
target/release/agent-desktop.exe   # the chat window (or pwsh scripts/start.ps1)
```

Details: [docs/operations.md](docs/operations.md). Connecting a Google account (needed for image input): [docs/operations.md#connecting-gemini-accounts](docs/operations.md#connecting-gemini-accounts).

## Documentation

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | components, execution loop, delegation model, perception order, gateway, IPC, memory, model routing |
| [docs/tool-contract.md](docs/tool-contract.md) | tool schema fields, result envelope, error codes, IPC framing |
| [docs/recovery.md](docs/recovery.md) | every failure mode and its bounded response |
| [docs/security.md](docs/security.md) | trust boundaries, modes, UAC, secrets, redaction, network exposure |
| [docs/operations.md](docs/operations.md) | setup, daily use, files and logs, tests, troubleshooting |
| [docs/desktop-control.md](docs/desktop-control.md) | working-window discipline, observation after every action, verified save/close/type, measured app behaviour |
| [docs/paint-engine.md](docs/paint-engine.md) | drawing in Microsoft Paint: planned geometry, pixel verification, repair |
| [docs/art-engine.md](docs/art-engine.md) | one drawing brain for every app that can draw: photo / SVG / template -> Paint, AutoCAD, PowerPoint, Word, Excel, SVG+PNG |
| [docs/cad.md](docs/cad.md) | 3D parts from text: cad.build / cad.inspect (text-to-cad `cadgen`, build123d on Open CASCADE) -> STEP / STL / GLB / 3MF, measured and previewed |
| [docs/attachments.md](docs/attachments.md) | files attached in the chat window: storage, planner view, limits |
| [docs/environment.md](docs/environment.md) | audited machine (Phase 0) |
| [docs/repository-inventory.md](docs/repository-inventory.md) | upstream audit, pinned commits, licenses, integration mode (Phase 1) |
| [configs/config.example.toml](configs/config.example.toml) | every setting with its default |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | provenance and licenses |

## Layout

```
apps/agent-cli        Rust CLI (`agent`)
apps/agent-desktop    desktop chat window (Rust, native WebView2, drives the CLI)
core/rust-agent       Rust core (lga-core) + policy/ + native shell/filesystem/environment tools
services/python-agent Python worker (Windows UI Automation, browser, vision, AutoCAD, knowledge)
services/gemini-gateway  Gemini Web gateway, Rust (primary/fallback, breaker, health)
services/lga-common   shared config / secrets / redaction / logging
tools/                tool contract (the only thing Gemini sees), incl. knowledge.*
services/company-brain   Company Brain: builds the knowledge index (Python 3.14, run on demand by "Cập nhật tri thức.bat"); data/ git-ignored
configs/ scripts/ tests/e2e/ docs/ references/ (pinned upstreams, git-ignored)
memory/ logs/ artifacts/ runtime/   (local state, git-ignored)
```

## Status (2026-10-08)

The tests below ran on the audited machine: Windows 11 Pro 26200, AutoCAD 2023, Chrome 152, with Gemini Web in anonymous mode.

| Test | Result | Where |
|---|---|---|
| TEST 1: Notepad: open, type, save, verify file | **PASS** (agent run, GUI path, file content checked externally) | `tests/e2e/run-e2e.ps1 -Test 1` |
| TEST 2: Chrome: open, navigate, read, click, verify | **PASS** | `services/python-agent/tests/test_browser_live.py` |
| TEST 3: screenshot → locate → click → verify | **PASS** with Windows OCR grounding; Gemini/UI-TARS grounding **BLOCKED** (see below) | `test_vision_live.py` |
| TEST 4: PowerShell harmless command, read output | **PASS** (agent run) | `run-e2e.ps1 -Test 4` |
| TEST 5: AutoCAD: launch, new drawing, geometry, save/DXF/PDF, verify | **PASS** (cold start and attach) | `test_autocad_live.py` |
| TEST 6: kill primary gateway → fallback | **PASS** for failover and recovery; fallback answering **BLOCKED** until it is signed in | `services/gemini-gateway/tests/live.rs` |
| TEST 7: kill browser → recovery | **PASS** | `test_browser_live.py` |
| TEST 8: destroy task state mid-run → resume | **PASS** (state file and backup destroyed, rebuilt from the audit log) | `run-e2e.ps1 -Test 8` |
| Acceptance 1: "Mở Chrome, tìm trang GitHub của Windows-Use, sau đó mở AutoCAD và tạo một bản vẽ mới." | **PASS**. The verifier rejected a first, too-generic completion; the planner then opened Jeomon/Windows-Use. Chrome and AutoCAD were checked externally after the agent exited. | `run-e2e.ps1 -Test A1` |
| Acceptance 2: "Mở AutoCAD và vẽ một hình tròn bán kính 100." (+ save) | **PASS**. The circle was verified by handle read-back, and the artifact path `C:\temp\circle.dwg` was reported. | `run-e2e.ps1 -Test A2` |
| Unified: "tìm tỷ lệ rắn OneCoat Pro trong tri thức rồi vẽ hình tròn bán kính đó trong AutoCAD" | **PASS** — one task: knowledge.search → 61% → autocad.create_circle r=61 → verified by handle | acceptance |
| Knowledge: "thông số OneCoat Pro?" | **PASS** — answered from TDS with citation | `knowledge.*` |
| Unit / integration suites | Rust 53 (core + native tools + gateway + desktop app; +2 live) · worker 123 (+ desktop/browser/AutoCAD live) | see [operations.md](docs/operations.md#tests) |

### Brains (who plans)

The gateway tries, in order, whatever is available:

| Brain | Needs | Notes |
|---|---|---|
| **Gemini API** (recommended) | a free key from aistudio.google.com/apikey, pasted in the chat window: ⚙ Cài đặt bộ não AI | Fastest and most reliable; the newest Flash model is picked automatically. |
| Gemini Web (gemini-web2api-go) | a Google cookie in its admin UI (`http://127.0.0.1:8083/admin`) | Free, but Google temporarily blocks an account that sends many requests ("unusual traffic" page); the gateway then skips it. |
| Gemini Web fallback (WebAI-to-API) | one-time `verify_login.py` sign-in | Optional. |
| Local model (Ollama `gemma4:12b`) | Ollama installed; started on demand | Works offline; ~3–6 s per step when the GPU is free, minutes when another app fills the GPU. |

### Known limits

| Item | Impact |
|---|---|
| Google blocking Gemini Web | Without an API key the agent falls back to the local model (slower, less capable planner). |
| GPU shared with other apps | The local model and UI-TARS slow down sharply when VRAM is taken by games or other LLM apps. |
| Windows OCR for Vietnamese | Only en-US Windows OCR is installed; `vision.ocr {"vietnamese": true}` uses the local EasyOCR model instead. Real app text is read through UI Automation, which handles Vietnamese fully. |

## Principles kept

- **Reuse:**
  - **Windows-Use:** used through an adapter with trimmed dependencies; not forked.
  - **browser-use:** event-bus primitives only.
  - **Gemini backends:** run unmodified as separate processes.
  - **UI-TARS parser and AutoCAD-MCP COM patterns:** adapted with attribution.
  - **Provenance:** every upstream is pinned to a commit.
- **Gemini never executes anything:** it only sees the tool contract, and the local router decides the implementation.
- **No fake success:** exit codes don't complete tasks, completion checks are re-run by the core, and an independent verifier must agree.
- **No secrets in git or logs:** see `.gitignore`, the `*_secret` config names, and redaction everywhere. No cookie scraping: WebAI-to-API's browser-cookie extraction is disabled.
- **Never elevates:** UAC-requiring actions return a structured escalation request.
