# Start the Gemini gateway in the foreground (Ctrl+C to stop; managed backends stop with it).
# The agent core starts it automatically when [llm] gateway_autostart = true.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$python = Join-Path $env:LGA_ROOT 'services\gemini-gateway\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'gateway venv missing; run scripts/setup-gateway.ps1' }
& $python -m lga_gateway
exit $LASTEXITCODE
