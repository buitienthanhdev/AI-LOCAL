# Local Gemini Agent

**One agent.** Company Brain (company RAG knowledge) and the computer-use agent are unified: a single
chat, a single orchestrator, a single Gemini brain. Company knowledge is just more tools
(`knowledge.*`) the planner can use. See [docs/unification.md](docs/unification.md).

A local Windows agent that takes a task in natural language and completes it on this PC, checking its own work at every step:

> look → understand → plan → act → check → fix → continue → complete

- **Planner:** Gemini, used through the Gemini Web UI. No Gemini API key is needed.
- **Everything that touches the machine runs locally:** windows, mouse and keyboard, UI Automation, PowerShell, files, Chrome, screen OCR/vision and AutoCAD.
- **Local tool router:** every action goes through it. The router validates each call against a schema, applies an execution policy, runs the call, and verifies the result.
- **Completion:** a task counts as complete only when the core has re-run read-only checks that prove the goal, and an independent verifier has accepted the evidence.

```
User → agent CLI / UI → Rust core (plan · route · policy · verify · recover · memory · audit)
     → Gemini gateway (gemini-web2api-go → fallback WebAI-to-API, circuit breakers)
     → Python worker → Windows-Use/UIA · PowerShell · filesystem · browser-use/Chrome · Windows OCR/vision · AutoCAD COM
```

## Quick start

```powershell
pwsh scripts/fetch-references.ps1 -Checkout
cd services/python-agent; uv sync; cd ../..
pwsh scripts/setup-gateway.ps1
cargo build --release
target/release/agent.exe doctor
target/release/agent.exe run "Mở Notepad, viết hello world và lưu vào C:\temp\hello.txt"
pwsh scripts/start-ui.ps1          # optional web UI at http://127.0.0.1:8791/
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
| [docs/environment.md](docs/environment.md) | audited machine (Phase 0) |
| [docs/repository-inventory.md](docs/repository-inventory.md) | upstream audit, pinned commits, licenses, integration mode (Phase 1) |
| [configs/config.example.toml](configs/config.example.toml) | every setting with its default |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | provenance and licenses |

## Layout

```
apps/agent-cli        Rust CLI (`agent`)
apps/agent-ui         local web UI (Python, drives the CLI)
core/rust-agent       Rust core (lga-core) + policy/
services/python-agent Python worker (Windows, shell, files, browser, vision, AutoCAD)
services/gemini-gateway  Gemini Web gateway (primary/fallback, breaker, health)
services/lga-common   shared config / secrets / redaction / logging
tools/                tool contract (the only thing Gemini sees), incl. knowledge.*
../brain             Company Brain = the knowledge subsystem (RAG), reached over HTTP
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
| TEST 6: kill primary gateway → fallback | **PASS** for failover and recovery; fallback answering **BLOCKED** until it is signed in | `services/gemini-gateway/tests/test_gateway_live.py` |
| TEST 7: kill browser → recovery | **PASS** | `test_browser_live.py` |
| TEST 8: destroy task state mid-run → resume | **PASS** (state file and backup destroyed, rebuilt from the audit log) | `run-e2e.ps1 -Test 8` |
| Acceptance 1: "Mở Chrome, tìm trang GitHub của Windows-Use, sau đó mở AutoCAD và tạo một bản vẽ mới." | **PASS**. The verifier rejected a first, too-generic completion; the planner then opened Jeomon/Windows-Use. Chrome and AutoCAD were checked externally after the agent exited. | `run-e2e.ps1 -Test A1` |
| Acceptance 2: "Mở AutoCAD và vẽ một hình tròn bán kính 100." (+ save) | **PASS**. The circle was verified by handle read-back, and the artifact path `C:\temp\circle.dwg` was reported. | `run-e2e.ps1 -Test A2` |
| Unified: "tìm tỷ lệ rắn OneCoat Pro trong tri thức rồi vẽ hình tròn bán kính đó trong AutoCAD" | **PASS** — one task: knowledge.search → 61% → autocad.create_circle r=61 → verified by handle | acceptance |
| Knowledge: "thông số OneCoat Pro?" | **PASS** — answered from TDS with citation | `knowledge.*` |
| Unit / integration suites | Rust 23 · worker 123 (+ desktop/browser/AutoCAD live) · gateway 9 (+ live) · UI 5 | see [operations.md](docs/operations.md#tests) |

`agent doctor` on this machine: every subsystem PASS except `vision` and `gemini-gateway`. Both are WARN, and both are caused by the missing Google sign-in.

### BLOCKED items

| Item | Cause | Workaround | Impact |
|---|---|---|---|
| Image input to Gemini (`vision.locate`, `vision.describe`, screenshots in planner prompts) | gemini-web2api-go runs anonymously; Gemini Web rejects image references without a signed-in account | Add a Google cookie in the primary's admin UI (`http://127.0.0.1:8083/admin`, token `LGA_PRIMARY_ADMIN_TOKEN` in `.env`) | Planning works (text). Visual grounding falls back to Windows OCR plus UI Automation; model-based grounding is unavailable. |
| Fallback backend answering (WebAI-to-API) | needs a one-time interactive Google sign-in (`verify_login.py`) | run it as described in docs/operations.md | Failover is attempted and recorded but cannot be served; the primary is auto-restarted. |
| Local UI-TARS grounding backend | 11–13 GB of the 16 GB GPU is used by other Ollama models; UI-TARS-1.5-7B needs ~7–9 GB quantised and has no official GGUF/Ollama build | free VRAM, serve UI-TARS-1.5-7B with vLLM or Ollama, set `[vision] backend = "ui_tars"` | The code path and parser are implemented and unit-tested; it was not run against a live model. |
| OCR for Vietnamese text | only the en-US Windows OCR language is installed | Settings → Time & language → add Vietnamese (with OCR) | `vision.ocr` reads English and Latin text; Vietnamese diacritics may be misread. |

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
