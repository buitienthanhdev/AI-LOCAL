# Current state (audit of 2026-10-14)

Labels: **FACT** = read in code or measured in this audit, **INFERENCE** = concluded from facts, **UNKNOWN** = not
established. Paths are relative to the repository root.

## 1. What the system is

A Windows 11 desktop agent. A Rust core plans with an LLM (the "planner"), and every action goes through a tool router
with schema validation, policy, execution, verification and audit. The LLM never executes anything itself.

```
chat window (apps/agent-desktop, WebView2)  /  CLI (apps/agent-cli)
        │ spawns/uses
        ▼
Rust core  core/rust-agent  (lga-core)
  agent.rs     TaskRunner: the execution loop, completion verification
  planner.rs   system prompt, per-turn prompt, reply parsing/repair
  state.rs     TaskStatus machine, TaskRecord, atomic persistence + audit-log rebuild
  policy.rs    execution modes (normal | trusted | full_access), path/shell/URL verdicts
  tools.rs     tool catalog (tools/*.json), JSON-schema validation, prompt rendering
  worker.rs    JSON-lines IPC client of the Python worker; restarts a dead/hung worker
  memory.rs    SQLite persistent memory      skills.rs  playbooks (skills/*.md)
  llm.rs       ChatProvider trait, GatewayProvider (HTTP to the gateway)
  native/      Rust tools: shell, filesystem, environment, web, knowledge (Qdrant + FTS5)
        │ HTTP loopback                           │ stdin/stdout JSON lines
        ▼                                         ▼
Gateway  services/gemini-gateway          Python worker  services/python-agent/lga_worker
  routes (brains), breakers, health,        UI Automation (Windows-Use), browser-use/CDP,
  retry, managed processes                  OCR/vision, AutoCAD COM, Office COM, paint/art/cad
        │
        ├─ api (Gemini API), primary/fallback (Gemini Web servers),
        │  cloud (Ollama cloud), local (Ollama), chrome_gemini (Chrome adapter, OFF)
        ▼
services/chrome-gemini  Python adapter -> CDP -> Chrome profile runtime/gemini-profile
```

## 2. Entry points and processes (FACT)

| Entry point | Role |
|---|---|
| `agent.exe run/resume/doctor/...` (`apps/agent-cli/src/main.rs`) | task runner, one process per task |
| `agent-desktop.exe` (`apps/agent-desktop`) | chat window + local HTTP API (`api.rs`), starts the CLI per task, activity tracker |
| `lga-gateway.exe` (`services/gemini-gateway`) | the only component that talks to model servers |
| `lga_worker` (Python, venv `services/python-agent/.venv`) | all machine-touching tools except the native Rust ones |
| `services/company-brain` (Python 3.14) | knowledge indexing, run on demand (`Cập nhật tri thức.bat`) |
| `services/chrome-gemini` | Gemini Web adapter; started by the gateway only when `gateway.chrome_gemini.enabled` |

Production routing is `brain_order = ["api"]` (`configs/config.toml`); `chrome_gemini.enabled = false`.

## 3. The execution loop (FACT: `agent.rs`, `state.rs`)

`TaskStatus`: `pending → planning → executing → observing → verifying → completed`, plus `recovering`, `needs_user`,
`paused`, `failed`, `cancelled`. Transitions are checked (`TaskStatus::can_transition`): completion is only reachable
from `verifying`.

Per iteration: cancel/pause/budget checks (`max_steps`, `task_timeout_ms`, `max_consecutive_failures`) →
`plan_next` (builds the prompt within `max_context_tokens`, calls the planner, repairs/parses JSON) → each action:
`validate_args` → `Policy::evaluate` (Allow / Confirm / Deny) → worker or native execution → observation → step record.
Retries happen only for `idempotent` tools on retryable errors (`agent.rs` ~868–906); a non-idempotent call that may
have run is reported, not repeated.

Completion (`verify_completion`): the planner supplies read-only `checks` with expected values; the core **re-runs them
itself**, and for UI work asks an independent LLM verifier. A rejected completion goes back to planning. After
`MAX_COMPLETION_REJECTIONS = 2` the task is accepted anyway with a warning (see gap G2; now recorded in
`completion_basis`).

