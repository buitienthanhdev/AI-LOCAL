# Open the unified agent's desktop chat window (pywebview / WebView2) — the one chat for everything.
# Requires `cargo build` (the agent CLI) and `uv sync` in apps/agent-ui.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$ui = Join-Path $env:LGA_ROOT 'apps\agent-ui'
$python = Join-Path $ui '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { Push-Location $ui; try { uv sync } finally { Pop-Location }; $python = Join-Path $ui '.venv\Scripts\pythonw.exe' }
Start-Process -FilePath $python -ArgumentList '-m', 'lga_ui.desktop' -WorkingDirectory $env:LGA_ROOT
Write-Host 'Agent chat window opening...'
