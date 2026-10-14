# Release staging (updated 2026-10-14, after the always-confirm policy)

Built **next to** production, never over it:

```
cargo build --release --target-dir target/staging -p agent-cli -p agent-desktop -p lga-gateway
cargo test  --release --target-dir target/staging --workspace
```

Source: HEAD `a9e9972` plus the uncommitted working tree (includes the Chrome Gemini provider, disabled by default).

| Artifact (target/staging/release) | sha256 | Production (target/release) sha256, untouched |
|---|---|---|
| agent.exe | f1ffca5ee1a6139422fd90b19969d3d6f53a02de8f79463281fd3c94cc033a2b | 826041c7d1882eb1eeeb8933bc4e6d3e8c68f5d6cac38ffa04c5c17f31c40cd4 |
| agent-desktop.exe | f48cd81ceccd80dc9cc189abaab2f89f2fdd190a40097730917eb41e4642634c | 97060d56e760cfc14da12fac45340656fde6bdfedfe643158f74ffcf8bfd6f52 |
| lga-gateway.exe | 177c8e016f15745c99d345365000461758ff63a2b3400a07c195647deb9926d6 | d5b3af4f7d6285744573766391556f013130f79b1d60649bcca39ebbb7696af6 |

## What the staged binaries add over production
* G1 untrusted-content rule and heading escaping; G2/G7 `completion_basis` + `completion.rs`; UI badge for unverified completions.
* **Always-confirm guard** (`core/rust-agent/src/guard.rs`): deletion outside scratch areas (recursive / bulk / single),
  data sent out (shell, python, browser upload, e-mail send), shutdown/restart, system changes (services, registry,
  scheduled tasks, installs, process kills, `schedule.create`), writes to protected system locations, `file:` URLs and
  unclassifiable (encoded / evaluated) commands need a person per action in EVERY mode, `full_access` included.
  `--yes` and unattended runs decline them. The scan covers python.exec `code`, not only shell `command`.
* Confirmation card in the chat window marked "luôn cần bạn xác nhận" with the reasons and destination.
* Redaction of AWS keys / JWT / Slack tokens / PEM blocks; the approval channel files are unreachable to tools.

## Verified (PASS)
* `cargo test --release --target-dir target/staging --workspace`: **150 passed, 0 failed, 11 ignored** (live/desktop tests).
* Debug workspace 150 passed; python-agent 280 passed / 19 skipped; chrome-gemini 86 passed / 28 skipped (real-Chrome suites opt-in).
* Security corpus 14 tests, tool-contract lint 6, guard unit tests 6 (incl. a real junction), interaction tests (no answer /
  wrong id / explicit yes), CLI test (`--yes` does not cover always-confirm), UI tests (`tests/ui/*.py`) all pass.
* Strings/behaviour in the artifacts: rule 18, `always-confirm [`, class names and `completion_basis` are in the staged
  `agent.exe`; the confirmation heading and the unverified notice are in the staged `agent-desktop.exe`; none of this is in
  the production binaries (hashes above differ). `agent.exe status` on a record with `completion_basis: unverified`
  prints the notice; a record without the field loads unchanged.
* `configs/config.toml` unchanged (`sha256` prefix `8241d2b2`), `brain_order=["api"]`, `chrome_gemini.enabled=false`.

## Already live without a new binary (read at runtime from the repository by every `agent.exe` task)
* `tools/*.json`: explicit `side_effects`; 4 read-only tools stop counting as state-changing.
* `core/rust-agent/policy/default.toml`: `.env` variants and the approval-channel files (`*.request.json`,
  `*.response.json`) are denied; `[always_confirm]` section (read only by the new binary, ignored by the old one).
* A production task started after 12:13 on 2026-10-14 (e.g. the Van Gogh painting run, pid 27996) already runs with these.

## Production state at the time of writing (read only)
`lga-gateway` pid 39556, `agent-desktop` pid 18580 and a running `agent` task pid 27996 are all on the OLD binaries
and were not touched. Promotion must wait for that task to end.

## Not done (needs approval)
Nothing copied over `target/release`; no process stopped or restarted.

## Promotion (when approved)
1. Let running tasks finish; close the chat window; stop the gateway (`Tắt Trợ lý.bat`).
2. Copy `target/release/{agent,agent-desktop,lga-gateway}.exe` to `target/release-prev/`.
3. Copy the three staging artifacts over `target/release/`.
4. Start with `Chạy Trợ lý.bat`; run `agent.exe doctor`; try one harmless request and one always-confirm request
   (e.g. "tắt máy sau 1 phút" must show the card and do nothing until you press the button).

## Rollback
Copy `target/release-prev/*.exe` back and restart. Records with the new fields stay readable by old binaries.
To revert the two runtime-read files: `git checkout HEAD -- tools core/rust-agent/policy/default.toml` (verified: they carry no
other local change).

## Behaviour change to expect after promotion
Scheduled tasks and `agent run` without a terminal cannot perform always-confirm actions (they are declined with a
message the planner sees). Benchmarks run in `C:\temp\lga-bench` (scratch) are not affected.

## Update 2026-10-14: assistant banner and screen-action feedback

Rebuilt with `cargo build --release --target-dir target/staging` after two small changes (hashes above are the new ones):

- **Banner** (`core/rust-agent/src/banner.rs`, `BANNER_TEXT` in `agent.rs`): rounded two-line card (title + hint), Segoe UI, green dot; still always-on-top, never focused, click-through. Only the compiled exe shows it; not yet looked at on a real screen.
- **Screen-action feedback** (Python worker, loaded from the source tree, no exe involved): `desktop.click` by coordinates now names the control under the point (and says when none is named); `desktop.observe` adds a "nothing visible changed" note when two observations in a row are identical. Pure helpers in `lga_worker/windows/feedback.py`, 6 unit tests. Contract: optional `under_pointer` (click) and `no_change` (observe) output fields.

Because the worker is read from the source tree, the Python change is live for any NEW worker process started from this tree (production included); running workers keep what they loaded. Rollback: delete `lga_worker/windows/feedback.py` and revert the three small hunks in `ops.py` / `tools/desktop.py` (see the file diffs).

Tests: python-agent 286 passed / 1 skipped (live desktop, AutoCAD, browser and vision suites excluded: they drive real windows); security corpus 14, tool contract lint 6, `classify_side_effects.py --check` OK. Not run: any live desktop test (a production task was running on the same screen).
