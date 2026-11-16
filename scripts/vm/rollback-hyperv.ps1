<#
.SYNOPSIS
  ROLLBACK of the agent-VM setup. Each part is optional; nothing is deleted without -Confirm prompts.
    -RemoveVm       stop and delete the VM "LGA-Agent" and its files under D:\LGA-VM (the ISO is kept)
    -UnregisterHv   remove the Hyper-V socket service registration
    -DisableHyperV  disable the Hyper-V role again (reboot needed afterwards)
  Restoring the boot configuration saved by enable-hyperv.ps1 is a last resort, done by hand:
    bcdedit /import <repo>\logs\vm\backup-<time>\bcd.bak
#>
#Requires -RunAsAdministrator
param([switch]$RemoveVm, [switch]$UnregisterHv, [switch]$DisableHyperV,
      [string]$Name = 'LGA-Agent', [string]$VmRoot = 'D:\LGA-VM',
      [string]$ServiceId = '6c6761c1-0a4e-4f5d-9a1b-7a6d0c2b9e01')
$ErrorActionPreference = 'Stop'
if ($RemoveVm -and (Get-Command Get-VM -ErrorAction SilentlyContinue) -and (Get-VM -Name $Name -ErrorAction SilentlyContinue)) {
    Stop-VM -Name $Name -TurnOff -Force -ErrorAction SilentlyContinue
    Remove-VM -Name $Name -Confirm
    Get-ChildItem $VmRoot -Exclude iso | Remove-Item -Recurse -Confirm
}
if ($UnregisterHv) {
    Remove-Item "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices\{$ServiceId}" -ErrorAction SilentlyContinue
}
if ($DisableHyperV) {
    Disable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -NoRestart
    Write-Output 'Hyper-V disabled; restart Windows to finish.'
}
