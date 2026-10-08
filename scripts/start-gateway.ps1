# Start the Gemini gateway in the foreground (Ctrl+C to stop; managed backends stop with it).
# The agent core starts it automatically when [llm] gateway_autostart = true.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$exe = Join-Path $env:LGA_ROOT 'target\release\lga-gateway.exe'
if (-not (Test-Path $exe)) { Push-Location $env:LGA_ROOT; try { cargo build --release -p lga-gateway } finally { Pop-Location } }
& $exe
exit $LASTEXITCODE
