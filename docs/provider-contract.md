# Provider contract and the Chrome Gemini adapter

Status: the contract and the adapter exist and are tested against fakes. The provider is **off by default**,
`brain_order` is unchanged, and no prompt has ever been sent through it. No core AI has been chosen.

## Layers

```
Rust Agent (planner, policy, audit)  ──ChatProvider──▶  gateway (lga-gateway)
                                                          ├─ HTTP backends (api, primary, fallback, cloud, local)
                                                          └─ chrome_gemini ──HTTP /v1/chat──▶ services/chrome-gemini
                                                                                                 └─ CDP ─▶ Chrome, profile runtime/gemini-profile
```

* The agent core only knows `ChatProvider`/`GatewayProvider`. Nothing about pages leaks into the planner.
* One owner per Chrome profile: `services/chrome-gemini` is the only process that opens
  `runtime/gemini-profile`. The Python worker keeps `runtime/chrome-profile`. Both launch Chrome through
  `lga_worker.browser.chrome` (the single launcher in the repository).

## Transaction contract (`services/gemini-gateway/src/provider.rs` ⇄ `chrome_gemini/contract.py`)

| status | meaning | gateway behaviour |
|---|---|---|
| `NOT_SUBMITTED` | nothing reached the page | classified by `error.class`; may retry / fail over |
| `SUBMISSION_UNCERTAIN` | the prompt may have been sent | `ErrorKind::SubmissionUncertain`: **never retried**, breaker not charged, other brains may answer |
| `GENERATING` | sent, completion not proven | same as above |
| `COMPLETED` | exchange finished technically | answer returned; says nothing about correctness |
| `FAILED` | sent, page showed an error | same as `SUBMISSION_UNCERTAIN` |

`NOT_SUBMITTED` classes: `busy`→rate_limited, `browser_unavailable`→backend_down, `not_logged_in` /
`selectors_unverified`→config, `unsupported`→capability, `duplicate`→client_error, others→transient.

Other contract points: capabilities (`text` only; images/files are refused with `unsupported`, never dropped),
`health` (read-only, never sends), `model_verified` (read from the page, `unverified` when unreadable, never
copied from configuration), transaction id per exchange.

## Rules the adapter enforces

1. One lock around the whole exchange (new conversation → submit → wait → read).
2. Each exchange starts in a fresh conversation and is refused if the page already shows an answer.
3. After the point of no return the record is `SUBMISSION_UNCERTAIN` until the page confirms; a repeated
   transaction id returns the recorded outcome and never reaches the page again.
4. `COMPLETED` needs: answer present, page reports *not generating*, same text on N consecutive reads.
   No usable "generating" signal ⇒ stays `GENERATING`.
5. No cookie/token/storage access; the page is only asked for visible state.
6. Selectors are not built in. `configs/chrome-gemini.selectors.json` must carry `"verified": true`
   (set by a human after checking each selector); otherwise every call returns `selectors_unverified`
   before Chrome is started.

## Enabling (not done)

```toml
[gateway.chrome_gemini]
enabled = true
[gateway]
brain_order = ["api", "chrome_gemini"]   # only if you decide so
```

## Tests

* `PYTHONPATH=services/chrome-gemini services/python-agent/.venv/Scripts/python.exe -m pytest services/chrome-gemini/tests`
* `cargo test -p lga-gateway --lib --test chrome_gemini --test routing`

## Open

* Real selectors / completion signals of the Gemini page: UNKNOWN until verified on the live page.
* `CdpGeminiPage` has not been run against Chrome.
* Whether failing over to another brain after `SUBMISSION_UNCERTAIN` is wanted for every task kind.
* Which core AI becomes the default.

## Chrome lifecycle (phase G, measured)

| situation | owner of Chrome | what happens |
|---|---|---|
| Chrome already running on the profile (person/script) | external | adapter attaches (same PID and CDP port), **never closes it**; survives adapter stop, restart and gateway exit |
| Chrome launched by the adapter | adapter (record `chrome-gemini-owner.json` in the profile, valid only for that PID) | closed with `Browser.close` on normal stop |
| adapter stopped normally (`ManagedProcess::stop`) | — | `POST /shutdown` → adapter closes its own Chrome, exits 0; terminated only if it does not answer within 15 s |
| gateway stopped normally | — | `gateway.close()` → same graceful stop, then the job object closes |
| gateway crash / terminate | — | no code runs; the kill-on-close job ends adapter and its Chrome (abrupt: profile not flushed, "restore pages" next time). No orphans |
| CDP lost, Chrome alive | adapter | next call re-finds Chrome by profile and reconnects; no relaunch |

