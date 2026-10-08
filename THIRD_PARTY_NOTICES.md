# Third-party notices

This project integrates or adapts code from the projects below. Full license texts are in
`third_party/licenses/`. Pinned commits are listed in `references/manifest.toml`.

| Project | License | How it is used | Files in this repo |
|---|---|---|---|
| Windows-Use (© 2025 CursorTouch) | MIT | Runtime dependency (pinned git commit), imported through an adapter. Not modified. | `services/python-agent/lga_worker/windows/` (adapter only) |
| Python-UIAutomation-for-Windows (yinkaisheng), vendored inside Windows-Use as `windows_use/uia` | Apache-2.0 | Transitively, through Windows-Use. Not modified. | none |
| browser-use (© 2024 Gregor Zunic) | MIT | Runtime dependency (pinned version). Not modified. | `services/python-agent/lga_worker/browser/` (adapter only) |
| UI-TARS (© 2025 Bytedance Ltd. and/or its affiliates) | Apache-2.0 | Action parser and `smart_resize` adapted; modifications are marked in the file header. | `services/python-agent/lga_worker/vision/ui_tars_parser.py` |
| AutoCAD-MCP (© 2024 AutoCAD MCP Server Contributors) | MIT | COM patterns adapted: STA executor with call-rejected retry, VARIANT point arrays, entity read-back and verify-by-handle, plot layout save/restore. | `services/python-agent/lga_worker/autocad/` (attribution in each adapted file header) |
| gemini-web2api-go (© 2026 gemini-web2api-go contributors) | MIT | Built from pinned source and run as a separate local process. Not modified. | none (binary built into the git-ignored runtime dir) |
| WebAI-to-API (© 2026 Mohammad) | MIT | Run from the pinned source as a separate local process. Not modified. | none |
| UI-TARS-desktop (Bytedance) | Apache-2.0 | Architecture reference only; no code copied. | none |

## Other runtime dependencies (installed from PyPI / crates.io, not redistributed)

All are locked in `services/*/uv.lock`, `apps/agent-ui/uv.lock` and `Cargo.lock`. Notable ones:

| Package | License | Used by |
|---|---|---|
| pywin32, comtypes | PSF / MIT | worker (Win32, COM, UI Automation) |
| PyWinRT (`winrt-*`) | MIT | worker (`vision.ocr`, Windows.Media.Ocr) |
| rapidfuzz | MIT | worker (fuzzy app-name matching) |
| aiohttp, jsonschema, Pillow, psutil | Apache-2.0 / MIT / HPND / BSD | worker, gateway, UI |
| fuzzywuzzy | GPL-2.0 | **transitive runtime dependency of Windows-Use only** (imported by `windows_use.agent.desktop.service`); installed from PyPI, not redistributed, and not imported by this project's own code |
| tokio, reqwest, serde, jsonschema (Rust), rusqlite (bundled SQLite), clap, regex | MIT / Apache-2.0 / public domain (SQLite) | Rust core and CLI |
