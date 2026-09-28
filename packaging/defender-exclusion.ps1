# Add Microsoft Defender exclusions for Home Assistant PC Link.
# Run as Administrator, ON A MACHINE YOU OWN, once. This reduces false
# positives for the legitimate behaviours PC Link performs (see docs/ANTIVIRUS.md).
#
#   powershell -ExecutionPolicy Bypass -File .\defender-exclusion.ps1

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:ProgramFiles "PCLink"      # standalone build (install.ps1)
$dataDir    = Join-Path $env:ProgramData "PCLink"       # configuration, statistics, logs

Write-Host "Adding Defender exclusions..."
if (Test-Path $installDir) {
    Add-MpPreference -ExclusionPath $installDir
    Add-MpPreference -ExclusionProcess (Join-Path $installDir "pclink.exe")
    Add-MpPreference -ExclusionProcess (Join-Path $installDir "pclink-agent.exe")
}
Add-MpPreference -ExclusionPath $dataDir

# Python install instead of the standalone build: exclude the interpreter that runs the service.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($python -and -not (Test-Path (Join-Path $installDir "pclink.exe"))) {
    Add-MpPreference -ExclusionProcess $python
    Add-MpPreference -ExclusionProcess ($python -replace "python.exe", "pythonw.exe")
}
Write-Host "Done. Current path exclusions:"
(Get-MpPreference).ExclusionPath
