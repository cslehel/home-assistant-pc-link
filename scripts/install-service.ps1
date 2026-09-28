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
