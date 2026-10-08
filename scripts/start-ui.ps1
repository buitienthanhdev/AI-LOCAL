# Start the local web UI (apps/agent-ui) in the foreground. Requires `cargo build` and `uv sync` in apps/agent-ui.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$ui = Join-Path $env:LGA_ROOT 'apps\agent-ui'
if (-not (Test-Path (Join-Path $ui '.venv\Scripts\python.exe'))) { Push-Location $ui; try { uv sync } finally { Pop-Location } }
& (Join-Path $ui '.venv\Scripts\python.exe') -m lga_ui
exit $LASTEXITCODE
