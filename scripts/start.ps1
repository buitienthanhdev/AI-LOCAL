# Single entry point for the unified agent: opens the chat window (apps/agent-desktop).
#
# The window itself starts the subsystems that are down (Gemini gateway, Company Brain knowledge backend)
# and shows their health. -Vision also starts the optional local UI-TARS vision server (~6 GB VRAM).

param([switch]$Vision)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$root = $env:LGA_ROOT
$desktop = Join-Path $root 'target\release\agent-desktop.exe'
if (-not (Test-Path $desktop) -or -not (Test-Path (Join-Path $root 'target\release\agent.exe'))) {
    Push-Location $root; try { cargo build --release } finally { Pop-Location }
}

if ($Vision) {
    $up = try { (Invoke-WebRequest -Uri 'http://127.0.0.1:11435/v1/models' -NoProxy -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
    if (-not $up) {
        Start-Process -FilePath 'pwsh' -ArgumentList '-NoProfile', '-File', (Join-Path $PSScriptRoot 'start-vision-server.ps1') -WindowStyle Hidden
    }
}

Start-Process -FilePath $desktop -WorkingDirectory $root
Write-Host 'Chat window opening (one chat for everything).'
