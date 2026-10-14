# Implementation roadmap

Order follows risk and dependency: things that can be proven offline and cannot change production behaviour come
first. Each item lists its test and what it must not touch.

## Done in this round (2026-10-14)

| Item | Gap | Change | Evidence |
|---|---|---|---|
| Untrusted-content rule | G1 | `planner.rs`: rule 18, labelled observation block, `defang_headings` | 3 new unit tests; `cargo test -p lga-core` 56 passed (52 before + the 3 planner tests + 1 state test) |
| Machine-readable completion outcome | G2 | `TaskRecord.completion_basis`, `unverified_checks`; set in `verify_completion`; CLI shows `completion: unverified` | state test (old records load); build of the workspace |
| Baseline + architecture docs | — | `docs/architecture/*` | — |

Not yet effective in the running product: the release binaries were not rebuilt (running processes are untouched).
The new prompt text takes effect at the next `cargo build --release` + restart of the chat window / CLI.

## Next (ordered)

| # | Item | Gap | Deliverable | Test / gate | Must not touch |
|---|---|---|---|---|---|
| 1 | Offline injection / exfiltration suite | G5 | scripted planner replies against recorded observations: hostile page/document/OCR text that asks for shell, file deletion, secret disclosure, task change; asserts policy denies and the prompt labels it data | deterministic, no brain | routing, config |
| 2 | Contract lint for tools | G6 | test that every tool has `side_effects`, a known `policy_category`, `timeout_ms > 0`, a post-condition for high risk; migrate the 68 derived tools mechanically (value = current effective behaviour) | lint test + unchanged `changes_state()` for every tool | tool behaviour |
| 3 | Unit tests for verification branches | G7 | `verify_completion` factored behind a small trait so completion checks can run against a fake worker: passes, fails, accept-anyway, direct answer, evidence step missing | unit tests; no behaviour change | loop semantics |
| 4 | Show `completion_basis` in the chat window | G2 | badge "chưa kiểm chứng đủ" from the record, not from text | UI test (`logs/dev/ui_conv_test.py` pattern) | API shape (additive) |
| 5 | Token / latency totals per task | G9, G10 | counters in `TaskRecord`, soft token budget with a notice | unit test | planner prompts |
| 6 | Capability registry + probes | G3 | per-route capabilities and measured latency; read-only listing in `agent doctor` | gateway tests | routing order |
| 7 | Memory provenance | G4 | additive columns (`source`, `confidence`, `expires_at`), migration with defaults, recall ordering by source | migration + recall tests | existing rows |
| 8 | Routing hints by task kind | G3 | opt-in config `[routing.by_kind]`, default empty = current behaviour | gateway tests; a benchmark run before any default change | production order |
| 9 | Method selector for desktop actions | G13 | record chosen method per step (COM/UIA/CDP/vision) | live GUI bench subset | existing tools |
| 10 | Live bench refresh | G10 | run `run_bench.py` per category when a brain and the desktop are free; report model / orchestration / tool failures separately | bench report | — |

## Rollback

Items 1–5 are code-only and additive; reverting the commit restores the previous behaviour. `completion_basis` is a
`serde(default)` field, so records written by new binaries still load in old ones (unknown keys are ignored by the
reader in old versions of `TaskRecord` only if they use `deny_unknown_fields`; they do not: FACT in `state.rs`).

## Round 2 result (2026-10-14)

Items 1, 2, 3 and 4 of "Next" are done (offline security corpus, contract lint + explicit `side_effects`, completion logic
extracted with unit tests, `completion_basis` in the chat window). A staging build containing them exists
(`docs/architecture/release-staging.md`); it has not been promoted. Remaining from the list: 5 (token/latency totals),
6 (capability registry), 7 (memory provenance), 8 (routing hints), 9 (method selector), 10 (live bench refresh).

## Round 3 result (2026-10-14)

Always-confirm policy implemented and tested (guard.rs, policy hook, `confirm_always`, ledger, UI card, corpus). Staging rebuilt and
verified; NOT promoted. Remaining before promotion: wait for the running production task (pid 27996) to finish, owner's go-ahead,
then the promotion steps in `release-staging.md`. Still open from the earlier list: 5 token/latency totals, 6 capability registry,
7 memory provenance, 8 routing hints, 9 method selector, 10 live bench refresh (needs a brain and a free desktop).
