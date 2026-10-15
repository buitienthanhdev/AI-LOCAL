# Links and paths, learning memory, clarification dialog

## Links and paths (chat window)
* One detector (`LGA_LINKS` in `apps/agent-desktop/ui/index.html`) scans every text that appears in the chat, the activity
  panel and the settings: http(s) URLs, `C:\...` paths (spaces, Unicode), UNC paths, relative paths (`src\a.rs`, `./x.json`),
  `path:line:col` and Python `line N`. Markdown links and result file chips use the same opener.
* Click = open, Shift+click = reveal in Explorer, right click = menu (open, reveal, VS Code at the line, copy), Enter on focus.
* `POST /api/open` (`apps/agent-desktop/src/openlink.rs`) decides: only `http`/`https`; other schemes refused; device paths
  (`\\?\`) refused; relative paths resolve inside the workspace or `artifacts/<task>` only; programs and scripts
  (`exe, bat, cmd, ps1, msi, lnk, ...`) are never started, only revealed; a missing path is reported as `not_found`
  with the nearest existing folder. Arguments go to Explorer/browser as an argument vector, never through a shell string.
* Tests: `cargo test -p agent-desktop openlink`, live: `cargo test -p agent-desktop -- --ignored live_open`,
  UI: `python logs/dev/ui_links_test.py`.

## Memory (`memory/persistent.db`)
| layer | storage | written | used |
|---|---|---|---|
| working | task record + planner window | during a task | every planner turn |
| episodic | `memories(kind=episodic)` | `Agent::finish` (no model call) | when the request shares >= 2 keywords |
| feedback | `memories(kind=feedback)` | 👍 / 👎 / teach tab | when similar; repeated corrections raise `hits` |
| semantic | `user_notes`, `memories(kind=semantic)` | "remember", memory tab | notes always, semantic when relevant |
| procedural / failures | `procedures`, `failures` | verified successes / observed errors | ranked by keywords |

Every row has source, confidence, evidence (verified/unverified/user), status (active/disabled/conflict), hits and use
counts. Secrets are refused (`Memory::looks_sensitive`). Near-duplicate rules with different wording are flagged as
`conflict` and shown to the planner as such. The settings tab "Bộ nhớ của AI" lists, searches, edits, switches off and
deletes any row (`/api/memory`). No weights are changed.

## Clarification dialog
`task.ask_user {question, options[], multi}` -> `Interaction::ask_choice` -> request file `{kind: ask, options, multi}` ->
dialog in the chat (single = radio, multi = checkbox, "Khác — nhập yêu cầu riêng" always last, free text when there are
no options, validation, retry on send failure). The answer text goes into the task's `conversation` and the task
continues from the same step. Tests: `python logs/dev/ui_clarify_test.py`, core: `cargo test -p lga-core interaction`.