Chrome launched by the adapter is inside the gateway's job (the job has no `BREAKAWAY_OK`; the launcher's
breakaway attempt fails and falls back). `BREAKAWAY_OK` was **not** added: the measured policy needs no
Chrome that outlives the gateway. Opt-in regression: `LGA_RUN_LIFECYCLE=1 pytest services/chrome-gemini/tests/test_lifecycle.py`
(after `cargo build -p lga-gateway --example lifecycle_host`).

## Recovery (phase H1, measured)

* **Model label**: polled until a deadline (6 s, every 0.25 s) after the navigation. States: `verified` (label read),
  `pending` (a zero-wait peek before the label rendered), `unknown` (deadline passed). `unknown` is reported in
  `metadata.signals.model_status` and never replaced by a default; it does not block a transaction. No click.
* **Adapter crash** (gateway alive): the health loop restarted it in ~3 s, same Chrome PID/port; the owner record
  made the new adapter own that Chrome again. A request racing the crash is classified `submission_uncertain`
  and not retried. `restart_if_dead` only fires on a refused connection; here the health loop did the work.
* **One adapter per profile**: an OS byte-lock in the profile (`chrome-gemini-adapter.lock`) makes a second adapter
  exit with code 3. Before this, two adapters on different ports served one Chrome side by side (reproduced).
* **CDP drop, Chrome alive**: reads reconnect once (10 s cap) to the same Chrome/port/tab, never relaunch. Submit
  steps never reconnect. **Chrome gone**: `browser_unavailable` immediately, no relaunch from the read path.

## Shutdown with a transaction in flight (phase H2)

States: the five contract statuses are reused (`GENERATING` is "in progress"); the adapter's lifecycle is
`running → shutting_down → closed` (never backwards). `POST /shutdown` (token required):

1. state becomes `shutting_down`; every new chat, and every chat queued behind the lock, is refused as
   `NOT_SUBMITTED` / `shutting_down` (nothing sent, nothing queued); health reports `shutting_down`;
2. an exchange not yet sent stops at the point of no return (refused, nothing sent);
3. an exchange in flight keeps polling until `--shutdown-grace-s` (config `shutdown_grace_ms`, default 10 s);
   a finished answer is returned as `COMPLETED`; otherwise it ends `GENERATING` (page showed it) or
   `SUBMISSION_UNCERTAIN` with `shutdown_interrupted`, and its caller receives that. Never re-sent;
4. only when no exchange holds the lock is the owned Chrome closed (external: left alone), then the process exits;
5. if the exchange cannot be ended (a page call that hangs), nothing is closed, the process keeps running in
   `shutting_down`, the reply says `shutdown: false, in_flight: true, pid`, and the gateway terminates it.

Token: the gateway generates 32 random bytes per start, passes them in the adapter's environment
(`LGA_ADAPTER_SHUTDOWN_TOKEN`, popped before Chrome starts) and sends `Authorization: Bearer`; compared with
`hmac.compare_digest`; no token configured = endpoint disabled (403). Not in argv, URL or logs.
`/v1/chat` is still unauthenticated loopback: separate item, no design yet.

## Hardening (phase H3)

* **`/v1/chat` authentication.** The gateway generates two independent 32-byte tokens per start and gives them
  to the adapter through its environment (popped before Chrome starts): one for `/shutdown`, one for `/v1/chat`.
  Each endpoint accepts only its own token (`hmac.compare_digest`); an unset token disables the endpoint (403).
  `/health` stays open (read-only). A 403 from the adapter reaches the gateway as a config error (nothing was
  sent, not retried). Tokens never appear in argv, URLs, replies or logs.
* **Failed `/shutdown` replies 503** (`shutdown:false, in_flight, pid`), so the gateway terminates the adapter at
  once instead of waiting out its full `wait`.
