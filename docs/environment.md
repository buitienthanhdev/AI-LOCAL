# Environment audit (Phase 0)

Audited: 2026-10-08, on the developer workstation the agent will run on.
Method: read-only probes (`Get-CimInstance`, `Get-Command`, `--version`, registry reads, `vswhere`, `nvidia-smi`).
No system configuration was changed during the audit.

## Summary

| Area | Status | Detail |
|---|---|---|
| OS | PASS | Windows 11 Pro 10.0.26200 (build 26200), x64 |
| CPU / RAM | PASS | Intel Core Ultra 7 265K, 20 cores; 63.4 GB RAM |
| Disk (C:) | PASS | ~90 GB free |
| Shell | PASS | PowerShell 7.6.6; Git Bash available; ExecutionPolicy LocalMachine=RemoteSigned, Process=Bypass |
| Privileges | INFO | Current session is **not** elevated (not Administrator). The agent must never self-elevate (see `docs/security.md`). |
| Long paths | PASS | `LongPathsEnabled = 1` |
| Python | PASS | CPython 3.14.6 (system default, `C:\Python314`), plus uv-managed CPython 3.12.13 and 3.11.15 |
| uv | PASS | uv 0.12.0 — chosen Python project/dependency manager |
| poetry | N/A | not installed, not needed |
| pip | PASS | pip 26.2.1 (Python 3.14) |
| Rust | PASS | rustc / cargo 1.99.0, toolchain `stable-x86_64-pc-windows-msvc` via rustup in `%USERPROFILE%\.cargo\bin`. **Note:** that directory was not on the PATH of the non-interactive shell; scripts call cargo through `scripts/env.ps1` which prepends it. |
| MSVC linker | PASS | Visual Studio Build Tools 2022 and Build Tools 18 present (VC x86/x64 tools), Windows 10/11 SDK present |
| Go | **MISSING** | Not installed. Needed only to build `gemini-web2api-go` from source. See "Go" below. |
| Node / npm | PASS | Node v24.18.1, npm 11.16.0. pnpm not installed (only needed if building UI-TARS-desktop, which we do not). |
| Git | PASS | git 2.56.0.windows.1. `gh` CLI not installed. |
| Docker | N/A | not installed; by design nothing in the Windows control path is containerised |
| Chrome | PASS | Chrome 152.0.7977.84 at `C:\Program Files\Google\Chrome\Application\chrome.exe` (also registered under `App Paths`) |
| Edge | INFO | Edge 154.0.4258.62 present (possible future browser fallback) |
| AutoCAD | PASS | AutoCAD 2023 (release R24.2, product key `ACAD-6101:409`), `acad.exe` at `D:\Autocad 2023\AutoCAD 2023\acad.exe` (version R24.2.53.0.0). COM ProgID `AutoCAD.Application` is registered. Install location is discovered from `HKLM\SOFTWARE\Autodesk\AutoCAD\<release>\<product>\AcadLocation`, never hardcoded. |
| GPU | WARN | NVIDIA GeForce RTX 5060 Ti, 16 GB VRAM, compute capability 12.0, driver 616.56. At audit time **13.3 GB of 16 GB was already in use** (Ollama models loaded by another project). Local vision models must check free VRAM before loading. Also present: Intel integrated graphics and a Parsec virtual display adapter (multi-display / DPI handling must not assume a single monitor). |
| Ollama | INFO | Ollama 0.34.1 with gemma4:12b, gemma3:12b, qwen3:14b/32b, qwen2.5:3b and embedding models. Candidate for the future local-model fallback; not a hard dependency. |

## Working directory

`C:\Users\TIEN THANH\Desktop\AI LOCAL` already contains an unrelated project ("Company Brain": `brain/`, `rust/`, `data/`, `README.md`, ...) and is not a git repository.
To avoid mixing the two, this agent lives in its own git repository:

```
C:\Users\TIEN THANH\Desktop\AI LOCAL\local-gemini-agent\
```

Note that the user profile path contains a space (`C:\Users\TIEN THANH`). Every script quotes paths, and Python/Rust code uses path APIs rather than string concatenation.

## Go

Go is required only to build the primary Gemini Web gateway (`references/gemini-web2api-go`) from source.
Decision: install a portable, user-scoped Go toolchain under `%LOCALAPPDATA%\Programs\go` (no machine-wide installer and no PATH change; `scripts/env.ps1` adds it per process). If the upstream project publishes Windows release binaries, `scripts/setup-gateway.ps1` can use them instead.

## Python version policy

The Python services use **CPython 3.12** (uv-managed) rather than the system 3.14, because several native dependencies of Windows-Use / browser-use / pywin32 publish wheels for 3.12 first. The system interpreter is left untouched; `uv` creates per-service virtual environments.

## Reproducing this audit

`scripts/audit-environment.ps1` reruns all of the probes above and prints the same table, so this document can be regenerated after machine changes.
