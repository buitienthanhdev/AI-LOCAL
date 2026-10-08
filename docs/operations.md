# Operations

## First-time setup

```powershell
cd "C:\Users\TIEN THANH\Desktop\AI LOCAL\local-gemini-agent"
pwsh scripts/fetch-references.ps1 -Checkout     # pinned upstream sources (needed to build the gateways)
cd services/python-agent; uv sync; cd ../..      # worker venv (Python 3.12)
pwsh scripts/setup-gateway.ps1                   # build gemini-web2api-go, fallback venv, local keys in .env
cargo build --release                            # target/release/agent.exe
target/release/agent.exe doctor
```

Requirements:
- Windows 10/11, uv, Rust (MSVC), Go ≥ 1.26 (for the primary gateway build), Chrome.
- AutoCAD (only for `autocad.*` tools).

See [environment.md](environment.md) for the audited machine.

## Connecting Gemini accounts

Without an account, the primary backend runs **anonymously**:
- text planning with `gemini-3.6-flash` works;
- image input (needed by `vision.locate` / `vision.describe` and by screenshots sent to the planner) does not.

To enable images, use either option.

**Primary (recommended).** Open `http://127.0.0.1:8083/admin` while the gateway runs. Log in with the admin token `LGA_PRIMARY_ADMIN_TOKEN` from `.env`, then add your Google cookie in *Cookie pool* (Firefox export recommended upstream).

**Fallback.** Run its interactive sign-in once:

```powershell
cd runtime\gateway\webai-to-api
$env:RUNTIME_DIR = "$PWD\runtime"; $env:AUTH_STATE_DIR = "$PWD\runtime\auth"
.\venv\Scripts\python.exe ..\..\..\references\webai-to-api\verify_login.py
```

This opens a Playwright browser for you to sign in. The session is stored under `runtime/` (git-ignored).

The agent never reads your browser's cookies itself.

## Everyday use

```powershell
agent run "Mở Notepad, viết hello world và lưu vào C:\temp\hello.txt"
agent run "Mở Chrome và tìm tài liệu về AutoCAD 2026"
agent run "Mở AutoCAD, tạo bản vẽ mới và vẽ một hình tròn bán kính 100" --mode trusted
agent status                 # active tasks and the latest finished one
agent status <TASK_ID>       # every step with its verification
agent cancel <TASK_ID>       # stop after the current action (from another terminal)
agent resume <TASK_ID>       # continue an interrupted / paused / failed task
agent history --limit 50
agent tools                  # the tool contract the planner sees
agent models                 # models offered by the gateway backends
agent screenshot             # artifacts/screenshots/...
agent gateway start|stop|status
agent doctor                 # PASS / WARN / FAIL per subsystem
```

Exit codes of `agent run` / `agent resume`:

| Code | Meaning |
|---|---|
| 0 | completed (all completion checks passed) |
| 1 | failed |
| 3 | waiting for the user (`task.ask_user` with no terminal) |
| 130 | cancelled |

## Modes and confirmations

`--mode normal|trusted|full_access` overrides `[agent] mode`. In `normal`, shell commands and other "confirm" actions prompt `Allow? [y/N]`. `--yes` approves prompts (never denials). See [security.md](security.md).

## Where things are

| What | Where |
|---|---|
| Task state (resumable) | `memory/tasks/<id>.json` (+ `.bak`) |
| Persistent memory | `memory/persistent.db` |
| Audit trail per task | `logs/tasks/<id>.jsonl`: task_id, step_id, timestamp, provider, tool, input hash, result, duration, error, retry count, verification result |
| Worker log | `logs/worker/<date>.jsonl`, `logs/worker/stderr.log` |
| Gateway log | `logs/gateway/<date>.jsonl`, `primary.log`, `fallback.log` |
| Screenshots | `artifacts/screenshots/<task_id>/` |
| Other artifacts (exports, downloads) | `artifacts/<task_id>/`, `artifacts/downloads/` |
| Runtime (binaries, venvs, Chrome profile, backend DBs) | `runtime/` |

## Tests

```powershell
cargo test --workspace                                   # Rust core
cd services/python-agent;  uv run pytest -q              # worker unit + protocol tests
uv run pytest -m desktop --run-desktop -v                # real desktop (Notepad, Character Map, OCR click)
uv run pytest -m browser --run-browser -v                # real Chrome (TEST 2, TEST 7)
uv run pytest -m autocad --run-autocad -v                # real AutoCAD (TEST 5)
cd ../gemini-gateway; uv run pytest -q                   # gateway routing
uv run pytest -m gateway --run-gateway -s                # TEST 6: kill primary, verify failover
pwsh tests/e2e/run-e2e.ps1 -Test all                     # full agent runs with Gemini (TEST 1, 4, 8, acceptance)
```

Desktop tests move the mouse and open windows. Do not use the PC while they run.

## Troubleshooting

| Symptom | Check |
|---|---|
| `gemini-gateway FAIL not running` | `agent gateway start`; then `logs/gateway/*.jsonl` |
| `primary=degraded ... anonymous mode` | normal without an account; add a cookie for image input |
| `fallback=degraded ... sign-in required` | run its `verify_login.py` (see above) |
| `python-worker FAIL` | `logs/worker/stderr.log`; `uv sync` in services/python-agent |
| Task stuck in `needs_user` | `agent status <id>` shows the question; `agent resume <id>` in a terminal |
| UI clicks land wrong on a multi-monitor setup | coordinates are physical pixels of the virtual desktop; check `system.environment` (screen layout) |
