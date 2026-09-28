# Convenience installer: install deps, create config, install + start the
# hardened service. Run from an ELEVATED PowerShell prompt in the repo root.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1

$ErrorActionPreference = "Stop"
Write-Host "== Home Assistant PC Link installer ==" -ForegroundColor Cyan

python --version
python -m pip install -r requirements-windows.txt

if (-not (Test-Path "$env:ProgramData\PCLink\config.yaml")) {
    python -m pclink init-config
    Write-Host "Edit $env:ProgramData\PCLink\config.yaml (MQTT host + your users), then re-run this script." -ForegroundColor Yellow
    exit 0
}

python -m pclink check-config
python -m pclink install
python -m pclink start
Write-Host "Installed and started. Manage with: python -m pclink stop|restart|remove" -ForegroundColor Green

# Home Assistant integration: open the API port if the API is enabled for the LAN.
$cfg = Get-Content "$env:ProgramData\PCLink\config.yaml" -Raw
if ($cfg -match "(?ms)^api:\s*\n(?:[ \t]+.*\n)*?[ \t]+enabled:\s*true" -and $cfg -notmatch "(?m)^[ \t]+host:\s*127\.0\.0\.1") {
    $port = 47392
    if ($cfg -match "(?m)^[ \t]+port:\s*(\d+)") { $port = [int]$Matches[1] }
    if (-not (Get-NetFirewallRule -DisplayName "PC Link API" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null
        Write-Host "Firewall: allowed TCP $port (PC Link API) on private networks." -ForegroundColor Green
    }
}