Persistence: `TaskRecord` JSON is written atomically after each transition, `.bak` kept, and rebuilt from
`logs/tasks/<id>.jsonl` if both are lost. `agent resume` continues a task.

## 4. Tools (FACT: `tools/*.json`, counted)

107 tools in 15 groups: desktop 20, autocad 18, browser 17, office 9, system 8, knowledge 6, assistant 6, activity 5,
vision 4, art 3, paint 3, cad 2, documents 2, memory 2, web 2. Each declares `input_schema`, `output_schema`,
`timeout_ms`, `risk_level`, `requires_confirmation`, `idempotent`, `supports_rollback`, `verification`,
`policy_category`, optional `side_effects`.

| Property | Count |
|---|---|
| risk low / medium / high | 70 / 31 / 6 |
| idempotent | 71 |
| `requires_confirmation: true` | 1 (the rest is decided by policy by category/risk/mode) |
| `side_effects` declared explicitly | 39 of 107 (none 21, possible 9, yes 8, process 1); 68 derive from `verification` |

Groups other than the base ones load on demand from request words (`GROUP_TRIGGERS`) or `tools.enable`.

## 5. Policy and trust (FACT: `policy.rs`, `docs/security.md`)

Modes `normal`, `trusted`, `full_access` (current config: `full_access`). Policy denies elevation in every mode,
private keys/credential stores, forbidden shell patterns, blocked URL schemes/domains, relative paths for path
arguments; asks confirmation for protected locations and risky shells. Secrets are redacted before audit/memory
(`redact.rs`). **Until this audit the planner prompt did not state that tool output is untrusted data** (docs said so;
the prompt did not): see G1.

## 6. Models and providers (FACT: `llm.rs`, gateway)

`ChatProvider` is the only LLM interface of the core (`chat`, `stream`, `upload`, `health`, `list_models`). The gateway
keeps an ordered list of routes with circuit breakers, per-route attempt timeouts and degrade detection. A request may pin a route (`metadata.brain`). Capabilities are `chat` and `images` only
(`types.rs::Capabilities`). There is no per-task model selection: the planner call always goes to the first healthy
route in the configured order.

## 7. Memory (FACT: `memory.rs`)

SQLite `memory/persistent.db`: `tasks`, `procedures` (goal, keywords, steps, success_count), `failures`
(tool, error_code, context, recovery, count), `app_state`, `user_notes`. Recall is keyword overlap. Up to 40 user
notes are in every prompt. No provenance, confidence or expiry columns. Playbooks (`skills/*.md`, 16 files) are
selected by accent-free trigger words.

## 8. Observability and recovery (FACT)

Per-task JSONL audit with redaction; gateway log with attempts; worker restart on death/hang; checkpoint = task
record; `docs/recovery.md` lists failure modes. Correlation: `task_id` travels to the gateway as request metadata.

## 9. Tests and baseline (measured 2026-10-14)

| Suite | Result |
|---|---|
| `cargo test --workspace` (debug) | lga-core 52 passed + 4 ignored (before this audit), worker IPC 1, gateway 17 + 15 + 9, brain_core 7, agent-desktop 4 + 3 ignored |
| python-agent `pytest tests` | 268 passed, 19 skipped (live desktop/AutoCAD/browser) |
| chrome-gemini `pytest` (default) | 86+ passed, real-Chrome suites opt-in (`LGA_RUN_LIFECYCLE=1`) |
| benches (`tests/bench`, 159 tasks, 11 categories) | **not re-run in this audit** (need a live brain; Ollama cloud monthly limit was reached 2026-10-13: UNKNOWN today) |

Unit tests of the agent loop itself are thin: `agent.rs` has two tests; completion verification is exercised by
benches, not by unit tests (gap G7).

## 10. Machine (not re-measured here)

Target: Windows 11, Core Ultra 7 265K, ~64 GB RAM, RTX 5060 Ti 16 GB. VRAM is shared with the owner's other
programs; earlier sessions measured the local brain at 25–27 tokens/s only when the GPU was otherwise free
(`docs/local-brain.md`). **UNKNOWN today**: current free VRAM and token rates (not measured in this audit).
