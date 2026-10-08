# Tool contract

Every action the agent can take is a **tool** declared in `tools/*.json`. That contract is the only
thing the planner model (Gemini) ever sees. Gemini never sees Python functions, provider classes or
backends; it names a tool and supplies arguments. The **local tool router** (Rust core) validates the
call, applies policy, picks the implementation and executes it in the Python worker.

```
Gemini ── {"tool": "autocad.create_circle", "args": {...}} ──► Rust core
           validate(input_schema) → policy(mode, category, paths) → confirm? → route → worker → verify
```

## Tool definition fields

| Field | Meaning |
|---|---|
| `name` | `category.action`, unique across all files |
| `description` | What the tool does, written for the planner. Includes how success is verified. |
| `input_schema` | JSON Schema (2020-12) for `args`. `additionalProperties: false` everywhere; defaults are applied by the worker. |
| `output_schema` | JSON Schema for `result.data`. The worker rejects its own output if it does not conform (`INTERNAL`). |
| `timeout_ms` | Upper bound for one call. The core sends `deadline_unix_ms = now + min(timeout_ms, remaining task budget)`. |
| `risk_level` | `low` / `medium` / `high` / `critical`. Input to the policy engine. |
| `requires_confirmation` | Always ask the user before running, in every mode. |
| `idempotent` | Repeating the call with the same args leaves the same state. Only idempotent calls are retried automatically after an ambiguous failure such as a timeout. |
| `supports_rollback` | The effect can be undone by another tool (for example `desktop.launch_app` ↔ `desktop.close_app`, `filesystem.write` of a new file ↔ delete). |
| `verification` | `self`: the tool checks its own post-condition and fails with `VERIFICATION_FAILED` otherwise. `observe`: the tool cannot prove its effect, so the core observes afterwards and the planner must check. `none`: read-only. |
| `policy_category` | Key into the policy rules (`desktop.input`, `desktop.app`, `shell`, `filesystem.read`, `filesystem.write`, `browser.*`, `vision.*`, `autocad.read`, `autocad.write`, `autocad.destructive`). |

Shared sub-schemas live in each file's top-level `$defs` and are injected into every tool schema of that file.

## Result envelope

Successful call (`status: "ok"`):

```json
{
  "data": { "...": "conforms to output_schema" },
  "verification": {
    "status": "passed | failed | deferred | not_applicable",
    "checks": [{"name": "read_back_matches", "passed": true, "detail": "11 bytes sha256=b94d27b9934d"}]
  },
  "artifacts": [{"kind": "screenshot", "path": "artifacts/screenshots/<task>/<step>.png"}],
  "observation": "short text the planner reads"
}
```

* `passed`: every post-condition check held.
* `failed`: an `observe` tool's checks failed, for example `shell.exec` with a non-zero exit code or `desktop.type` whose text is not in the field. The core treats the step as failed and starts recovery.
* `deferred`: there is nothing to check locally (a raw click). The core runs an observation before the next planning step.
* `not_applicable`: read-only tool.

A `self`-verified tool never returns `failed`; it raises `VERIFICATION_FAILED` instead.

## Error model

`status: "error"` carries `{"code", "message", "retryable", "details"}`. The codes are identical in Python (`lga_worker/errors.py`) and Rust (`ErrorCode`):

| Code | Retryable by default | Typical recovery |
|---|---|---|
| `INVALID_ARGUMENT` | no | planner fixes the arguments |
| `UNKNOWN_TOOL` | no | planner picks a listed tool |
| `NOT_FOUND` | no | planner re-plans |
| `WINDOW_NOT_FOUND` | yes | list windows, relaunch or refocus |
| `ELEMENT_NOT_FOUND` | yes | re-inspect, scroll, use vision grounding |
| `STALE_ELEMENT` | yes | re-inspect UI, retry with fresh `element_id` |
| `TIMEOUT` | yes | idempotent: retry with backoff; otherwise observe first |
| `CANCELLED` | no | task stops |
| `POLICY_DENIED` | no | planner chooses another approach or asks the user |
| `ELEVATION_REQUIRED` | no | the core returns a structured escalation request to the user; never bypassed |
| `BACKEND_UNAVAILABLE` | yes | provider restart / worker restart |
| `BUSY` | yes | backoff and retry (for example AutoCAD rejecting COM calls) |
| `VERIFICATION_FAILED` | yes | observe, re-ground, retry once with changed approach |
| `EXECUTION_FAILED` | no | observe and re-plan |
| `UNSUPPORTED` | no | use a different tool (for example `desktop.type` instead of `desktop.set_value`) |
| `CONFLICT` | no | planner decides (for example `overwrite: true`) |
| `INTERNAL` | no | logged with traceback; counted as a failed step |

`details` is structured. Examples: `blocking_dialog` for `desktop.close_app`, `escalation_request` for `ELEVATION_REQUIRED`, `checks` for `VERIFICATION_FAILED`.

## IPC framing

Core ↔ worker messages are one JSON object per line over the worker's stdio. See `services/python-agent/lga_worker/protocol.py`:

* request: `request_id, task_id, step_id, tool, args, deadline_unix_ms`
* response: `request_id, status, result, error, metrics{started_unix_ms, duration_ms, backend}`
* plus `cancel`, `ping`/`pong`, `ready`, `shutdown`.

The worker's stdout carries nothing else. Its logs go to stderr and `logs/worker/`.

## Core-handled pseudo-tools

These appear in the planner's tool list but are executed by the core itself, not the worker:

* `task.complete {summary, evidence_steps, checks[]}`. The core re-runs each listed read-only check, for example `filesystem.stat` or `autocad.verify_geometry`, and accepts completion only when all pass.
* `task.ask_user {question}`. Interactive CLI only; otherwise the task pauses with status `needs_user`.
* `task.fail {reason}`. The planner gives up with an explanation.

## Adding a tool

1. Add the definition to the right `tools/<category>.json`.
2. Register a handler with `@registry.handler("<name>")` in `services/python-agent/lga_worker/tools/<category>.py`.
3. The worker refuses to start a handler without a contract entry. Contract entries without a handler are reported by `agent doctor`.
4. Add a policy rule if the tool uses a new `policy_category`.
