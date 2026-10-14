# Gap analysis (2026-10-14)

Classes: **C** critical correctness/security, **R** reliability, **I** intelligence/reasoning, **P** performance,
**M** missing capability, **T** maintainability, **U** unverified assumption. Status: fixed / open / not-touched
(constraint) / unknown.

| # | Class | Finding (evidence) | Status |
|---|---|---|---|
| G1 | C | The planner prompt had no rule that tool output is data. `planner.rs::system_prompt` rules 1–17 contain none; `docs/security.md` asserts "untrusted data" but nothing told the model. Observations were pasted under `# Latest observation`, and a line of tool output starting with `# ` could pose as a prompt section (`# Task`, `# Tools`). Policy still blocks dangerous *actions* regardless (FACT), so the exposure was the model being steered, not unrestricted execution. | **fixed**: rule 18, data label on the observation block, `defang_headings` on observations, step observations and recalled memory; 3 unit tests. Model compliance is **UNKNOWN** (no injection benchmark, G5). |
| G2 | C | A completion accepted after 2 rejected verifications (`agent.rs` ~1228) finished as plain `Completed`; the only signal was a "⚠" text prefix in `final_summary`. Any consumer reading `status` saw success. | **fixed**: `TaskRecord.completion_basis` (`verified` / `unverified` / `direct_answer`) + `unverified_checks`, old records still load (test), CLI prints it. The chat window does not show it yet (open). |
| G3 | I | No model routing by task: every planner turn uses the first healthy route (`gateway.rs::ordered_routes`); capabilities are `chat`/`images` only (`types.rs`), so tool-calling, structured output, context length and latency are not part of routing. Cheap tasks (greeting, extraction) cost a full planner call. | open, **not touched**: production routing must not change. Design in target-state. |
| G4 | I | Memory is keyword-matched and unscored: no provenance, confidence, expiry or contradiction handling (`memory.rs` schema). Model-written procedures and failure contexts are recalled with the same weight as user-stated notes. | open |
| G5 | M | No security / prompt-injection benchmark: `tests/bench` has 159 tasks in 11 categories and none targets injection, exfiltration or confused-deputy cases. | open |
| G6 | T | `side_effects` is explicit for 39 of 107 tools; the other 68 derive it from `verification`. Derived values cannot say "possible" vs "yes", and a new tool can silently get the wrong class. | open |
| G7 | T/R | The loop's decisive logic (completion verification, batch handling, retry rules) lives in a 1 292-line `agent.rs` with 2 unit tests. Behaviour is pinned by live benches that need a brain and the desktop. | open |
| G8 | R | Task states have no explicit `blocked` / `recovery_required`; `needs_user` and `recovering` cover them. Naming differs from the target vocabulary only. | accepted (not worth a migration) |
| G9 | R | `MAX_COMPLETION_REJECTIONS` bounds rejections but not total planner tokens per task; there is a step budget and a time budget, no token/cost budget. | open |
| G10 | P | No current numbers for VRAM, tokens/s, time-to-first-token or end-to-end latency; benchmarks exist (`run_bench.py`) but were not run (no brain available today). | unknown |
| G11 | U | Chrome Gemini adapter: one real `COMPLETED` on Flash-Lite, one Pro refusal. Reliability over many prompts, long answers and rate limits is not established. Provider is OFF and stays OFF. | open (constraint) |
| G12 | R | Only one `ChatProvider` implementation (`GatewayProvider`); the core cannot use a model without the gateway. Acceptable (one process boundary), but offline use depends on the gateway process being healthy. | accepted |
| G13 | M | Windows automation selects among COM / UIA / CDP / vision by *tool choice of the planner*, guided by prompt rules 4–5, not by a method selector with confidence. | open |

## What was verified to already exist (so it is not rebuilt)

State machine with checked transitions; atomic checkpoints and resume; policy enforced in the core (not by the prompt);
schema-validated tool calls; read-only completion checks re-run by the core plus an independent verifier; retries only
for idempotent tools; worker restart on hang; redaction; audit log; gateway breakers/health/attempt log; transaction
contract for non-idempotent browser exchanges (`SUBMISSION_UNCERTAIN`); prompt budget (`max_context_tokens`); skill
playbooks; attachments; activity tracking.

## Explicitly not claimed

No statement here says the agent is "smarter" or "production-grade": the benches that would show it were not run, and
the baseline above is unit/contract level.

