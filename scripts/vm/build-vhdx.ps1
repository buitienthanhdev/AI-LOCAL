<#
.SYNOPSIS
  CHANGES THE HOST DISK (D:\LGA-VM only): builds the agent VM's system disk straight from the Windows ISO,
  with no setup UI. The VHDX is partitioned for UEFI (Gen 2), install.wim is applied, the boot files are
  written, and an unattend.xml skips OOBE and creates a local admin "lga-admin". Its random password is
  written to runtime\vm\guest-admin.txt (ACL: the owner only); provision-guest.ps1 uses it.
  Run elevated, after Hyper-V is active (step H2a in docs/vm-isolation.md).
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)] [string]$IsoPath,
    [string]$VhdPath = 'D:\LGA-VM\LGA-Agent.vhdx',
    [int64]$SizeGB = 96,
    [string]$Edition = 'Enterprise Evaluation',
    [string]$TimeZone = 'SE Asia Standard Time'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
if (Test-Path $VhdPath) { throw "$VhdPath already exists" }
New-Item -ItemType Directory -Force -Path (Split-Path $VhdPath) | Out-Null

$iso = Mount-DiskImage -ImagePath $IsoPath -PassThru -Access ReadOnly -StorageType ISO
try {
    $isoLetter = ($iso | Get-Volume).DriveLetter
    $wim = "${isoLetter}:\sources\install.wim"
    if (-not (Test-Path $wim)) { $wim = "${isoLetter}:\sources\install.esd" }
    # Exactly one image must match: the 90-day evaluation (EditionID EnterpriseEval), never a retail edition.
    $image = @(Get-WindowsImage -ImagePath $wim | Where-Object ImageName -eq "Windows 11 $Edition")
    if ($image.Count -ne 1) { $image = $null } else { $image = $image[0] }
    if (-not $image) { throw "edition '$Edition' not in ${wim}: $((Get-WindowsImage -ImagePath $wim).ImageName -join ', ')" }
    Write-Output "applying $($image.ImageName) (index $($image.ImageIndex))"

    New-VHD -Path $VhdPath -SizeBytes ($SizeGB * 1GB) -Dynamic | Out-Null
    $disk = Mount-VHD -Path $VhdPath -Passthru | Get-Disk
    try {
        Initialize-Disk -Number $disk.Number -PartitionStyle GPT
        $efi = New-Partition -DiskNumber $disk.Number -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
        Format-Volume -Partition $efi -FileSystem FAT32 -NewFileSystemLabel System -Confirm:$false | Out-Null
        New-Partition -DiskNumber $disk.Number -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' | Out-Null
        $os = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
        Format-Volume -Partition $os -FileSystem NTFS -NewFileSystemLabel Windows -Confirm:$false | Out-Null
        $efi | Add-PartitionAccessPath -AssignDriveLetter
        $efi = Get-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber
        $osRoot = "$($os.DriveLetter):\"
        Expand-WindowsImage -ImagePath $wim -Index $image.ImageIndex -ApplyPath $osRoot | Out-Null
        & "$osRoot\Windows\System32\bcdboot.exe" "$osRoot\Windows" /s "$($efi.DriveLetter):" /f UEFI | Out-Null

        $pwFile = Join-Path $repo 'runtime\vm\guest-admin.txt'
        New-Item -ItemType Directory -Force -Path (Split-Path $pwFile) | Out-Null
        $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!#%+=?'
        $bytes = New-Object byte[] 24; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
        $password = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
        Set-Content -Path $pwFile -Value $password -NoNewline -Encoding ascii
        icacls $pwFile /inheritance:r /grant:r "${env:USERNAME}:(R,W)" "SYSTEM:(F)" "Administrators:(F)" | Out-Null

        $unattend = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>LGA-AGENT</ComputerName>
      <TimeZone>$TimeZone</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>vi-VN</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC><SkipMachineOOBE>true</SkipMachineOOBE><SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts><LocalAccounts>
        <LocalAccount wcm:action="add">
          <Name>lga-admin</Name><Group>Administrators</Group><DisplayName>LGA admin</DisplayName>
          <Password><Value>$password</Value><PlainText>true</PlainText></Password>
        </LocalAccount>
      </LocalAccounts></UserAccounts>
    </component>
  </settings>
</unattend>
"@
        New-Item -ItemType Directory -Force -Path "$osRoot\Windows\Panther" | Out-Null
        Set-Content -Path "$osRoot\Windows\Panther\unattend.xml" -Value $unattend -Encoding utf8
        Write-Output "system disk ready: $VhdPath"
    } finally {
        Dismount-VHD -Path $VhdPath
    }
} finally {
    Dismount-DiskImage -ImagePath $IsoPath | Out-Null
}
