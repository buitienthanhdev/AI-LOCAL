# Target state

The audit found a runtime that already has the main structure the brief asks for (state machine, core-side policy,
schema-validated tools, core-run verification, checkpoints, recovery, audit). The target is therefore **incremental**:
keep the loop, close the gaps in `gap-analysis.md`, and add the pieces that are really missing. No rewrite.

## Principles

1. Policy, verification and state transitions stay in the Rust core; nothing a model says can change them.
2. Everything a tool, page, file or OCR returns is data. It is labelled as such to the model and can never become a
   section of the prompt (done: G1).
3. A task's outcome is a record, not a sentence: `status` + `completion_basis` + evidence (done: G2).
4. Any change is additive and testable offline; production routing, providers and configuration are not changed by
   engineering work without a separate approval.

## Layers and what changes

| Layer | Now | Target | Gap |
|---|---|---|---|
| Task runtime | `TaskRunner` + `TaskStatus` | same machine; split `agent.rs` along existing seams (plan / execute / verify) only when a test pins the behaviour first | G7 |
| Task understanding | one planner prompt decides everything | a small, deterministic *pre-classification* record (kind: answer / lookup / act; touches UI?; destructive?; sensitive?) computed in the core from the request + tool groups already loaded, used for verification depth and routing hints, never for permissions | G3 |
| Model routing | static order, `chat`/`images` capabilities | capability registry per route (tool calling, JSON schema decoding, vision, context tokens, measured latency/tokens/s) filled by probes and run history; the router picks among *healthy, capable* routes by task kind. Production order stays the default until a benchmark justifies a change | G3 |
| Context / memory | keyword recall, unscored | entries carry `source` (user / tool / model), `created`, `confidence`, optional `expires`; user-stated facts outrank model-written ones; contradictions surface instead of silently coexisting | G4 |
| Tools | JSON contract, risk, idempotency, policy category | every tool declares `side_effects`; a contract-lint test fails a tool without it; high-risk tools list their post-condition check | G6 |
| Computer use | planner picks the tool by rules | an explicit method order per target (API/COM → UIA → CDP → vision) recorded in the step, with the chosen method and why | G13 |
| Verification | core re-runs read-only checks; LLM second opinion for UI work | unchanged, plus unit tests for each verification branch and a UI surface for `completion_basis` | G2 rest, G7 |
| Evaluation | 159-task live bench | + deterministic offline suites (contracts, security/injection, state machine) that run in CI without a brain; live benches stay separate and report model vs orchestration vs tool failures | G5, G10 |
| Observability | audit JSONL, gateway attempt log | + per-task token and latency totals in the record (budget, G9) | G9 |
| Providers | gateway routes; Chrome Gemini adapter OFF | unchanged; adapter stays disabled until the owner decides | G11 |

## Non-goals (this round)

* A vector database or reranker: the knowledge subsystem already has hybrid retrieval (Qdrant + FTS5 + RRF); nothing
  here shows a need for another.
* Replacing the planner model, or any claim of beating a particular model.
* New desktop capabilities without a use case and a test.