## Status update (after the second round, 2026-10-14)

| # | Status | What changed | Evidence |
|---|---|---|---|
| G1 | fixed, hardened | heading escape now covers indented, full-width and small `#`, setext underlines and every Unicode/control line separator; applied to observations, step observations and recalled memory | `core/rust-agent/tests/security_corpus.rs` (20 hostile payloads x 3 carriers) |
| G2 | fixed end to end | `completion_basis` / `unverified_checks` set by the core, passed by the API, shown in the chat window (warning header, count, verbatim list); old records unchanged | state test, API test, `tests/ui/completion_basis_test.py` |
| G5 | **done offline** | `tests/security/corpus.json` + `security_corpus.rs`: prompt boundary, runtime authorisation (31 must-deny, 13 must-not-allow-without-consent), redaction, audit trail. Model obedience stays NOT_TESTED (needs inference) | 10 tests pass |
| G6 | **done** | explicit `side_effects` on 107/107 tools with evidence (`scripts/classify_side_effects.py`, `tool-contract-report.md`); contract lint (6 tests) fails a tool without it | lint negative-tested |
| G7 | **done for the decision logic** | `completion.rs` (assessment, second-opinion rule, accept/reject + basis) with 10 unit tests; `agent.rs` -247 lines. The orchestration around it (`verify_completion` itself) is still covered by live benches, not unit tests | 66 core tests |

### New findings from the security corpus (all fixed unless listed)

* `python.exec` bypassed every shell pattern: policy scanned `command` only and `python.exec` carries `code`. Fixed: both are scanned (credential/UAC denies now hold for Python code too).
* `.env.local` / `.env.production` / `.env.*.local` were readable (only the exact `.env` path was denied). Fixed in `policy/default.toml`.
* Redaction missed AWS access key ids, JWTs, Slack tokens and PEM private-key blocks. Fixed in Rust and in the Python mirror.
* **Open, by the owner's choice:** in `full_access` (the production mode) recursive delete, `curl`/`Invoke-WebRequest`
  (exfiltration), `shutdown`, writes to protected system paths and `file:` URLs are **allowed without confirmation**.
  Pinned in the corpus (`accepted_in_full_access`). Credential stores, private keys, UAC and elevation stay denied in every mode.
* **Open, inherent:** a regex deny-list is not a boundary against obfuscation (string assembly, encoded commands);
  pinned as `known_gaps`. Mode and confirmation are the real control.

## Status update (third round, 2026-10-14: always-confirm policy)

| # | Status | Change |
|---|---|---|
| G5 | extended | the corpus now pins an `always_confirm` group (35 calls, asked in all three modes), `ordinary_allowed` (15 calls that must stay Allow in full_access and trigger no class), approval-forging and approval-channel cases. Model obedience is still NOT_TESTED |
| new | fixed | the `full_access` exposure listed under "Open, by the owner's choice" is closed for deletion, data sent out, shutdown, system change, protected writes, `file:`; credentials/UAC were already denied |
| new | fixed | a task could answer its own confirmation by writing `memory/tasks/<id>.response.json`; the file names are now denied to filesystem tools, shell and python (path/text deny, not an authenticated channel) |
| new | fixed | links: a junction inside a scratch folder pointing outside no longer makes a deletion "scratch" (resolved before the check; unresolvable = outside) |

### Known gaps that remain (by design or by limit)

* Pattern classification is not a boundary against obfuscation: reading a private key through assembled strings (a
  PowerShell `Get-Content` of a path built with `+`) is still not denied (pinned as a known gap; `-enc`, `iex`, `exec(`,
  `b64decode(` are caught as `opaque_command`).
* `autocad.erase` / document edits in office apps are not always-confirm (they are reversible in the app; listing them would
  ask during ordinary drawing work).
* Reading data and sending it by other means than the listed network tools/libraries (a custom binary, a browser form typed by
  `browser.type`) is not classified; `browser.type` into forms is not treated as sending.
* "Resources outside the task's scope" is only enforced for the classes above; there is no per-task allow-list of folders.
* Unattended runs (scheduled tasks, `agent run` without a terminal) now decline always-confirm actions instead of performing
  them: safe, but a scheduled clean-up of a user folder will report "declined".
* The approval channel is a file pair; an authenticated channel (named pipe with ACL) would remove the dependency on the path
  deny. Not done.
