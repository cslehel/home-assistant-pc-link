<#
.SYNOPSIS
  Install or upgrade Home Assistant PC Link (standalone build - no Python needed).

.DESCRIPTION
  Copies this folder to C:\Program Files\PCLink, creates the configuration on the
  first run, sets up the connection to Home Assistant (the local API with a token
  and HTTPS, unless it is set up already), installs the Windows service with
  tamper resistance, starts it, and opens the firewall port for the Home
  Assistant integration.  Run it again with a newer version to upgrade in place; the
  configuration and statistics in C:\ProgramData\PCLink are kept.

  An upgrade never leaves the PC without a working service: the new version is
  copied next to the old one and checked first, the two folders are then swapped,
  and when the new version does not start the old one is put back and started
  again.  What happened is written to install-result.json in the data folder,
  where the service reads it (it is shown on the "PC Link update" entity).

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
    [switch]$NoPause,
    [string]$ResultFile = (Join-Path $env:ProgramData "PCLink\install-result.json")
)
$ErrorActionPreference = "Stop"
$script:NewVersion = ""
$script:OldVersion = ""

function Test-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Finish([int]$Code) {
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $Code
}

function Test-ReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

# What this run did, for the service to report.  Only into a data folder that exists and is a real folder.
function Write-Result([bool]$Ok, [string]$Message, [bool]$RolledBack) {
    try {
        $dir = Split-Path -Parent $ResultFile
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return }
        if (Test-ReparsePoint $dir) { return }
        $data = [ordered]@{
            ok          = $Ok
            version     = $script:NewVersion
            previous    = $script:OldVersion
            error       = $Message
            rolled_back = $RolledBack
            ts          = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        }
        $data | ConvertTo-Json | Set-Content -LiteralPath $ResultFile -Encoding UTF8
    } catch {
        Write-Host "Note: could not write $ResultFile ($($_.Exception.Message))."
    }
}

function Fail([string]$Message, [bool]$RolledBack = $false) {
    Write-Host $Message -ForegroundColor Red
    Write-Result $false $Message $RolledBack
    Finish 1
}

function Stop-PCLink {
    $svcInfo = Get-CimInstance Win32_Service -Filter "Name='PCLink'" -ErrorAction SilentlyContinue
    if ($svcInfo -and $svcInfo.State -ne "Stopped") {
        Write-Host "Stopping the running service..."
        Stop-Service -Name PCLink -Force
        if ($svcInfo.ProcessId -gt 0) { Wait-Process -Id $svcInfo.ProcessId -Timeout 30 -ErrorAction SilentlyContinue }
    }
    Get-Process -Name "pclink-agent" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

# Rename a folder; a file in it may still be closing, so a few tries.
function Move-Folder([string]$From, [string]$To) {
    for ($try = 1; ; $try++) {
        try { Move-Item -LiteralPath $From -Destination $To -Force; return }
        catch { if ($try -ge 5) { throw }; Start-Sleep -Seconds 2 }
    }
}

function Remove-Folder([string]$Path) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue }
}

