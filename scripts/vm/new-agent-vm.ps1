<#
.SYNOPSIS
  CHANGES THE HOST: creates the isolated agent VM "LGA-Agent" (Hyper-V Gen 2, vTPM, Secure Boot) from a
  Windows 11 ISO the owner supplies, registers the Hyper-V socket service for the agent channel and writes the
  channel key. Run only after approval (docs/vm-isolation.md, step H2).

.ISOLATION DEFAULTS (deliberate)
  * NO network adapter: the guest cannot reach the LAN, the host or the internet. The agent channel uses a
    Hyper-V socket, which needs no network. (Office activation / Windows Update need temporary network: see
    -TemporaryInternet, which attaches the "Default Switch" (NAT) until you run Remove-VMNetworkAdapter.)
  * Enhanced session OFF for this VM: vmconnect then uses a basic session, so the host clipboard, drives,
    printers and smart cards are NOT redirected into the VM.
  * Guest Service Interface ON only during provisioning (Copy-VMFile host->guest), then turned off.
  * Checkpoints: "Production"; one named "clean-provisioned" is taken by provision-guest.ps1.
  * Memory: dynamic 4-12 GB (host has 64 GB). 6 vCPUs. Disk: dynamic VHDX on D:\LGA-VM (299 GB free).

.EXAMPLE
  .\new-agent-vm.ps1 -IsoPath D:\iso\Win11_24H2_English_x64.iso
#>
#Requires -RunAsAdministrator
param(
    [string]$IsoPath,
    # A system disk prepared by build-vhdx.ps1 (no Windows setup UI). Used instead of -IsoPath.
    [string]$VhdPath,
    [string]$Name = 'LGA-Agent',
    [string]$VmRoot = 'D:\LGA-VM',
    [int64]$DiskGB = 96,
    [int]$Cpus = 6,
    [int64]$StartupGB = 6, [int64]$MinGB = 4, [int64]$MaxGB = 12,
    [string]$ServiceId = '6c6761c1-0a4e-4f5d-9a1b-7a6d0c2b9e01',
    [switch]$TemporaryInternet
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
if (-not $VhdPath -and -not ($IsoPath -and (Test-Path $IsoPath))) { throw "give -VhdPath (build-vhdx.ps1) or an existing -IsoPath" }
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { throw "VM $Name already exists" }

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null
if ($VhdPath) {
    New-VM -Name $Name -Generation 2 -Path $VmRoot -VHDPath $VhdPath | Out-Null
} else {
    $vhd = Join-Path $VmRoot "$Name.vhdx"
    New-VM -Name $Name -Generation 2 -Path $VmRoot -NewVHDPath $vhd -NewVHDSizeBytes ($DiskGB * 1GB) | Out-Null
}
Get-VMNetworkAdapter -VMName $Name | Remove-VMNetworkAdapter          # no network at all
Set-VM -Name $Name -ProcessorCount $Cpus -DynamicMemory -MemoryStartupBytes ($StartupGB * 1GB) `
    -MemoryMinimumBytes ($MinGB * 1GB) -MemoryMaximumBytes ($MaxGB * 1GB) `
    -AutomaticCheckpointsEnabled $false -CheckpointType Production `
    -AutomaticStopAction Save -AutomaticStartAction Nothing -EnhancedSessionTransportType VMBus
Set-VMHost -EnableEnhancedSessionMode $false                         # basic session: no clipboard/drive redirection
Set-VMKeyProtector -VMName $Name -NewLocalKeyProtector
Enable-VMTPM -VMName $Name
Set-VMFirmware -VMName $Name -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
if (-not $VhdPath) {
    Add-VMDvdDrive -VMName $Name -Path $IsoPath
    Set-VMFirmware -VMName $Name -FirstBootDevice (Get-VMDvdDrive -VMName $Name)
}
Enable-VMIntegrationService -VMName $Name -Name 'Guest Service Interface'   # provisioning only
if ($TemporaryInternet) { Add-VMNetworkAdapter -VMName $Name -SwitchName 'Default Switch' -Name 'temporary-internet' }

# Hyper-V socket service registration (host side), so the guest can listen on this service id.
$gcs = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices'
New-Item -Path "$gcs\{$ServiceId}" -Force | Out-Null
Set-ItemProperty -Path "$gcs\{$ServiceId}" -Name ElementName -Value 'LGA agent channel'

# Channel key (hex). Readable by the owner only.
$keyFile = Join-Path $repo 'runtime\vm\agent-vm.key'
New-Item -ItemType Directory -Force -Path (Split-Path $keyFile) | Out-Null
if (-not (Test-Path $keyFile)) {
    $bytes = New-Object byte[] 32; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
    Set-Content -Path $keyFile -Value (($bytes | ForEach-Object { $_.ToString('x2') }) -join '') -NoNewline -Encoding ascii
}
icacls $keyFile /inheritance:r /grant:r "${env:USERNAME}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null

$vmId = (Get-VM -Name $Name).Id.Guid
Write-Output "Created $Name ($vmId). Endpoint for configs/config.toml [vm]: hv:${vmId}:$ServiceId"
Write-Output "Next: Start-VM $Name; vmconnect localhost $Name; install Windows; then scripts/vm/provision-guest.ps1"
