# Dot-source this file to put user-scoped toolchains on PATH for the current process only.
#   . .\scripts\env.ps1
# It never modifies the persistent user or machine PATH.

$ErrorActionPreference = 'Stop'

$candidateDirs = @(
    (Join-Path $env:USERPROFILE '.cargo\bin'),
    (Join-Path $env:LOCALAPPDATA 'Programs\go\bin'),
    (Join-Path $env:USERPROFILE 'go\bin'),
    (Join-Path $env:USERPROFILE '.local\bin')
)

$current = $env:Path -split ';'
foreach ($dir in $candidateDirs) {
    if ((Test-Path $dir) -and ($current -notcontains $dir)) {
        $env:Path = "$dir;$env:Path"
    }
}

$env:LGA_ROOT = Split-Path -Parent $PSScriptRoot
