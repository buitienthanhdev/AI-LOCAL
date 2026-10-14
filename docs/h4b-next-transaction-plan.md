# H4B: plan for the one remaining real transaction (NOT approved, NOT run)

Goal: obtain, once, a `COMPLETED` transaction on the real Gemini page, from one send to one terminal state,
and judge separately whether the answer is any good. Nothing here is executed until the operator approves it
in so many words.

## Preconditions (all must hold; any failure = stop, nothing sent)
1. The operator has looked at the Gemini window by hand and reported: no limit/error/account notice, and that a
   manual prompt of theirs is answered (their action, not ours). If Gemini refuses a plain question by hand too,
   the content criterion below cannot be met by any integration and the run is not worth starting.
2. Profile `runtime/gemini-profile` (the only signed-in one), Chrome found from the profile, no CDP client, no
   adapter or host on it, exactly one Gemini tab, empty composer, no conversation turns.
3. Model label equals `Gemini Flash-Lite` exactly, enforced by `--require-model` (config `require_model`; the adapter refuses
   otherwise and never switches model). `3.1 Pro` is excluded: on this account it refused plain questions
   (2 + 2, "what can you do") while `3.5 Flash-Lite` answered normally (operator's manual comparison).
4. Test host only (never `lga-gateway.exe`), random per-run tokens, `enabled=false` and `brain_order=["api"]`
   untouched, production Gateway pid and `config.toml` hash recorded before and after.
5. Preflight report (read-only) green on the day.

## The transaction
* One prompt, fixed beforehand: the operator chooses it; `2 + 2 ...` is not required (a refusal there is already
  known). A prompt that Gemini answers by hand in the pre-check is preferable.
* One `chat` command to the test host, timeout set before the run (suggested 600 000 ms, the host cap), no
  retry anywhere. The adapter's own budget is that value minus 5 s.
* Evidence recorded: transaction id, `submit_calls`, `submitted_at`, `first_generating_at`, `completed_at`,
  `last_observed_state` + `last_observed_at`, model label before/after, page counts (user turns, answers), the
  answer text, and the shutdown/cleanup facts. No cookies, tokens or HTML.
* Afterwards: terminate the host (it must not close the operator's Chrome), confirm no listener/adapter is left.

## Success criteria (reported separately, never merged)
| Axis | PASS means | Anything else |
|---|---|---|
| Transaction | exactly one prompt sent; terminal status `COMPLETED` reported to the caller before the deadline; positive done signal (`answer_done`) seen, not merely "generating disappeared"; answer text belongs to this conversation (one user turn = our prompt) | `PARTIAL`: sent once but not `COMPLETED`; `FAIL`: not sent / wrong model / duplicate |
| Content | the answer actually answers the prompt | a refusal or a wrong answer is `CONTENT_FAIL`, reported as such; it never turns the transaction axis into a failure and is never counted as quality success |

## What does not count
* A `late_observation` (answer seen after the transaction ended): supplementary evidence only, marked
  `official: false`. It does not turn a `GENERATING` / `SUBMISSION_UNCERTAIN` result into `COMPLETED`.
* An answer that appears only after a manual action by the operator.

## Abort rules
Any precondition failing, a second user turn appearing, a model label other than `Gemini Flash-Lite`, Chrome/CDP lost
before the send: stop and report. After the send there is no abort that resends; the transaction is only observed.
