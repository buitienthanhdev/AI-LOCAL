# Security

## Trust boundaries

| Component | Trust |
|---|---|
| Gemini (planner) | **Untrusted.** Its output is a proposal. It cannot run anything itself; every proposed call is schema-validated, policy-checked, and executed only by the local router. |
| Web pages, documents, window titles, OCR text | Untrusted data. They reach the planner only as observations, never as instructions to the core. |
| Python worker | Trusted executor running with the current user's rights. It only executes calls the core authorised. |
| Gateway backends (gemini-web2api-go, WebAI-to-API) | Third-party code run as separate local processes, bound to `127.0.0.1`. |

## Privileges and UAC

- The agent runs with the rights of the current Windows user. **No mode elevates.** `full_access` means "everything the current user may do", not Administrator.
- Shell commands that request elevation (`-Verb RunAs`), change UAC settings, or use known UAC-bypass binaries are **denied in every mode** (`policy/default.toml`, `[shell] deny`).
- When an action fails because it needs Administrator rights, the worker returns `ELEVATION_REQUIRED` with a structured `escalation_request` (`kind`, `tool`, `command`, `reason`, `how_to_grant`). The task stores it, and the CLI prints it. The user decides; the agent never retries around it.

## Execution modes

| Mode | Shell | Writes outside the normal roots | AutoCAD destructive ops | Browser uploads |
|---|---|---|---|---|
| `normal` (default) | confirm every command | confirm | confirm | confirm |
| `trusted` | allowed; risky patterns (recursive delete, downloads, services, scheduled tasks, process kills, installs, firewall) confirm | allowed (system locations confirm) | confirm | allowed |
| `full_access` | allowed except the always-denied patterns | allowed | allowed | allowed |

In every mode, these are denied:
- **Credential stores and secrets:** Windows Credentials/Vault/DPAPI folders, browser profiles (Chrome, Edge, Brave, Firefox), `.ssh`, `.aws`, gcloud, this project's `.env`, the gateway runtime directory with its account database, and the automation Chrome profile.
- **System hives.**
- **Shell commands** that touch credentials (`cmdkey`, `vaultcmd`, DPAPI, cookie/Login Data databases), disable Defender, or edit HKLM.

Confirmation is interactive in the CLI. `--yes` approves "confirm" decisions but never overrides "deny". Without a terminal and without `--yes`, confirmations are declined.

## Secrets

- Secrets never appear in TOML, code or git. Config keys ending in `_secret` only *name* a secret. It is resolved from, in order: the process environment, the repo `.env` (git-ignored), then Windows Credential Manager (generic credential `local-gemini-agent/<name>`).
- `scripts/setup-gateway.ps1` generates the three local service keys (gateway API key, primary API key, primary admin token) into `.env`. These protect the loopback services. They are not Google credentials.
- **Google sign-in** for the Gemini backends is a user action (see [operations.md](operations.md)). The project contains no code that reads browser cookies:
  - gemini-web2api-go receives the cookie only through its own admin UI;
  - WebAI-to-API's automatic host-browser cookie scraping is disabled (`[Browser] name = none`, visible in its log as "Unsupported browser: none");
  - its sign-in happens in its own Playwright browser via `verify_login.py`.
- The backends store their session material inside `runtime/gateway/` (git-ignored, and denied to the agent's own tools by policy).

## Logging and redaction

- All logs (core audit, worker, gateway, backend output) pass through a redactor:
  - known secret values (resolved keys) are replaced literally;
  - patterns are replaced too: Google session cookies (`__Secure-*PSID*`, `SID`, `HSID`, …), `Cookie:` headers, `Authorization`/Bearer tokens, `api key`/`token`/`password` assignments, `sk-…`, `AIza…`, `ghp_…`.
- The gateway never logs request bodies. The primary runs with `log_requests = false`. Its startup banner's partial key is redacted.
- The redactor is tested in Python (`test_foundation.py`) and Rust (`redact.rs`).
- Task memory and persistent memory are redacted before they are written.

## Network exposure

- **Bound to `127.0.0.1` only:** the gateway (8790), gemini-web2api-go (8083), WebAI-to-API (6969) and Chrome's DevTools port. The gateway refuses a non-loopback bind address.
- **API-key protection:** the gateway's `/v1/*` endpoints require the gateway API key. The primary requires its own API key, and its admin UI requires the admin token. WebAI-to-API has no authentication of its own (upstream limitation), so it is reachable only on loopback.
- **No proxies on loopback:** loopback clients (Rust `reqwest`, Python `urllib`) explicitly disable proxies.
- **Telemetry off:** browser-use telemetry, version checks and extension downloads are disabled. Windows-Use's telemetry is never imported (import shim).

## Browser

The agent uses a **dedicated Chrome profile** under `runtime/chrome-profile` (git-ignored). It never opens, copies or reads the user's default profile. Attaching to a Chrome the user started with remote debugging is opt-in (`[browser] cdp_url`).

## Supply chain

- Every upstream project is pinned to a commit (`references/manifest.toml`).
- Python dependencies are locked (`uv.lock`) and Rust dependencies are locked (`Cargo.lock`).
- The Go toolchain was downloaded from go.dev and checked against its published SHA-256.
- Windows-Use is installed from the pinned commit with its dependency list trimmed, so its 39 transitive SDKs are never installed.

## Always-confirm actions (all modes, including full_access)

Some actions cannot be undone or send data off the machine. For these the core asks a person, per action, whatever the
mode and whatever the model says (`core/rust-agent/src/guard.rs`):

| Class | Fires for | Does not fire for |
|---|---|---|
| `delete_recursive`, `delete_bulk`, `delete_outside_scratch` | recursive / wildcard / pipeline / single deletions by shell or python whose targets are not strictly inside a scratch area (`%TEMP%`, `C:\temp`, `artifacts/`; links are resolved), or whose target cannot be determined | deleting inside scratch areas |
| `network_send` | upload/POST/PUT, `-InFile`, `-F`, `@file`, `scp/ftp`, pipelines of file content into web tools, python `requests.post`/`smtplib`/…, `browser.upload`, `office.outlook_send` | public GET / downloads |
| `system_power` | shutdown, restart, log off | |
| `system_change` | services, registry, scheduled tasks (incl. `schedule.create`), firewall, installs, permission changes, process kills | |
| `system_dir_write` | writes under Windows / Program Files / ProgramData (tool or shell text) | reads |
| `file_scheme` | browser navigation to `file:` | |
| `opaque_command` | encoded, evaluated or dynamically assembled code that cannot be classified | |

How consent works: it comes only from `Interaction::confirm_always` (a person pressing the button in the chat window or
answering at a terminal). `--yes`, an unattended run, a timeout (10 min) or a missing answer all mean *declined*. An
approval is bound to the exact call (tool + every argument, SHA-256) and is single-use and short-lived, so a changed
destination, path or text asks again. Text or arguments that claim approval are just text. The files of the approval
channel (`*.request.json`, `*.response.json`) are denied to every tool.

Limits, stated plainly: this is pattern classification of the command text. Obfuscation (string assembly, encoded
payloads) is only caught by the `opaque_command` rule where the construct is recognisable, and a regex list is never a
boundary against a determined author of the command; the defence is that a person sees the action. The approval-channel
protection is a path/text deny, not an authenticated channel. The policy is enforced in the Rust core only; the Python
worker has no policy of its own (it is reachable only through the core's pipe).
