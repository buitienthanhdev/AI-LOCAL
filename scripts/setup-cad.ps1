# Installs the CAD runtime used by cad.build / cad.inspect: text-to-cad's `cadgen` (build123d on Open CASCADE)
# in its own Python 3.13 virtual environment at runtime/cad. Telemetry stays off (CADGEN_INSTALL_CHANNEL is set by the tool).
# Requires uv (https://docs.astral.sh/uv/). Safe to rerun.

param([string]$Version = '0.7.21')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root 'runtime\cad'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $python)) {
    uv venv $venv --python 3.13
}
uv pip install --python $python "cadgen==$Version"
& $python -c "import cadgen, build123d; print('cadgen OK, build123d', build123d.__version__)"
