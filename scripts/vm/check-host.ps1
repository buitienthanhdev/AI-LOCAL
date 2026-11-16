<#
.SYNOPSIS
  READ-ONLY readiness check of this PC for the agent VM (docs/vm-isolation.md). Changes nothing.
  Run elevated to also see the optional-feature states.
#>
$ErrorActionPreference = 'Continue'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$r = [ordered]@{}
$r.os = "$($os.Caption) build $($os.BuildNumber)"
$r.edition_ok = $os.Caption -match 'Pro|Enterprise|Education'
$r.ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$r.ram_free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$r.cpu = "$($cpu.Name) ($($cpu.NumberOfLogicalProcessors) logical)"
# With a hypervisor already running (VBS / Hyper-V), Win32_Processor reports the firmware flags as False.
$r.hypervisor_present = [bool]$cs.HypervisorPresent
$r.vt_firmware = $cpu.VirtualizationFirmwareEnabled
$r.hyperv_module = [bool](Get-Command Get-VM -ErrorAction SilentlyContinue)
$r.vmms_service = [bool](Get-Service vmms -ErrorAction SilentlyContinue)
$dg = Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction SilentlyContinue
$r.vbs_status = if ($dg) { $dg.VirtualizationBasedSecurityStatus } else { 'unknown' }
$r.drives = (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free -gt 0 } |
    ForEach-Object { "$($_.Name): {0:N0} GB free" -f ($_.Free / 1GB) }) -join '; '
$r.tpm_host = try { (Get-Tpm -ErrorAction Stop).TpmPresent } catch { 'needs admin' }
$r.admin = $admin
if ($admin) {
    foreach ($f in 'Microsoft-Hyper-V-All', 'Microsoft-Hyper-V-Management-PowerShell', 'Containers-DisposableClientVM') {
        $r["feature_$f"] = (Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue).State
    }
} else {
    $r.features = 'run elevated to read Hyper-V / Windows Sandbox feature states'
}
$r.python_af_hyperv = try { & py -3.12 -c "import socket;print(hasattr(socket,'AF_HYPERV'))" 2>$null } catch { 'n/a' }
$r.iso_found = (Get-ChildItem C:\, D:\, E:\, "$env:USERPROFILE\Downloads" -Filter *.iso -ErrorAction SilentlyContinue |
    Select-Object -First 5 -ExpandProperty FullName) -join '; '
$r.verdict = if (-not $r.edition_ok) { 'Hyper-V needs Pro/Enterprise/Education' }
    elseif ($r.ram_gb -lt 16) { 'too little RAM for a Windows guest next to your work' }
    elseif (-not $r.vmms_service) { 'ready to ENABLE Hyper-V (needs admin + reboot: scripts/vm/enable-hyperv.ps1)' }
    else { 'Hyper-V is on: ready to create the VM (scripts/vm/new-agent-vm.ps1)' }
$r | ConvertTo-Json
