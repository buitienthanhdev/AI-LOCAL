# Single entry point for the unified agent: opens the chat window (apps/agent-desktop).
#
# The window itself starts the subsystems that are down (Gemini gateway, Company Brain knowledge backend)
# and shows their health.


$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$root = $env:LGA_ROOT
$desktop = Join-Path $root 'target\release\agent-desktop.exe'
if (-not (Test-Path $desktop) -or -not (Test-Path (Join-Path $root 'target\release\agent.exe'))) {
    Push-Location $root; try { cargo build --release } finally { Pop-Location }
}

Start-Process -FilePath $desktop -WorkingDirectory $root
Write-Host 'Chat window opening (one chat for everything).'
