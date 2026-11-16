<#
.SYNOPSIS
  CHANGES WINDOWS: enables the Hyper-V role (platform + management tools). Needs admin. Does NOT reboot.
  Run only after the owner approved it (docs/vm-isolation.md, step H1).

  What it does, and nothing else:
    1. pre-flight: admin, Windows edition, a running hypervisor or VT firmware flag, RAM, free space on C:
       (the feature needs ~1 GB), no pending reboot;
    2. saves a backup of the current optional-feature and boot (BCD) state to logs/vm/backup-<time>/;
    3. Enable-WindowsOptionalFeature Microsoft-Hyper-V-All -All -NoRestart;
    4. writes a transcript to logs/vm/enable-hyperv-<time>.log.
  It never restarts Windows: the owner restarts when convenient.

.ROLLBACK
  scripts/vm/rollback-hyperv.ps1 (disables the feature again; reboot needed).
#>
#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logDir = Join-Path $repo 'logs\vm'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir "enable-hyperv-$stamp.log") | Out-Null
try {
    # 1. pre-flight
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    if ($os.Caption -notmatch 'Pro|Enterprise|Education') { throw "Hyper-V is not available on $($os.Caption)" }
    if (-not ($cs.HypervisorPresent -or $cpu.VirtualizationFirmwareEnabled)) {
        throw 'CPU virtualization is disabled in the firmware (BIOS/UEFI): enable Intel VT-x first'
    }
    if ($cs.TotalPhysicalMemory -lt 16GB) { throw 'less than 16 GB RAM' }
    if ((Get-PSDrive C).Free -lt 5GB) { throw 'less than 5 GB free on C:' }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
        throw 'Windows has a pending reboot from another change: restart first, then run this again'
    }
    $state = (Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All).State
    Write-Output "pre-flight OK: $($os.Caption) $($os.BuildNumber), RAM $([math]::Round($cs.TotalPhysicalMemory/1GB)) GB, Hyper-V state: $state"
    if ($state -eq 'Enabled') { Write-Output 'Hyper-V is already enabled; nothing to do.'; return }

    # 2. backup of what this script changes
    $backup = Join-Path $logDir "backup-$stamp"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Get-WindowsOptionalFeature -Online | Select-Object FeatureName, State |
        Export-Csv (Join-Path $backup 'optional-features.csv') -NoTypeInformation
    bcdedit /enum all > (Join-Path $backup 'bcd.txt')
    bcdedit /export (Join-Path $backup 'bcd.bak') | Out-Null
    Write-Output "backup written to $backup"

    # 3. the change
    if ($WhatIf) { Write-Output 'WhatIf: would run Enable-WindowsOptionalFeature Microsoft-Hyper-V-All -All -NoRestart'; return }
    $result = Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart
    Write-Output "Hyper-V enabled (RestartNeeded=$($result.RestartNeeded)). Restart Windows yourself when convenient."
} finally {
    Stop-Transcript | Out-Null
}
