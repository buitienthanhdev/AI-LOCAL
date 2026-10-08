# Repository inventory (Phase 1)

All upstream projects are cloned read-only into `references/<name>` at the commits pinned in
`references/manifest.toml` (verify with `pwsh scripts/fetch-references.ps1`). Audit date: 2026-10-08.
Line references below point into those pinned trees.

## Summary table

| repo | commit | license | language | entry point | role here | integration mode |
|---|---|---|---|---|---|---|
| [Jeomon/Windows-Use](https://github.com/Jeomon/Windows-Use) | `582a566` (v0.8.1) | MIT (+ vendored `uia/` is Apache-2.0, from yinkaisheng/Python-UIAutomation-for-Windows) | Python ≥3.10 | `windows_use.Agent`, CLI `windows-use`; primitives in `windows_use.agent.desktop.service.Desktop` and `windows_use.uia` | Windows execution layer (first `WindowsControlProvider` implementation) | **Dependency** (pinned git commit), with trimmed dependency metadata and an import shim; no fork |
| [zexadev/gemini-web2api-go](https://github.com/zexadev/gemini-web2api-go) | `bc09c71` (v4.20.2) | MIT | Go 1.26.8 | `main.go` → `internal/app.Run()` | Primary Gemini Web backend | **Separate process**, built from the pinned source; called over its OpenAI-compatible HTTP API |
| [Amm1rr/WebAI-to-API](https://github.com/Amm1rr/WebAI-to-API) | `dd85ae0` (v0.7.1) | MIT | Python ≥3.11,<3.13 | `src/run.py` (FastAPI/uvicorn) | Fallback Gemini Web backend | **Separate process** in its own uv venv; called over HTTP |
| [bytedance/UI-TARS](https://github.com/bytedance/UI-TARS) | `582f3a7` | Apache-2.0 | Python ≥3.10 (no runtime deps) | `codes/ui_tars/action_parser.py` | Grounding output parser + prompt formats for `VisionProvider` | **Vendored & modified** (~150 lines) with Apache header + modification notice |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | `c75e847` (v0.13.11) | MIT | Python ≥3.11 | `browser_use.browser.BrowserSession` | Browser runtime for `BrowserProvider` | **Dependency** (pinned version); event-bus primitives only, never its LLM `Agent` or `Tools` |
| [beiming183-cloud/AutoCAD-MCP](https://github.com/beiming183-cloud/AutoCAD-MCP) | `11f7c47` (v4.0.0) | MIT | Python ≥3.10 | `autocad_mcp.server:main` (FastMCP stdio) | AutoCAD COM patterns | **Reference + small vendored pieces** with MIT notice; our own thin COM layer |
| [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) | `20b2dfe` | Apache-2.0 | TypeScript monorepo | `packages/ui-tars/sdk/src/GUIAgent.ts` | Agent-loop architecture reference | **Reference only** — no code copied |

## Per-repository detail

### Windows-Use — `references/windows-use`

- **License.** MIT, © 2025 CursorTouch. `windows_use/uia/*` is a modified copy of Python-UIAutomation-for-Windows (Apache-2.0, header in `uia/core.py:1-12`); attribution is kept in `THIRD_PARTY_NOTICES.md`.
- **Architecture.** `Desktop` (`agent/desktop/service.py`) holds every OS primitive. `agent/tree/` turns the UIA tree into a flat list of interactive `TreeElementNode`s (bounding box, center, name, control type, live `uia.Control`, metadata). `uia/` is comtypes over `UIAutomationCore.dll`. `agent/service.py` + `providers/*` is the LLM loop; we do not use it.
- **Useful modules.**
  - `Desktop`: `launch_app`, `get_windows`, `get_active_window`, `bring_window_to_top`, `click`, `move`, `drag`, `scroll`, `type`, `shortcut`, `get_screenshot`, `get_state`, `execute_command`.
  - `uia`: `Click`, `DoubleClick`, `RightClick`, `MoveTo`, `DragDrop`, `SendKeys`, `ControlFromHandle`, `ControlFromPoint`, Value/LegacyIAccessible/Text/Window patterns.
  - `agent/tree`: cached UIA traversal.
- **Dependencies.** 39 declared runtime deps, including all major LLM SDKs, Discord/Telegram/Slack, ElevenLabs, `datasets`. `levenshtein`/`multiprocess` lack cp314 wheels. The primitive layer only imports pywin32, comtypes, pillow, psutil, fuzzywuzzy, markdownify, tabulate, requests.
- **Integration plan.**
  1. Install the pinned commit into the Python 3.12 worker venv, overriding its dependency metadata so only the primitive layer's dependencies are installed (`[tool.uv.dependency-metadata]`).
  2. Import `windows_use.agent.desktop.service` through `services/python-agent/lga_worker/windows/_upstream.py`. It pre-registers the parent packages so `windows_use/__init__.py` (which loads every LLM SDK, PostHog telemetry and `load_dotenv()` from the CWD) never runs.
  3. `WindowsUseProvider` adapts `Desktop` to `WindowsControlProvider`. Primitives Windows-Use lacks are added in our adapter: `close_app`, `find_element`, `read_element`, `write_element`, filesystem ops and a stricter PowerShell runner.
  4. All calls run on one dedicated STA thread.
- **Known limitations** (and how the adapter handles each).
  - Importing `uia.core` switches the process to Per-Monitor-V2 DPI awareness, process-wide. Coordinates are physical pixels; we rely on that and document it.
  - `Desktop.click` defaults to `clicks=2`. The adapter always passes the count explicitly.
  - `launch_app` returns a 4-tuple on one path (`service.py:307`). The adapter normalises the return value.
  - `execute_command` uses Windows PowerShell 5.1, drops stderr when stdout is non-empty, has a 10 s timeout and doesn't kill grandchildren. The adapter uses its own runner with a Job Object, separate stdout/stderr and a configurable timeout.
  - `get_windows` drops windows that cannot both minimise and maximise (most dialogs). The adapter enumerates with `win32gui.EnumWindows`.
  - Elements on monitors left of or above the primary get a zero box. The adapter filters elements whose box is empty.
  - The ScrollElementNode kwargs mismatch means scrollable nodes are always empty.
  - The tree traversal is recursive.
  - Virtual-desktop COM IIDs are per build. Build 26200 is unverified; it degrades to "current desktop".

### gemini-web2api-go — `references/gemini-web2api-go`

- **License.** MIT, © 2026 gemini-web2api-go contributors.
- **Architecture.** `internal/app`: `server.go` (handlers), `gemini.go` (model table, StreamGenerate), `messages.go` (OpenAI messages → one prompt; tool calls parsed from a ```` ```tool_call ```` block), `vision.go`/`upload.go`, `cookie_pool.go`/`rotate.go` (1PSIDTS rotation every 10 min), SQLite via modernc (no CGO).
- **API.**
  - `GET /`: unauthenticated liveness, `{status, version, models}`.
  - `GET /v1/models`
  - `POST /v1/chat/completions`: OpenAI shape, SSE streaming with `[DONE]`; images as `image_url` data URLs ≤12 MB, which need a cookie.
  - `POST /v1/responses`
  - `/admin/api/*`
  - Local auth is `Authorization: Bearer <API_KEY>`.
- **Auth to Gemini.** Anonymous mode for flash models. A user-supplied cookie (admin UI or `--cookie-file`) is required for image input and Pro models. There is no browser scraping code. Cookies are stored in plaintext in its SQLite DB, which lives in our git-ignored state dir.
- **Dependencies.** tls-client/fhttp, modernc sqlite, tiktoken-go.
- **Integration plan.**
  1. `scripts/setup-gateway.ps1` builds the pinned source with a user-scoped Go 1.26.
  2. The gateway supervisor launches it with `--config` (bound to `127.0.0.1`), `--db`, `--port`, `--admin-token`, and env `API_KEY`. The key comes from the secret store and is never logged.
  3. Health is `GET /`; models are listed with `GET /v1/models`.
- **Known limitations.**
  - It binds `0.0.0.0` by default, so our config forces `127.0.0.1`.
  - The admin API is open if the admin token is empty, so we always set one.
  - Google `/sorry` rate-blocks after roughly 80–180 burst requests per IP.
  - Sampling params are ignored.
  - Prompt cap of 128 000 bytes.
  - No graceful shutdown; it is terminated.
  - Requests with tools are buffered rather than streamed.

### WebAI-to-API — `references/webai-to-api`

- **License.** MIT, © 2026 Mohammad.
- **Architecture.** FastAPI app (`src/app/main.py`), `services/providers/gemini/*` (gemini-webapi 2.1.1 backend + Playwright backend), `utils/browser.py` (browser cookie extraction), SQLite conversation snapshots.
- **API.**
  - `POST /v1/stateless/chat/completions`: the client owns the history.
  - `GET /v1/stateless/models`
  - `GET /health`: 200, no upstream call.
  - `GET /v1/auth/status`
  - Images must be `{"type":"file","file":{"filename","file_data":"data:..."}}`; `image_url` returns 422.
  - `temperature`/`top_p`/`top_k`/`response_format`/`tool_choice` return 400.
- **Auth to Gemini.** Sources, in order: `config.conf [Gemini]` → `runtime/auth/gemini.json` (written by `verify_login.py` after an interactive login) → **automatic host-browser cookie scraping** (browser_cookie3 and DPAPI decryption). That last path violates our policy. It is disabled by `[Browser] name = none`, and a test asserts it stays disabled.
- **Dependencies.** fastapi, uvicorn, gemini-webapi, curl-cffi, playwright, browser-cookie3, pywin32. Python <3.13, so it gets its own uv 3.12 venv.
- **Integration plan.**
  1. Run it from a private working directory with a generated `config.conf`:
     - `[General] check_updates=false`
     - `[Browser] name=none`
     - `[Gemini] backend=webapi`
     - no cookies in the file
  2. Point `RUNTIME_DIR`/`AUTH_STATE_DIR` at the git-ignored state directory.
  3. The user performs a one-time `verify_login.py`.
  4. The adapter strips unsupported params, converts `image_url` parts to `file` parts, and treats an SSE stream without `[DONE]` as a failure.
- **Known limitations.**
  - No local API authentication and CORS `*`, so it is bound to 127.0.0.1 and treated as low-trust.
  - No 1PSIDTS rotation on the webapi backend.
  - Only one tool call per response.
  - Text/file ordering is not preserved.

### UI-TARS — `references/ui-tars`

- **License.** Apache-2.0, © 2025 Bytedance Ltd. Model weights carry their own Hugging Face license (not redistributed here).
- **Useful modules.** `codes/ui_tars/action_parser.py`:
  - `smart_resize` (L115-143)
  - `parse_action_to_structure_output` (L146-276)
  - box/point conversion

  `codes/ui_tars/prompt.py`: `COMPUTER_USE_DOUBAO`, `GROUNDING_DOUBAO`.
- **Coordinate semantics.** UI-TARS-1.5 (`qwen25vl`) emits absolute pixels in smart-resized image space. UI-TARS-1.0 emits relative 0–1000.
- **Integration plan.** Vendor the parser into `services/python-agent/lga_worker/vision/ui_tars_parser.py`, keeping the Apache header plus a modification notice. Changes:
  - `ast.literal_eval` instead of `eval`
  - no `assert`/`print`
  - fix the `ast.Str` removal in Python 3.14
  - typed results
  - left/right scroll

  `parsing_response_to_pyautogui_code` is **not** used, because execution belongs to our tool router.
- **Known limitations.**
  - Official GGUFs were withdrawn because quantization hurt accuracy; there is no official Ollama build.
  - UI-TARS-1.5-7B needs about 7–9 GB VRAM quantised. Only about 3–5 GB is free on this machine while other Ollama models are loaded.
  - A local UI-TARS backend is therefore optional; the default vision backend is Gemini Web with image input.

### browser-use — `references/browser-use`

- **License.** MIT, © 2024 Gregor Zunic.
- **Architecture.**
  - `BrowserSession` (`browser/session.py`) is built on `cdp-use` (pure CDP; Playwright is not required) and a `bubus` event bus.
  - Watchdogs handle actions, DOM, screenshots and downloads.
  - `DomService` merges DOM, snapshot and AX tree into `selector_map: {backend_node_id: EnhancedDOMTreeNode}`.
- **Useful primitives.**
  - `navigate_to`, `get_tabs`
  - events: `SwitchTabEvent`, `CloseTabEvent`, `ClickElementEvent`, `ClickCoordinateEvent`, `TypeTextEvent`, `UploadFileEvent`, `ScrollEvent`, `SendKeysEvent`
  - `get_browser_state_summary`, `take_screenshot`, `FileDownloadedEvent`
- **Dependencies.** Heavy: all LLM SDKs, google-api-client, posthog, reportlab, etc. Top-level imports are lazy.
- **Integration plan.**
  1. `ChromeProvider` launches Chrome itself: detected path, a **dedicated** `--user-data-dir` under the agent state dir, `--remote-debugging-port=0`, bound to 127.0.0.1, port read from `DevToolsActivePort`.
  2. It attaches with `BrowserSession(cdp_url=..., keep_alive=True, highlight_elements=False, enable_default_extensions=False)`. This avoids browser-use copying profiles (and their cookies) into `%TEMP%` and leaving them there.
  3. Telemetry is off: `ANONYMIZED_TELEMETRY=false`, `BROWSER_USE_VERSION_CHECK=false`, `BROWSER_USE_DISABLE_EXTENSIONS=1`, set before import.
  4. We dispatch events directly and never use `Tools`, which instantiates telemetry and swallows exceptions.
  5. The user's *default* Chrome profile is never attached to automatically (Chrome 136+ forbids remote debugging there). Attaching to an already-running Chrome is supported only when the user starts it with debugging enabled and passes `browser.cdp_url`.
- **Known limitations.**
  - It reconnects to the same CDP URL only (3 attempts); there is no relaunch. Our provider detects `is_cdp_connected == False`, relaunches Chrome, and invalidates indices.
  - The PopupsWatchdog auto-accepts dialogs.
  - The upload event has no path allowlist, so our policy layer enforces one.
  - Indices are valid only against the latest state.

### AutoCAD-MCP — `references/autocad-mcp`

- **License.** MIT, © 2024 AutoCAD MCP Server Contributors (a fork of puran-water/autocad-mcp, also MIT).
- **Architecture.** A 2 200-line FastMCP server (12 multi-operation tools). `backends/file_ipc.py` (5 500 lines) uses COM first and falls back to a LISP dispatcher via `SendCommand` and WM_CHAR posting.
  - `com_sta.py` provides an STA executor with RPC_E_CALL_REJECTED retry.
  - `_com_entity_to_dict` + `verify_created_entity` provide read-back verification.
  - The optional .NET plugin targets AutoCAD 2025 only.
- **Integration plan.** Do **not** depend on it:
  - It doesn't fit our tool contract.
  - Its preferred backend can't load on AutoCAD 2023.
  - Its LISP/keystroke fallback conflicts with "structured operations over GUI".
  - It hardcodes `C:/temp` and AutoCAD 2025 registry paths.

  We write `services/python-agent/lga_worker/autocad/` as a thin COM layer, adapting these pieces under its MIT notice:
  - STA executor and retry-on-call-rejected for idempotent calls (`com_sta.py`)
  - VARIANT point arrays (`_solid_point`)
  - entity read-back fields (`_com_entity_to_dict`)
  - verify-by-handle (`backends/base.py:635-698`)
  - PlotToFile layout save/restore

  LISP and keystroke fallbacks are deliberately not ported: when COM fails, the tool fails closed.
- **Known limitations of upstream.** 61 `except Exception: pass` blocks. The sync STA wait blocks its event loop. The create-then-LISP fallback can duplicate entities.

### UI-TARS-desktop (Agent TARS) — `references/agent-tars`

- **License.** Apache-2.0.
- **Used as architecture reference only.**
  - `GUIAgent` loop (`packages/ui-tars/sdk/src/GUIAgent.ts:130-433`).
  - Status enum `init/running/pause/end/call_user/user_stopped/error`.
  - Numeric error classes (screenshot / invoke / execute retry, max loop, environment).
  - Separate retry budgets per stage.
  - An invalid screenshot does not consume loop budget.
  - Sliding window of the last 5 images in the model context.
  - Canonical action names (`multimodal/gui-agent/shared/src/types/actions.ts`).

  These ideas shaped `core/rust-agent` (state machine, retry budgets) and `docs/tool-contract.md` (action names). No code was copied.
- **Explicitly not reused.**
  - The remote operators and the ByteDance free-model proxy.
  - UTIO telemetry.
  - The NutJS executor, which lacks left/right scroll and has a falsy-zero coordinate bug.
  - Doubao-specific prompts.
  - Silent parse-failure handling.

## Dependency graph

```
apps/agent-cli (Rust) ──► core/rust-agent (Rust: lifecycle, planner loop, policy, router, audit, memory index)
                                │  JSON-lines over stdio (request_id, task_id, tool, args, deadline)
                                ▼
                    services/python-agent (Python 3.12 worker)
                    ├── windows/   ──► windows-use @582a566 (trimmed deps, import shim) ──► UIAutomationCore, pywin32
                    ├── browser/   ──► browser-use 0.13.11 (CDP only) ──► Chrome 152 (dedicated profile)
                    ├── vision/    ──► vendored UI-TARS parser ──► ChatProvider (Gemini image input) | optional local UI-TARS
                    ├── autocad/   ──► pywin32 COM ──► AutoCAD 2023 (R24.2)
                    ├── shell/, filesystem/ (own implementations)
                    └── gemini gateway client (ChatProvider)
                                │  HTTP 127.0.0.1
                                ▼
                    services/gemini-gateway (supervisor + circuit breaker)
                    ├── primary:  gemini-web2api-go.exe @2158ea0  (Go 1.26)
                    └── fallback: WebAI-to-API @dd85ae0 (own uv venv, Python 3.12)
```

## Licensing conclusion

Every upstream is MIT or Apache-2.0, and both are compatible with this project. Our own code is MIT-licensed.
Obligations are tracked in `THIRD_PARTY_NOTICES.md`:
- keep the copyright and permission notices for MIT sources we adapt (Windows-Use, AutoCAD-MCP);
- keep the Apache-2.0 headers, add modification notices, and include the license text for vendored UI-TARS code and Windows-Use's `uia/`.
