<#
.SYNOPSIS
  CHANGES THE VM: installs the guest worker into the agent VM over PowerShell Direct (VMBus) and takes the
  "clean-provisioned" checkpoint. Needs host admin. Nothing is downloaded inside the VM: Python, the
  wheels and the code are copied from the host (docs/vm-isolation.md, step H3).

  Inside the VM:
    1. C:\LGA <- bundle (python-agent + lga-common + tools + skills + configs; no memory/, logs or host keys
       except the VM channel key), a standalone CPython 3.12 and a wheelhouse built on the host;
    2. venv C:\LGA\services\python-agent\.venv installed offline (--no-index) from the wheelhouse;
    3. standard (non-admin) local user "lga-agent" with auto-logon, so the agent has an interactive desktop;
    4. scheduled task "LGA guest worker" at logon of lga-agent (pythonw, restart on failure):
       -m lga_worker.remote.guest --listen hv:<wildcard>:<service-guid> --key-file C:\LGA\vm.key
    5. host: Guest Service Interface off, temporary network adapter removed, restart, checkpoint.
#>
#Requires -RunAsAdministrator
param(
    [string]$Name = 'LGA-Agent',
    # Default: lga-admin with the password build-vhdx.ps1 wrote to runtime\vm\guest-admin.txt
    [pscredential]$GuestAdmin,
    [string]$ServiceId = '6c6761c1-0a4e-4f5d-9a1b-7a6d0c2b9e01',
    [string]$Wheelhouse = 'D:\LGA-VM\wheelhouse',
    [string]$Requirements = 'D:\LGA-VM\requirements-guest.txt',
    [string]$PythonHome = "$env:APPDATA\uv\python\cpython-3.12.13-windows-x86_64-none",
    [int]$ReadyTimeoutMin = 30,
    [switch]$KeepNetwork
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$key = Join-Path $repo 'runtime\vm\agent-vm.key'
if (-not (Test-Path $key)) { throw "channel key missing: run new-agent-vm.ps1 first" }
foreach ($p in $Wheelhouse, $Requirements, "$PythonHome\python.exe") { if (-not (Test-Path $p)) { throw "missing $p" } }
if (-not $GuestAdmin) {
    $pw = Get-Content (Join-Path $repo 'runtime\vm\guest-admin.txt') -Raw
    $GuestAdmin = New-Object pscredential('lga-admin', (ConvertTo-SecureString $pw -AsPlainText -Force))
}

# 0. wait until Windows in the VM finished its first boot and accepts PowerShell Direct
$deadline = (Get-Date).AddMinutes($ReadyTimeoutMin)
while ($true) {
    try { Invoke-Command -VMName $Name -Credential $GuestAdmin -ScriptBlock { $env:COMPUTERNAME } -ErrorAction Stop | Out-Null; break }
    catch { if ((Get-Date) -gt $deadline) { throw "VM not ready after $ReadyTimeoutMin min: $($_.Exception.Message)" }; Start-Sleep 15 }
}
Write-Output "VM ready (PowerShell Direct)"

# 1. bundle
$stage = Join-Path $env:TEMP "lga-guest-bundle"
Remove-Item $stage, "$stage.zip" -Recurse -Force -ErrorAction SilentlyContinue
foreach ($d in 'services\python-agent', 'services\lga-common', 'tools', 'skills') {
    robocopy (Join-Path $repo $d) (Join-Path $stage $d) /E /XD .venv __pycache__ .pytest_cache /NFL /NDL /NJH /NJS | Out-Null
}
robocopy $PythonHome (Join-Path $stage 'python') /E /NFL /NDL /NJH /NJS | Out-Null
robocopy $Wheelhouse (Join-Path $stage 'wheelhouse') /E /NFL /NDL /NJH /NJS | Out-Null
Copy-Item $Requirements (Join-Path $stage 'requirements-guest.txt')
New-Item -ItemType Directory -Force (Join-Path $stage 'configs') | Out-Null
Copy-Item (Join-Path $repo 'configs\config.example.toml') (Join-Path $stage 'configs\config.example.toml')
Set-Content (Join-Path $stage 'configs\config.toml') "[vm]`nenabled = false`n" -Encoding ascii   # the guest never routes
Compress-Archive -Path "$stage\*" -DestinationPath "$stage.zip" -CompressionLevel Fastest
Copy-VMFile -Name $Name -SourcePath "$stage.zip" -DestinationPath 'C:\LGA\bundle.zip' -CreateFullPath -FileSource Host -Force
Copy-VMFile -Name $Name -SourcePath $key -DestinationPath 'C:\LGA\vm.key' -CreateFullPath -FileSource Host -Force
Remove-Item $stage, "$stage.zip" -Recurse -Force
Write-Output "bundle copied"

# 2-4. inside the guest (PowerShell Direct)
Invoke-Command -VMName $Name -Credential $GuestAdmin -ArgumentList $ServiceId -ScriptBlock {
    param($ServiceId)
    $ErrorActionPreference = 'Stop'
    Remove-Item C:\Windows\Panther\unattend.xml -Force -ErrorAction SilentlyContinue   # held the admin password
    Expand-Archive C:\LGA\bundle.zip -DestinationPath C:\LGA -Force
    Remove-Item C:\LGA\bundle.zip
    $venv = 'C:\LGA\services\python-agent\.venv'
    & C:\LGA\python\python.exe -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install --no-index --find-links C:\LGA\wheelhouse -r C:\LGA\requirements-guest.txt -q --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }
    # our two packages by path (no build backend needed offline)
    Set-Content "$venv\Lib\site-packages\lga-local.pth" "C:\LGA\services\lga-common`r`nC:\LGA\services\python-agent" -Encoding ascii
    & "$venv\Scripts\python.exe" -c "import lga_worker.remote.guest, lga_worker.windows.windows_use_provider, cua_driver; print('imports ok')"
    if ($LASTEXITCODE -ne 0) { throw 'guest imports failed' }
    Remove-Item C:\LGA\wheelhouse -Recurse -Force

    if (-not (Get-LocalUser -Name lga-agent -ErrorAction SilentlyContinue)) {
        Add-Type -AssemblyName System.Web
        $plain = [System.Web.Security.Membership]::GeneratePassword(24, 4)
        New-LocalUser -Name lga-agent -Password (ConvertTo-SecureString $plain -AsPlainText -Force) `
            -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
        Add-LocalGroupMember -Group Users -Member lga-agent
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty $wl AutoAdminLogon '1'; Set-ItemProperty $wl DefaultUserName 'lga-agent'
        Set-ItemProperty $wl DefaultDomainName $env:COMPUTERNAME
        Set-ItemProperty $wl DefaultPassword $plain     # VM-local account; the VM has no network afterwards
    }
    icacls C:\LGA /grant "lga-agent:(OI)(CI)(M)" | Out-Null
    icacls C:\LGA\vm.key /inheritance:r /grant:r "lga-agent:(R)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null
    [Environment]::SetEnvironmentVariable('LGA_ROOT', 'C:\LGA', 'Machine')
    $argLine = "-m lga_worker.remote.guest --listen hv:00000000-0000-0000-0000-000000000000:$ServiceId " +
               "--key-file C:\LGA\vm.key --log-file C:\LGA\logs\vm\guest-worker.log"
    $action = New-ScheduledTaskAction -Execute "$venv\Scripts\pythonw.exe" -Argument $argLine -WorkingDirectory 'C:\LGA'
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User lga-agent
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId lga-agent -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'LGA guest worker' -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
    # no lock screen / sleep inside the agent desktop
    powercfg /change monitor-timeout-ac 0; powercfg /change standby-timeout-ac 0
    New-Item 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' NoLockScreen 1
    Write-Output "guest configured"
}

# 5. close the provisioning doors
Disable-VMIntegrationService -VMName $Name -Name 'Guest Service Interface'
if (-not $KeepNetwork) { Get-VMNetworkAdapter -VMName $Name | Remove-VMNetworkAdapter }
Restart-VM -Name $Name -Force -Wait -For Heartbeat
Start-Sleep 60   # auto-logon and the scheduled task
Checkpoint-VM -Name $Name -SnapshotName 'clean-provisioned'
Write-Output "PROVISION OK. endpoint = hv:$((Get-VM $Name).Id.Guid):$ServiceId"
