# Single entry point for the unified agent.
#
# Starts the one chat (the ● Agent popup) and pre-warms the subsystems it orchestrates:
#   - Gemini Web gateway (the brain)
#   - Company Brain knowledge backend (headless `python -m brain serve`, the KnowledgeProvider's backend)
#   - optional: local UI-TARS vision server (only with -Vision; needs ~6 GB VRAM)
#
# This replaces "Company Brain.bat" (its chat widget): there is now ONE agent and ONE chat. Company Brain
# keeps running only as the knowledge subsystem; the user talks to the ● Agent popup, which decides by
# itself when to use company knowledge, Windows, Chrome, AutoCAD, vision, shell or files.

param([switch]$Vision, [switch]$NoKnowledge)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$root = $env:LGA_ROOT

function Test-Up([string]$url) {
    try { (Invoke-WebRequest -Uri $url -NoProxy -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}

# 1. Gemini gateway (the agent CLI also autostarts it, but pre-warming avoids first-task latency).
if (-not (Test-Up 'http://127.0.0.1:8790/health')) {
    & (Join-Path $root 'target\release\agent.exe') gateway start
}

# 2. Company Brain knowledge backend (headless). The worker also autostarts it on first knowledge.* call.
if (-not $NoKnowledge -and -not (Test-Up 'http://127.0.0.1:8765/api/health')) {
    if (Test-Path (Join-Path $root '..\brain\__main__.py')) {
        Write-Host 'Starting Company Brain knowledge backend (headless serve)...'
        Start-Process -FilePath 'pythonw' -ArgumentList '-m', 'brain', 'serve' -WorkingDirectory (Join-Path $root '..')
    } else {
        Write-Warning 'Company Brain not found next to this repo; knowledge.* tools will be unavailable.'
    }
}

# 3. Optional local vision model.
if ($Vision -and -not (Test-Up 'http://127.0.0.1:11435/v1/models')) {
    Start-Process -FilePath 'pwsh' -ArgumentList '-NoProfile', '-File', (Join-Path $PSScriptRoot 'start-vision-server.ps1') -WindowStyle Hidden
}

# 4. The one chat.
& (Join-Path $PSScriptRoot 'start-widget.ps1')
Write-Host 'Unified agent ready. Talk to it through the ● Agent popup (one chat for everything).'
