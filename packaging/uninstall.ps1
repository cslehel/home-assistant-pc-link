<#
.SYNOPSIS
  Uninstall Home Assistant PC Link (standalone build).

.DESCRIPTION
  Stops and removes the service (which first restores the logon rights of every
  managed user, so nobody stays locked out), removes the firewall rule and the
  program folder.  Configuration and statistics in C:\ProgramData\PCLink are
  kept unless -Purge is given.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles "PCLink"),
    [switch]$Purge,
    [switch]$NoPause
)
$ErrorActionPreference = "Stop"

function Test-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Finish([int]$Code) {
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $Code
}

if (-not (Test-Admin)) {
    Write-Host "Administrator rights are needed; asking Windows for them..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-InstallDir", "`"$InstallDir`"")
    if ($Purge) { $argList += "-Purge" }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList
    exit 0
}

Write-Host "== Home Assistant PC Link uninstaller ==" -ForegroundColor Cyan
$exe = Join-Path $InstallDir "pclink.exe"
$svcInfo = Get-CimInstance Win32_Service -Filter "Name='PCLink'" -ErrorAction SilentlyContinue
if ($svcInfo) {
    if (Test-Path $exe) {
        # 'remove' stops the service, restores logon rights, restores the default ACL and deletes it.
        & $exe remove | Out-Host
    } else {
        sc.exe stop PCLink | Out-Null
        sc.exe delete PCLink | Out-Null
    }
    if ($svcInfo.ProcessId -gt 0) { Wait-Process -Id $svcInfo.ProcessId -Timeout 30 -ErrorAction SilentlyContinue }
}
Get-Process -Name "pclink-agent" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName "PC Link API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Removed $InstallDir"
}
$data = Join-Path $env:ProgramData "PCLink"
if ($Purge) {
    if (Test-Path $data) { Remove-Item $data -Recurse -Force; Write-Host "Removed $data" }
} else {
    Write-Host "Kept the configuration and statistics in $data (run with -Purge to delete them)."
}
Write-Host "Home Assistant PC Link was uninstalled." -ForegroundColor Green
Finish 0
