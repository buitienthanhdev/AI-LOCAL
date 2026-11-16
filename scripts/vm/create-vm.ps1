<#
.SYNOPSIS
  Owner-approved sequence (2026-10-22): system disk from the ISO, the VM with temporary internet, start it.
  Stops at the first error. Transcript: logs\vm\create-vm-<time>.log
#>
#Requires -RunAsAdministrator
param([string]$IsoPath = 'D:\LGA-VM\iso\Win11_Enterprise_Eval_26H2_x64_en-us.iso',
      [string]$VhdPath = 'D:\LGA-VM\LGA-Agent.vhdx', [string]$Name = 'LGA-Agent')
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
New-Item -ItemType Directory -Force -Path "$repo\logs\vm" | Out-Null
Start-Transcript -Path "$repo\logs\vm\create-vm-$(Get-Date -Format yyyyMMdd-HHmmss).log" | Out-Null
try {
    & "$PSScriptRoot\build-vhdx.ps1" -IsoPath $IsoPath -VhdPath $VhdPath
    & "$PSScriptRoot\new-agent-vm.ps1" -VhdPath $VhdPath -Name $Name -TemporaryInternet
    Start-VM -Name $Name
    Get-VM -Name $Name | Format-List Name, State, Id, ProcessorCount, MemoryStartup, Generation
    Write-Output 'CREATE-VM OK'
} catch {
    Write-Output "CREATE-VM FAILED: $($_.Exception.Message) at $($_.InvocationInfo.PositionMessage)"
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
