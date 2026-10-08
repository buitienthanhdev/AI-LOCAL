# Architecture

## Goal

A local Windows agent that takes a natural-language task and finishes it on this PC. It works in a loop: look, understand, plan, act, check, fix, continue, complete. Gemini (through the Gemini Web UI, without an API key) is only the planner. Everything that touches the machine runs locally, behind a tool router that validates, authorises and verifies every action.

```
User ──► agent CLI (Rust) ──► Task runner (Rust core) ──► ChatProvider ──► Gemini gateway ──► Gemini Web
                                   │  ▲                                         (primary / fallback)
                       tool call   │  │ result + verification
                                   ▼  │
                     Tool router: schema check → policy → confirm → route
                                   │
                        JSON lines over stdio (IPC)
                                   ▼
                     Python worker ── Windows (Windows-Use + UIA) · Shell · Filesystem
                                   ├─ Browser (browser-use over CDP, dedicated Chrome profile)
                                   ├─ Vision (Windows OCR, Gemini/UI-TARS grounding)
                                   └─ AutoCAD (COM on an STA thread)
```

## Components

| Path | Language | Responsibility |
|---|---|---|
| `apps/agent-cli` | Rust | The `agent` command: run, resume, status, cancel, history, doctor, models, tools, screenshot, gateway. Interactive confirmation prompts, Ctrl+C cancellation. |
| `core/rust-agent` (`lga-core`) | Rust | The orchestrator: task lifecycle and state machine, planner loop, tool routing, timeouts, cancellation, retry policy, observation/verification state, policy engine, audit events, memory index, IPC with the worker, the `ChatProvider` trait. |
| `core/rust-agent/policy/` | TOML | Execution policy per mode (normal / trusted / full_access). |
| `services/python-agent` (`lga_worker`) | Python 3.12 | Execution and AI-adapter layer: implements every tool in `tools/*.json` on top of Windows-Use, browser-use, Windows OCR, AutoCAD COM. |
| `apps/agent-desktop` | Rust | The chat window (tao + wry over WebView2): starts tasks through the CLI, shows progress, confirmations, questions and results; starts the gateway and the knowledge backend when they are down. |
| `services/gemini-gateway` (`lga-gateway`) | Rust | Local HTTP service (axum) implementing `ChatProvider` over Gemini Web: primary/fallback routing, circuit breakers, retries, health monitor, backend process supervision in a kill-on-close Job Object. |
| `services/lga-common` | Python | Shared config loading, secret resolution, redaction, JSON logging. |
| `tools/` | JSON | The unified tool contract: the only interface Gemini sees ([tool-contract.md](tool-contract.md)). |
| `configs/` | TOML | `config.example.toml` holds every default; `config.toml` holds local overrides (git-ignored). |
| `references/` | – | Pinned upstream clones (read-only), see [repository-inventory.md](repository-inventory.md). |
| `memory/`, `logs/`, `artifacts/`, `runtime/` | – | Task state and persistent memory, logs, screenshots/exports, local runtime (built binaries, venvs, dedicated Chrome profile). All git-ignored. |

Why this split: the computer-use libraries that already work well are Python (Windows-Use, browser-use, pywin32 COM, PyWinRT OCR), so they are reused rather than rewritten. Rust owns everything else — the parts that must be deterministic and robust (state, policy, retries, cancellation, audit), the Gemini gateway and the chat window.

## The execution loop

`core/rust-agent/src/agent.rs`:

```
PLAN     build the prompt: goal, plan, recent steps, latest observation, recalled memory, notices
         → ChatProvider.chat → parse exactly one JSON action (bounded re-asks if malformed)
TOOL     unknown tool / invalid args / repeated identical call → rejected without executing
POLICY   deny → POLICY_DENIED;  confirm → ask the user (CLI) or decline when non-interactive
EXECUTE  worker call with deadline; retry only idempotent calls on retryable errors (backoff);
         dead/hung worker → restart; non-idempotent calls are never blindly repeated
OBSERVE  if the result is unverified ("deferred") or failed: automatic UI snapshot
         (desktop.inspect_ui or browser.get_state)
VERIFY   self-verified tools must pass their own post-condition; failed checks = failed step
CONTINUE/RECOVER  next planning turn carries the result, the error's recovery hint, the observation
COMPLETE task.complete: the core re-runs the planner's read-only checks and accepts completion
         only if all pass; tasks that changed state cannot complete on "evidence" alone
```

