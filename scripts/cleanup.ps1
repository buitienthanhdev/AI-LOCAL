# Remove old generated output so the repository folder does not grow without bound.
#   pwsh scripts/cleanup.ps1               # dry run: lists what would be removed
#   pwsh scripts/cleanup.ps1 -Apply        # removes it
#   pwsh scripts/cleanup.ps1 -Days 30 -Apply
# Covers: artifacts/<task>/ and artifacts/screenshots older than -Days, test output (artifacts/cad-unit,
# artifacts/dev), and memory/tasks/*.json older than -TaskDays (default 60; the chat history reads them).
param([int]$Days = 14, [int]$TaskDays = 60, [switch]$Apply)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')
$root = $env:LGA_ROOT
$cut = (Get-Date).AddDays(-$Days)
$taskCut = (Get-Date).AddDays(-$TaskDays)
$targets = @()
$art = Join-Path $root 'artifacts'
if (Test-Path $art) {
    $targets += Get-ChildItem $art -Directory | Where-Object { $_.Name -in 'cad-unit', 'dev' -or ($_.Name -ne 'screenshots' -and $_.LastWriteTime -lt $cut) }
    $shots = Join-Path $art 'screenshots'
    if (Test-Path $shots) { $targets += Get-ChildItem $shots -File -Recurse | Where-Object { $_.LastWriteTime -lt $cut -and $_.Name -ne '.gitkeep' } }
}
$tasks = Join-Path $root 'memory\tasks'
if (Test-Path $tasks) { $targets += Get-ChildItem $tasks -File | Where-Object { $_.LastWriteTime -lt $taskCut } }
$bytes = 0
foreach ($t in $targets) {
    $size = if ($t.PSIsContainer) { (Get-ChildItem $t.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum } else { $t.Length }
    $bytes += [long]$size
    if ($Apply) { Remove-Item -LiteralPath $t.FullName -Recurse -Force } else { Write-Host ("would remove {0}" -f $t.FullName) }
}
$verb = if ($Apply) { 'Removed' } else { 'Would remove' }
Write-Host ("{0} {1} items, {2:N1} MB" -f $verb, $targets.Count, ($bytes / 1MB))
