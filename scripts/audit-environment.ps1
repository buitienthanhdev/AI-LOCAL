# Read-only environment audit. Prints one line per probe: STATUS  NAME  DETAIL.
# Regenerates the facts recorded in docs/environment.md. Changes nothing on the system.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')

function Report([string]$status, [string]$name, [string]$detail) {
    '{0,-5} {1,-14} {2}' -f $status, $name, $detail
}

function Probe-Command([string]$name, [string[]]$versionArgs = @('--version')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    $out = & $cmd.Source @versionArgs 2>&1 | Select-Object -First 1
    return "$out ($($cmd.Source))"
}

$os = Get-CimInstance Win32_OperatingSystem
Report 'PASS' 'os' "$($os.Caption) $($os.Version) $($os.OSArchitecture)"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Report 'INFO' 'elevated' "$isAdmin"

$required = [ordered]@{ python = 'FAIL'; uv = 'FAIL'; cargo = 'FAIL'; git = 'FAIL'; node = 'WARN'; go = 'WARN' }
foreach ($name in $required.Keys) {
    $version = if ($name -eq 'go') { Probe-Command $name @('version') } else { Probe-Command $name }
    if ($version) { Report 'PASS' $name $version } else { Report $required[$name] $name 'not found' }
}

$uvPythons = & uv python list --only-installed 2>&1 | Select-String 'cpython-3\.12'
if ($uvPythons) { Report 'PASS' 'python3.12' ($uvPythons[0].ToString().Trim()) } else { Report 'WARN' 'python3.12' 'run: uv python install 3.12' }

$chromeKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
$chrome = (Get-ItemProperty $chromeKey -ErrorAction SilentlyContinue).'(default)'
if ($chrome -and (Test-Path $chrome)) {
    Report 'PASS' 'chrome' "$((Get-Item $chrome).VersionInfo.ProductVersion) ($chrome)"
} else { Report 'WARN' 'chrome' 'not registered under App Paths' }

$acadRoot = 'HKLM:\SOFTWARE\Autodesk\AutoCAD'
$acadFound = $false
if (Test-Path $acadRoot) {
    foreach ($release in Get-ChildItem $acadRoot) {
        foreach ($product in Get-ChildItem $release.PSPath -ErrorAction SilentlyContinue) {
            $location = (Get-ItemProperty $product.PSPath -ErrorAction SilentlyContinue).AcadLocation
            if ($location -and (Test-Path (Join-Path $location 'acad.exe'))) {
                Report 'PASS' 'autocad' "$($release.PSChildName)/$($product.PSChildName) at $location"
                $acadFound = $true
            }
        }
    }
}
if (-not $acadFound) { Report 'WARN' 'autocad' 'no AutoCAD installation found in registry' }

if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = & nvidia-smi --query-gpu=name,memory.total,memory.used,compute_cap --format=csv,noheader 2>&1
    Report 'INFO' 'gpu' "$gpu"
} else { Report 'INFO' 'gpu' 'no NVIDIA GPU / driver' }

$longPaths = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled
Report ($(if ($longPaths -eq 1) { 'PASS' } else { 'WARN' })) 'longpaths' "LongPathsEnabled=$longPaths"
