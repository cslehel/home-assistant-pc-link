# Add a Microsoft Defender exclusion for the PCLink install directory.
# Run as Administrator, ON A MACHINE YOU OWN, once. This reduces false
# positives for the legitimate behaviors PCLink performs (see docs/ANTIVIRUS.md).
#
#   powershell -ExecutionPolicy Bypass -File packaging\defender-exclusion.ps1

$ErrorActionPreference = "Stop"

# Adjust if you installed elsewhere.
$installDir = "C:\Program Files\PCLink"
$dataDir    = "$env:ProgramData\PCLink"

# The Python that runs the service (edit if you use a venv).
$python = (Get-Command python -ErrorAction SilentlyContinue).Source

Write-Host "Adding Defender exclusions..."
if (Test-Path $installDir) { Add-MpPreference -ExclusionPath $installDir }
Add-MpPreference -ExclusionPath $dataDir
if ($python) {
    Add-MpPreference -ExclusionProcess $python
    Add-MpPreference -ExclusionProcess ($python -replace "python.exe","pythonw.exe")
}
Write-Host "Done. Current path exclusions:"
(Get-MpPreference).ExclusionPath
