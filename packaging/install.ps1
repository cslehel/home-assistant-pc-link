<#
.SYNOPSIS
  Install or upgrade Home Assistant PC Link (standalone build - no Python needed).

.DESCRIPTION
  Copies this folder to C:\Program Files\PCLink, creates the configuration on the
  first run, installs the Windows service with tamper resistance, starts it, and
  opens the firewall port for the Home Assistant integration when the local API
  is enabled.  Run it again with a newer version to upgrade in place; the
  configuration and statistics in C:\ProgramData\PCLink are kept.

  The service must run from a folder that only administrators can change
  (Program Files), otherwise a standard user could replace the executable.

.EXAMPLE
  Double-click install.cmd, or from PowerShell:
  powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles "PCLink"),
    [switch]$NoHarden,
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

function Wait-ServiceProcessExit([int]$ProcessId) {
    if ($ProcessId -gt 0) { Wait-Process -Id $ProcessId -Timeout 30 -ErrorAction SilentlyContinue }
}

if (-not (Test-Admin)) {
    Write-Host "Administrator rights are needed; asking Windows for them..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-InstallDir", "`"$InstallDir`"")
    if ($NoHarden) { $argList += "-NoHarden" }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList
    exit 0
}

Write-Host "== Home Assistant PC Link installer ==" -ForegroundColor Cyan
$source = $PSScriptRoot
if (-not (Test-Path (Join-Path $source "pclink.exe"))) {
    Write-Host "pclink.exe was not found next to this script. Extract the whole zip first." -ForegroundColor Red
    Finish 1
}
$exe = Join-Path $InstallDir "pclink.exe"
$config = Join-Path $env:ProgramData "PCLink\config.yaml"

# 1. Stop a running copy so its files can be replaced.
$svcInfo = Get-CimInstance Win32_Service -Filter "Name='PCLink'" -ErrorAction SilentlyContinue
if ($svcInfo -and $svcInfo.State -ne "Stopped") {
    Write-Host "Stopping the running service..."
    Stop-Service -Name PCLink -Force
    Wait-ServiceProcessExit $svcInfo.ProcessId
}
Get-Process -Name "pclink-agent" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 2. Copy the files (a clean _internal folder, so no stale libraries remain after an upgrade).
$src = (Resolve-Path $source).Path.TrimEnd("\")
$dst = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
if ($src -ine $dst) {
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    $internal = Join-Path $dst "_internal"
    if (Test-Path $internal) { Remove-Item $internal -Recurse -Force }
    Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
    Write-Host "Copied to $dst"
}

# 3. Configuration: create it on the first run and stop so it can be edited.
if (-not (Test-Path $config)) {
    & $exe init-config | Out-Host
    Write-Host ""
    Write-Host "A new configuration was created: $config" -ForegroundColor Yellow
    Write-Host "Enter your Home Assistant connection and your users, save it, then run the installer again."
    Start-Process notepad.exe -ArgumentList "`"$config`""
    Finish 0
}
& $exe check-config | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "Fix the configuration shown above, then run the installer again." -ForegroundColor Red
    Finish 1
}

# 4. Install the service, or point an existing one (also a Python-based one) at this copy.
if (Get-Service -Name PCLink -ErrorAction SilentlyContinue) {
    & $exe update | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "Updating the service failed." -ForegroundColor Red; Finish 1 }
    if (-not $NoHarden) { & $exe harden | Out-Host }
} else {
    if ($NoHarden) { & $exe install --no-harden | Out-Host } else { & $exe install | Out-Host }
    if ($LASTEXITCODE -ne 0) { Write-Host "Installing the service failed." -ForegroundColor Red; Finish 1 }
}
Start-Service -Name PCLink
(Get-Service -Name PCLink).WaitForStatus("Running", [TimeSpan]::FromSeconds(30))

# 5. Firewall: open the local API port for the Home Assistant integration when it is enabled for the LAN.
$cfgText = Get-Content $config -Raw
if ($cfgText -match "(?ms)^api:\s*\n(?:[ \t]+.*\n)*?[ \t]+enabled:\s*true" -and $cfgText -notmatch "(?m)^[ \t]+host:\s*127\.0\.0\.1") {
    $port = 47392
    if ($cfgText -match "(?ms)^api:\s*\n(?:[ \t]+.*\n)*?[ \t]+port:\s*(\d+)") { $port = [int]$Matches[1] }
    Get-NetFirewallRule -DisplayName "PC Link API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null
    Write-Host "Firewall: allowed TCP $port (PC Link API) on private networks."
}

$version = (& $exe version)
Write-Host ""
Write-Host "Home Assistant PC Link $version is installed in $dst and running." -ForegroundColor Green
Write-Host "Commands: `"$exe`" help      Uninstall: run uninstall.cmd"
Finish 0
