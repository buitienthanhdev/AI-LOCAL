<#
.SYNOPSIS
  Everyday control of the agent VM: status, viewer, pause/resume, save, restore the clean checkpoint.
    .\vm-control.ps1 status | view | pause | resume | save | start | revert
  view   opens vmconnect (basic session: no clipboard/drive sharing). Clicking into it = manual take-over;
         the agent's own Stop stays in the chat window and on the browser overlay.
  pause  freezes the VM instantly (emergency stop of everything inside it; resume continues).
  revert restores the "clean-provisioned" checkpoint (discards everything done in the VM since).
  Needs membership in "Hyper-V Administrators" (or admin).
#>
param([Parameter(Mandatory)][ValidateSet('status', 'view', 'pause', 'resume', 'save', 'start', 'revert')] [string]$Action,
      [string]$Name = 'LGA-Agent')
$ErrorActionPreference = 'Stop'
switch ($Action) {
    'status' { Get-VM -Name $Name | Select-Object Name, State, Uptime, CPUUsage,
                   @{n = 'MemoryGB'; e = { [math]::Round($_.MemoryAssigned / 1GB, 1) } }, Heartbeat | Format-List }
    'view'   { Start-Process vmconnect.exe -ArgumentList 'localhost', $Name }
    'pause'  { Suspend-VM -Name $Name }
    'resume' { Resume-VM -Name $Name }
    'save'   { Save-VM -Name $Name }
    'start'  { Start-VM -Name $Name }
    'revert' { Restore-VMSnapshot -VMName $Name -Name 'clean-provisioned' -Confirm:$true }
}