* **Frozen Chrome (measured, real Chrome suspended by PID):** owned Chrome + chat in flight: gateway stop 4.0 s
  (grace 1 s + 3 s margin), adapter terminated, the job removed Chrome, no listener left. External Chrome frozen:
  adapter terminated, external Chrome untouched. Idle adapter + frozen owned Chrome: 9.5 s (one 8 s close budget;
  before the fix 18.5 s because the websocket close handshake waited 10 s on the frozen browser).
* Config: `shutdown_grace_ms` (default 10 000) in `[gateway.chrome_gemini]`; the gateway waits it + 15 s.

## End-to-end against a local stand-in page (phase H4A)

`tests/fake_gemini.py` serves a page built from the structure observed in phase F (custom elements, role=textbox,
send button only while the composer has text, `.send-button.stop` / `aria-busy` while generating, one
`model-response > message-content` per answer). It counts, server side, every prompt that really arrives. The
chain Gateway (`gateway_host`) -> adapter -> CDP -> real Chrome -> that page runs with the SHIPPED selector file.
`LGA_RUN_LIFECYCLE=1 pytest services/chrome-gemini/tests/test_e2e.py`.

Findings it produced:
* Completion needed a positive signal. With only "no generating element" the driver read an absent selector as
  "not generating", so a page whose generating marker stopped matching could have been completed half-written.
  New optional selector `answer_done` (`model-response .markdown[aria-busy="false"]`): when configured,
  generating is True / False (only on that match) / None (unknown). Observed once in phase F (n = 1).
* `/health` used to start Chrome. It now only observes (`peek_session`): no Chrome => `degraded`, and the first
  request starts the browser. This also stops a closed window coming back every probe interval.
* `--gemini-url` (tests only) accepts https://gemini.google.com or http://127.0.0.1:<port>; anything else is refused.

## Observations and late answers (H4B follow-up)

* `signals.last_observed_state` (NOT_SUBMITTED / SUBMITTED / GENERATING / COMPLETED / FAILED / UNKNOWN) with
  `last_observed_at` is what the PAGE last showed. It is not the transaction status. The gateway copies it to the
  attempt log (`last_observed_state`) and the message (`transaction <id> ... last_observed_state=GENERATING`); the
  error kind and the no-resend rules do not change. `observation_gap` marks a read failure.
* `pending` (optional selector `pending-response, thinking-dots-animation`) raises the observation to GENERATING and
  confirms the prompt was accepted. It never contributes to COMPLETED.
* **Late answers.** A transaction that ended `GENERATING` / `SUBMISSION_UNCERTAIN` stays that way. If the answer
  appears afterwards, the adapter records `late_observation {state: COMPLETED_LATE, content, observed_at,
  seconds_after_terminal}` beside the unchanged terminal result (status, error, finished_at, content ""). It looks
  (read-only, one read) when `GET /v1/transactions/{id}` is called (chat token) and before the next exchange
  navigates away. Only an unambiguous page (one answer, positive done signal, no pending/error) is recorded.
  Assumption: nobody else used the tab. Nothing is re-sent, no new transaction is created.
* Test host: `chat <timeout_ms>` is capped at 600 s; production timeouts are untouched.
* Measured on the real page (H4B): the Pro answer arrived after the 116 s budget and was a refusal
  ("I can't help with that"); `answer` and `answer_done` matched the real DOM.

## H4B result (2026-10-14)

Real Gemini, one prompt, model `Gemini Flash-Lite` (locked): `COMPLETED` in 18.3 s from submit (20.8 s Gateway call),
`submit_calls=1`, 1 attempt, positive `answer_done` (3 stable polls), answer `2 cộng 2 bằng 4.`, new conversation with
exactly one user turn, adapter gone and Chrome (external) untouched, production unchanged. Earlier attempt on `3.1 Pro`:
answer arrived after the 116 s budget and was a refusal; Pro is not used on this account. Not exercised in the
successful run: the graceful `/shutdown` path (the host was terminated to leave the operator's Chrome alone); it is
covered by the lifecycle/shutdown/hang suites. Provider still disabled; core AI not integrated.
