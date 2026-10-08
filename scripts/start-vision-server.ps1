# Serve UI-TARS-1.5-7B locally for vision grounding, OpenAI-compatible, on 127.0.0.1:11435.
#
# Uses the llama-server bundled with Ollama (no extra install) and the GGUF + vision projector in
# runtime/models/ui-tars/ (fetched once; see docs/operations.md). The agent's vision backend talks to
# http://127.0.0.1:11435/v1 when [vision] backend = "ui_tars".
#
#   pwsh scripts/start-vision-server.ps1            # foreground
# Needs ~6-7 GB free VRAM. Ctrl+C to stop.

param([int]$Port = 11435, [int]$GpuLayers = 999, [int]$ContextSize = 8192)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')

$models = Join-Path $env:LGA_ROOT 'runtime\models\ui-tars'
$model = Join-Path $models 'UI-TARS-1.5-7B-q4_k_m.gguf'
$mmproj = Join-Path $models 'UI-TARS-1.5-7B-f16.mmproj'
foreach ($f in @($model, $mmproj)) {
    if (-not (Test-Path $f)) { throw "missing $f; see docs/operations.md (download UI-TARS model)" }
}

$server = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\lib\ollama\llama-server.exe'
if (-not (Test-Path $server)) {
    $server = (Get-Command llama-server.exe -ErrorAction SilentlyContinue).Source
}
if (-not $server) { throw 'llama-server.exe not found (install Ollama, or put llama-server on PATH)' }

Write-Host "UI-TARS vision server on http://127.0.0.1:$Port/v1  (model: $model)"
& $server --model $model --mmproj $mmproj --host 127.0.0.1 --port $Port `
    --n-gpu-layers $GpuLayers --ctx-size $ContextSize --temp 0 --alias ui-tars-1.5-7b
exit $LASTEXITCODE
