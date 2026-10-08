# Failure recovery

Every failure has a bounded, deterministic response. Nothing retries forever.

| Failure | Detection | Response | Bound |
|---|---|---|---|
| Tool timeout | worker deadline (`deadline_unix_ms`) | `TIMEOUT`; retried with backoff only if the tool is idempotent; otherwise the planner observes first | `[retry] max_attempts` |
| Retryable tool error (`STALE_ELEMENT`, `WINDOW_NOT_FOUND`, `BUSY`, …) | error `retryable` flag | idempotent tools: automatic retry with exponential backoff and jitter; others: the planner gets the error plus a recovery hint | `[retry]` |
| Stale UI element | element id unknown or runtime id changed | the worker first re-locates the element by its locator (window, name, type, automation id); if ambiguous, `STALE_ELEMENT` is raised; the core auto-observes (`desktop.inspect_ui`) and the planner retries with a fresh id | once per step, then planner |
| Element not found by UI Automation | `ELEMENT_NOT_FOUND` | hint: re-inspect, scroll, then `vision.ocr` / `vision.locate`; a located point is cross-checked with `ControlFromPoint` | step budget |
| Click had no visible effect | `verification: deferred` | automatic observation after every unverified action; the planner compares and re-grounds (inspect → screenshot → locate → click new coordinates) | identical-action guard |
| Window not found / closed | `WINDOW_NOT_FOUND` | hint: `desktop.list_windows`, refocus or relaunch | step budget |
| Dialog blocks closing | `desktop.close_app` returns `blocking_dialog` | planner handles the dialog explicitly (never auto-dismissed) | step budget |
| Same action repeated | identical `input_hash` in consecutive steps | call rejected before execution: "change your approach" | `[agent] max_identical_actions` |
| Repeated failures | consecutive failed steps | task fails with the last error | `[agent] max_consecutive_failures` |
| Malformed planner reply | JSON parse / shape check | corrective notice and re-ask; then counted as a failed step | `[agent] max_parse_retries` |
| Gemini primary down | connection error, 5xx, timeout | gateway fails over to WebAI-to-API; breaker opens after `failure_threshold`; half-open probe later; dead managed process restarted immediately | breaker + `[gateway.retry]` |
| Gemini rate limited (429) | status code | backoff and retry on the same backend, then failover | `[gateway.retry] max_attempts` |
| Gemini cannot do the request (no account for images) | capability error | failover without penalising the breaker | – |
| Whole gateway unreachable | `/health` | autostart (detached) and wait; planner calls retried with backoff | `[llm] gateway_startup_timeout_ms`, `[retry]` |
| Worker crashed or hung | stdout EOF / no reply after deadline + `hung_grace_ms` | worker killed (job object) and restarted; idempotent call retried; non-idempotent call reported as `BACKEND_UNAVAILABLE` ("observe before retrying") | `[worker] max_restarts` per task |
| COM call stuck (UIA, AutoCAD) | per-call timeout on the STA executor | thread abandoned, new STA thread, `TIMEOUT` | per call |
| AutoCAD busy ("call was rejected by callee") | COM `RPC_E_CALL_REJECTED` / `RETRYLATER` | idempotent calls retried with backoff while pumping messages; otherwise `BUSY` | AutoCAD retry budget |
| Browser crashed / CDP disconnected | `is_cdp_connected` false or Chrome process exited | Chrome relaunched with the dedicated profile, new session; tab/element ids invalidated and reported | per call |
| Browser tab gone | unknown tab id | `NOT_FOUND`; planner lists tabs and switches | step budget |
| Process restart (Chrome, backends) | supervisor | restarts rate limited (gateway: 5 per 10 min) | supervisor window |
| Needs Administrator | `ELEVATION_REQUIRED` | structured escalation request stored on the task and shown to the user; never bypassed | – |
| Runner killed / machine rebooted | task left non-terminal | `agent resume <id>` | – |
| Task state file destroyed | JSON unreadable | load `<id>.json.bak`; if that is unreadable too, rebuild from the audit log `logs/tasks/<id>.jsonl` | – |
| Cancellation | `agent cancel <id>` writes `memory/tasks/<id>.cancel`; Ctrl+C | in-flight worker call cancelled (shell process trees killed); task ends `cancelled` | polled every 500 ms |
| Task budget | wall clock / step count | task fails with an explicit reason | `[agent] task_timeout_ms`, `max_steps` |

## Verified scenarios

| Scenario | Test |
|---|---|
| Primary Gemini backend killed → failover attempted → primary restarted and serving | `services/gemini-gateway/tests/live.rs` (TEST 6) |
| Routing, breaker, rate limit retry, capability failover, broken streams | `services/gemini-gateway/tests/routing.rs` |
| Runner killed mid-task, state file corrupted and backup deleted → `agent resume` rebuilds from the audit log and completes; stale element ids from the dead worker detected and recovered | `tests/e2e/run-e2e.ps1 -Test 8` |
| Shell command cancelled / timed out → process tree killed | `services/python-agent/tests/test_foundation.py`, `test_worker_protocol.py` |
| STA call timeout → thread abandoned, next call served | `test_foundation.py::test_sta_executor_runs_on_single_thread_and_times_out` |
| Browser killed → relaunch and continue | `services/python-agent/tests/test_browser_live.py` (TEST 7) |
