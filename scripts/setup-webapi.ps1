# Create the venv used by services/gemini-webapi/server.py (Gemini web app through the gemini-webapi library).
#   pwsh scripts/setup-webapi.ps1
# Optional cookies (signed-in session, refreshed automatically): save {"__Secure-1PSID": "...",
# "__Secure-1PSIDTS": "..."} as runtime/gateway/gemini-webapi/cookies.json (git-ignored).
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$dir = Join-Path $env:LGA_ROOT 'runtime\gateway\gemini-webapi'
New-Item -ItemType Directory -Force $dir | Out-Null
$venv = Join-Path $dir 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { python -m venv $venv }
& (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --upgrade gemini-webapi aiohttp
Write-Host 'gemini-webapi ready. Enable it with [gateway.webapi] enabled = true in configs/config.toml.'