# Start the service and make sure it stays up; throws when it does not.
function Start-PCLink([string]$Exe, [string]$Expected) {
    Start-Service -Name PCLink
    (Get-Service -Name PCLink).WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
    Start-Sleep -Seconds 5
    $state = (Get-Service -Name PCLink).Status
    if ($state -ne "Running") { throw "the service stopped again right after starting (state: $state)" }
    if ($Expected) {
        $running = (& $Exe version)
        if ($LASTEXITCODE -ne 0 -or "$running".Trim() -ne $Expected) { throw "the installed program reports version '$running', expected '$Expected'" }
    }
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
$dataDir = Join-Path $env:ProgramData "PCLink"
$config = Join-Path $dataDir "config.yaml"

# The data folder holds the configuration, and the configuration says what the service runs as
# SYSTEM.  A link there (made by whoever could, before PC Link was installed) would put all of
# that into a folder of their choosing: nothing is installed until it is gone.
if (Test-ReparsePoint $dataDir) {
    Write-Host "$dataDir is a link or junction to another folder, not a folder." -ForegroundColor Red
    Write-Host "PC Link did not create it. Look where it points (dir /AL `"$env:ProgramData`"), remove it with" -ForegroundColor Red
    Write-Host "  rmdir `"$dataDir`"" -ForegroundColor Red
    Write-Host "and run the installer again." -ForegroundColor Red
    Finish 1
}

$src = (Resolve-Path $source).Path.TrimEnd("\")
$dst = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
$exe = Join-Path $dst "pclink.exe"
$new = "$dst.new"
$old = "$dst.old"
$inPlace = ($src -ieq $dst)
$upgrade = Test-Path -LiteralPath $exe

$versionFile = Join-Path $src "VERSION"
if (Test-Path -LiteralPath $versionFile) { $script:NewVersion = "$(Get-Content -LiteralPath $versionFile -TotalCount 1)".Trim() }
if ($upgrade) { try { $script:OldVersion = "$(& $exe version)".Trim() } catch { } }

# 1. Put the new version next to the installed one and see that it runs at all.  Nothing has been
#    stopped or changed yet: whatever fails up to step 3 leaves the PC exactly as it was.
$staged = $exe
if (-not $inPlace) {
    try {
        Remove-Folder $new
        New-Item -ItemType Directory -Force -Path $new | Out-Null
        Copy-Item -Path (Join-Path $src "*") -Destination $new -Recurse -Force
    } catch {
        Remove-Folder $new
        Fail "Copying the new version to $new failed: $($_.Exception.Message)"
    }
    $staged = Join-Path $new "pclink.exe"
}
$reported = ""
try { $reported = "$(& $staged version)".Trim() } catch { }
if ($LASTEXITCODE -ne 0 -or -not $reported) {
    if (-not $inPlace) { Remove-Folder $new }
    Fail "The new version does not run on this PC ($staged version failed)."
}
if (-not $script:NewVersion) { $script:NewVersion = $reported }

# What is in the data folder must have been written by an administrator: the service runs the
# configuration's 'commands:' entries as SYSTEM and trusts the database's settings.  This looks
# BEFORE anything of PC Link touches the folder (init-config, check-config and the service secure
# it, and take ownership of what stays in it): what somebody else put there before the first
# install is shown here while it still carries its owner.  When the folder is secured for the
# first time such entries are moved into a quarantine-<time> folder inside it and not used.
if (Test-Path -LiteralPath $dataDir -PathType Container) {
    try {
        $trusted = @("S-1-5-32-544", "S-1-5-18", [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
        try { $trusted += @(Get-LocalGroupMember -SID "S-1-5-32-544" -ErrorAction Stop | ForEach-Object { $_.SID.Value }) } catch { }
        $foreign = @(Get-ChildItem -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue | Where-Object {
            -not $_.FullName.Substring($dataDir.Length).TrimStart("\").StartsWith("quarantine-") -and $(
                try { (Get-Acl -LiteralPath $_.FullName).GetOwner([System.Security.Principal.SecurityIdentifier]).Value -notin $trusted }
                catch { $true })   # an owner that cannot be read is not an administrator's
        } | Select-Object -First 20)
        if ($foreign.Count -gt 0) {
            Write-Host "WARNING: these files in $dataDir belong to another account, not to an administrator:" -ForegroundColor Yellow
            $foreign | ForEach-Object { Write-Host "  $($_.FullName)" -ForegroundColor Yellow }
            Write-Host "A monitored user may have put them there before PC Link was installed. When PC Link secures the" -ForegroundColor Yellow
            Write-Host "folder for the first time it moves them into $dataDir\quarantine-<date> and does not use them;" -ForegroundColor Yellow
            Write-Host "in a folder that is secured already they are kept. Check them (config.yaml: especially 'commands:'" -ForegroundColor Yellow
            Write-Host "and 'apps:'; delete a pclink.db you do not know: it holds limits and bonus time)." -ForegroundColor Yellow
            if (-not $NoPause) { Read-Host "Press Enter to continue, or Ctrl+C to stop" | Out-Null }
        }
    } catch { }
}

# 2. Configuration: create it on the first run, with the connection to Home Assistant set up, and
#    stop so the users can be entered.
if (-not (Test-Path -LiteralPath $config)) {
    & $staged init-config | Out-Host
    $created = ($LASTEXITCODE -eq 0)
    if ($created) {
        & $staged setup-api --no-restart | Out-Null   # the token is shown at the end of the next run
        if ($LASTEXITCODE -ne 0) { Write-Host "Note: the connection to Home Assistant could not be set up; run `"$exe`" setup-api later." -ForegroundColor Yellow }
    }
    if (-not $inPlace) { Remove-Folder $new }
    if (-not $created) {
        Write-Host "The configuration could not be created (see above)." -ForegroundColor Red
        Finish 1
    }
    Write-Host ""
    Write-Host "A new configuration was created: $config" -ForegroundColor Yellow
    Write-Host "The connection to Home Assistant is already set up in it. Enter your users, save it,"
    Write-Host "then run the installer again."
    Start-Process notepad.exe -ArgumentList "`"$config`""
    Finish 0
}
& $staged check-config | Out-Host
if ($LASTEXITCODE -ne 0) {
    if (-not $inPlace) { Remove-Folder $new }
    Fail "The new version does not accept the configuration shown above. Fix it, then run the installer again."
}
# Home Assistant can reach the PC only through the local API: set it up when it is not (a setup that
# works is left exactly as it is - Home Assistant holds its token and certificate).
$apiNow = $null
try { $apiNow = (& $staged api-info) | ConvertFrom-Json } catch { }
if ($apiNow -and ($apiNow.PSObject.Properties.Name -contains "lan") -and -not $apiNow.lan) {
    Write-Host "Setting up the connection to Home Assistant (local API, token, HTTPS)..."
    & $staged setup-api --no-restart | Out-Null   # the token is shown at the end
    if ($LASTEXITCODE -ne 0) { Write-Host "Note: the connection to Home Assistant could not be set up; run `"$exe`" setup-api later." -ForegroundColor Yellow }
}

# 3. Stop the running copy and swap the folders.
$swapped = $false
try {
    Stop-PCLink
    if (-not $inPlace) {
        Remove-Folder $old
        if (Test-Path -LiteralPath $old) { throw "$old is in the way and could not be removed" }
        if (Test-Path -LiteralPath $dst) { Move-Folder $dst $old }
        try { Move-Folder $new $dst } catch {
            if (Test-Path -LiteralPath $old) { Move-Folder $old $dst }
            throw
        }
        $swapped = $true
        Write-Host "Installed in $dst"
    }

    # 4. Install the service, or point an existing one (also a Python-based one) at this copy, and start it.
    if (Get-Service -Name PCLink -ErrorAction SilentlyContinue) {
        & $exe update | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "updating the service registration failed" }
        if (-not $NoHarden) {
            & $exe harden | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "hardening the service failed" }
        }
    } else {
        if ($NoHarden) { & $exe install --no-harden | Out-Host } else { & $exe install | Out-Host }
        if ($LASTEXITCODE -ne 0) { throw "installing the service failed" }
    }
    Start-PCLink $exe $script:NewVersion
} catch {
    $why = $_.Exception.Message
    Write-Host "The new version could not be put into service: $why" -ForegroundColor Red
    $restored = $false
    if ($swapped -and (Test-Path -LiteralPath (Join-Path $old "pclink.exe"))) {
        # Put the previous version back and start it: time limits matter more than the update.
        Write-Host "Putting the previous version back..." -ForegroundColor Yellow
        try {
            Stop-PCLink
            $failed = "$dst.failed"
            Remove-Folder $failed
            Move-Folder $dst $failed
            Move-Folder $old $dst
            & $exe update | Out-Host
            Start-Service -Name PCLink
            (Get-Service -Name PCLink).WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
            Remove-Folder $failed
            $restored = $true
            Write-Host "The previous version ($($script:OldVersion)) is running again." -ForegroundColor Yellow
        } catch {
            Write-Host "Restoring the previous version failed as well: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "The PC Link service is NOT running. The previous files are in $old or $dst.failed." -ForegroundColor Red
        }
    } elseif (-not $swapped) {
        # nothing was replaced: the installed copy (if any) is untouched, start it again
        if (-not $inPlace) { Remove-Folder $new }
        if ($upgrade) {
            try { Start-Service -Name PCLink; $restored = $true } catch { Write-Host "The service could not be started again: $($_.Exception.Message)" -ForegroundColor Red }
        }
    }
    Fail "Update failed: $why" $restored
}
if (-not $inPlace) { Remove-Folder $old }

# 5. Firewall: the local API port for the Home Assistant integration, for this program only, and only
#    while the API is enabled for the LAN.  The settings come from the program itself (it reads the
#    configuration properly); when they cannot be read the existing rule is left alone.
$ruleName = "PC Link API"
$api = $null
try { $api = (& $exe api-info) | ConvertFrom-Json } catch { }
if ($api -and ($api.PSObject.Properties.Name -contains "enabled")) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if ($api.lan) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort ([int]$api.port) -Program $exe -Action Allow -Profile Private | Out-Null
        Write-Host "Firewall: allowed TCP $($api.port) for PC Link (the local API) on private networks."
    }
} else {
    Write-Host "Note: the API settings could not be read; the firewall rule was left as it is." -ForegroundColor Yellow
}

Write-Result $true "" $false
$version = (& $exe version)
Write-Host ""
Write-Host "Home Assistant PC Link $version is installed in $dst and running." -ForegroundColor Green
Write-Host "Commands: `"$exe`" help      Uninstall: run uninstall.cmd"
Write-Host ""
& $exe setup-api --show | Out-Host
Finish 0
