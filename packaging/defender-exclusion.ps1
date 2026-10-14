# Add Microsoft Defender exclusions for Home Assistant PC Link.
# Run as Administrator, ON A MACHINE YOU OWN, once. This reduces false
# positives for the legitimate behaviours PC Link performs (see docs/ANTIVIRUS.md).
#
#   powershell -ExecutionPolicy Bypass -File .\defender-exclusion.ps1

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:ProgramFiles "PCLink"      # standalone build (install.ps1)
$dataDir    = Join-Path $env:ProgramData "PCLink"       # configuration, statistics, logs

# Accounts that may change a program Defender is told to leave alone: LocalSystem, Administrators,
# TrustedInstaller, and CREATOR OWNER (which only ever applies to what an account created itself).
$trusted = @("S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
$write  = [Security.AccessControl.FileSystemRights]"WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership"
$rename = [Security.AccessControl.FileSystemRights]"DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership"
$generic = 0x10000000 -bor 0x40000000   # GENERIC_ALL, GENERIC_WRITE (as they appear in some ACEs)

# Can an account other than an administrator change $Path?  $Rights: what counts as a change.
function Test-OthersCanWrite([string]$Path, $Rights) {
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { return $true }   # the owner can always rewrite the ACL
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne "Allow") { continue }
        if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }   # for what is below, not this
        if ($rule.IdentityReference.Value -in $trusted) { continue }
        if (([int64]$rule.FileSystemRights -band ([int64]$Rights -bor $generic)) -ne 0) { return $true }
    }
    return $false
}

# Excluding a program in a folder that a standard user can change would exclude whatever that user puts there.
function Test-AdminOnly([string]$Exe) {
    try {
        if (Test-OthersCanWrite $Exe $write) { return $false }
        $dir = Split-Path -Parent $Exe
        if (Test-OthersCanWrite $dir $write) { return $false }
        # the folders above: nobody else may rename or replace the folder
        $up = Split-Path -Parent $dir
        while ($up) {
            if (Test-OthersCanWrite $up $rename) { return $false }
            $next = Split-Path -Parent $up
            if ($next -eq $up) { break }
            $up = $next
        }
        return $true
    } catch {
        return $false   # what cannot be read is not known to be safe
    }
}

Write-Host "Adding Defender exclusions..."
if (Test-Path $installDir) {
    Add-MpPreference -ExclusionPath $installDir
    Add-MpPreference -ExclusionProcess (Join-Path $installDir "pclink.exe")
    Add-MpPreference -ExclusionProcess (Join-Path $installDir "pclink-agent.exe")
}
Add-MpPreference -ExclusionPath $dataDir

# Python install instead of the standalone build: exclude the interpreter that runs the service -
# but only one that only administrators can change (whichever python comes first on PATH could be
# one in a user's profile).
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($python -and -not (Test-Path (Join-Path $installDir "pclink.exe"))) {
    if (Test-AdminOnly $python) {
        Add-MpPreference -ExclusionProcess $python
        $pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
        if ((Test-Path -LiteralPath $pythonw) -and (Test-AdminOnly $pythonw)) { Add-MpPreference -ExclusionProcess $pythonw }
    } else {
        Write-Host "WARNING: $python is in a folder that accounts other than administrators can change;" -ForegroundColor Yellow
        Write-Host "no exclusion was added for it. Install Python for all users (in Program Files) to exclude it." -ForegroundColor Yellow
    }
}
Write-Host "Done. Current path exclusions:"
(Get-MpPreference).ExclusionPath