States are `pending → planning → executing → observing/recovering → planning → … → verifying → completed`, plus `needs_user`, `failed` and `cancelled`. Transitions are validated (`state.rs`), and completion is only reachable through `verifying`.

Exit code 0 never completes a task. Every important side effect is checked by reading it back:
- files by size and SHA-256;
- windows by their appearance or disappearance;
- typed text by UI Automation value read-back;
- AutoCAD entities by handle read-back;
- web pages by URL and title.

## Delegation model

Gemini says *what*: `{"tool": "autocad.create_circle", "args": {...}}`. The local router decides *how*:
- which worker implementation runs it;
- with which timeout and retry policy;
- whether it is allowed at all.

Gemini never receives shell access, mouse control or raw Python functions. It only sees the tool contract.

## Perception order

1. Accessibility tree: `desktop.inspect_ui`.
2. Semantic lookup: `desktop.find_element`, `desktop.read_element`.
3. OCR: `vision.ocr`, using Windows' built-in engine.
4. Screenshot plus vision model: `vision.locate` / `vision.describe`. UI-TARS or Gemini act only as a grounding subsystem.
5. Coordinate action: `desktop.click {x, y}`.

A located point is always cross-checked with UI Automation (`ControlFromPoint`) before the planner uses it.

## Gemini gateway

```
GeminiGateway (ChatProvider over HTTP on 127.0.0.1:8790, API-key protected)
├── PrimaryBackend   gemini-web2api-go (Go binary built from the pinned source)
├── FallbackBackend  WebAI-to-API (own Python 3.12 venv)
└── HealthMonitor    periodic probes, restart of dead managed backends
```

Each request goes through the primary, then the fallback:
- **Per-backend circuit breaker:** consecutive failures open it, a half-open probe tests recovery, and the open duration doubles up to a cap.
- **Bounded exponential backoff:** used for rate-limit and transient errors.
- **Shared deadline:** one deadline covers every attempt of a request.
- **Error routing:** client errors (prompt too long) are returned without failover. Capability errors (no account for images) fail over without penalising the breaker.

The core only knows the `ChatProvider` trait (`llm.rs`).

## Model routing (current and future)

| Provider | Status |
|---|---|
| Gemini Web via gemini-web2api-go | implemented (primary) |
| Gemini Web via WebAI-to-API | implemented (fallback) |
| Gemini API, Claude API, OpenAI API, Ollama, vLLM, other local models | not implemented |

Adding a provider means implementing `ChatProvider` (Rust trait in `llm.rs`; the gateway's Python `ChatProvider` mirrors it) and selecting it with `[llm] provider`. The planner prompt and parser are provider-neutral: plain JSON, no vendor function-calling.

## IPC

Rust core ↔ Python worker: one JSON object per line over the worker's stdio.
- Requests carry `request_id, task_id, step_id, tool, args, deadline_unix_ms`.
- Responses carry `request_id, status, result, error, metrics`.
- The core and the worker share no mutable state.
- The worker runs inside a Job Object, so it dies with the core.
- A missing reply after deadline plus grace marks the worker as hung. It is killed and restarted.

## Memory

| Tier | Where | Content |
|---|---|---|
| Short-term | planner prompt | last `history_steps` steps verbatim, older ones summarised; last `history_images` screenshots |
| Task memory | `memory/tasks/<id>.json` (+ `.bak`), rebuildable from `logs/tasks/<id>.jsonl` | goal, plan, every step, status, artifacts, escalation requests |
| Persistent | `memory/persistent.db` (SQLite) | task history, successful procedures, failure patterns with the recovery that worked, known application state |

Everything is redacted before it is stored. Relevant memories are recalled into the prompt by keyword overlap with the goal.

## Threading model (worker)

- All UI Automation work runs on one STA thread (`StaExecutor("desktop")`). AutoCAD COM runs on its own STA thread.
- Shell, filesystem and HTTP work runs on worker threads.
- Browser work runs on the asyncio loop (CDP).
- A COM call that exceeds its timeout abandons that STA thread; a fresh one takes over.

## Process model

| Process | Lifetime |
|---|---|
| `agent.exe` | per command |
| Python worker | child of `agent.exe`, inside a kill-on-close Job Object |
| Gemini gateway | detached, shared across runs; started on demand; `agent gateway stop` |
| gemini-web2api-go, WebAI-to-API | children of the gateway (kill-on-close job) |
| Chrome (automation profile) | started by the browser subsystem; relaunched after crashes |
| AutoCAD | started by `autocad.launch`, left running for the user |

Nothing in the Windows control path is containerised.
