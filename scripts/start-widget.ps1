# Start the floating popup: a small always-on-top button to give the agent tasks and watch it work.
#   pwsh scripts/start-widget.ps1
# Requires `cargo build` (the agent CLI) and `uv sync` in apps/agent-ui.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$ui = Join-Path $env:LGA_ROOT 'apps\agent-ui'
$python = Join-Path $ui '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { $python = Join-Path $ui '.venv\Scripts\python.exe' }
if (-not (Test-Path $python)) { Push-Location $ui; try { uv sync } finally { Pop-Location }; $python = Join-Path $ui '.venv\Scripts\pythonw.exe' }
Start-Process -FilePath $python -ArgumentList '-m', 'lga_ui.widget' -WorkingDirectory $env:LGA_ROOT
Write-Host 'Agent popup started (look for the floating "● Agent" button at the bottom-right).'
