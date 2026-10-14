# Convenience installer: install deps, create config, install + start the
# hardened service. Run from an ELEVATED PowerShell prompt in the repo root.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
#
# The service runs as SYSTEM from this checkout and this Python: keep both in
# folders that only administrators can change (not in a user's profile or a
# synchronised folder), or use the standalone build, which installs itself
# into Program Files.

$ErrorActionPreference = "Stop"
Write-Host "== Home Assistant PC Link installer ==" -ForegroundColor Cyan

$dataDir = Join-Path $env:ProgramData "PCLink"
$dataItem = Get-Item -LiteralPath $dataDir -Force -ErrorAction SilentlyContinue
if ($dataItem -and ($dataItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    Write-Host "$dataDir is a link or junction to another folder. PC Link did not create it; remove it (rmdir) and run this again." -ForegroundColor Red
    exit 1
}

python --version
python -m pip install -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not (Test-Path (Join-Path $dataDir "config.yaml"))) {
    python -m pclink init-config
    if ($LASTEXITCODE -ne 0) { exit 1 }
    python -m pclink setup-api --no-restart | Out-Null   # the connection to Home Assistant; the token is shown at the end
    Write-Host "Edit $dataDir\config.yaml (your users), then re-run this script." -ForegroundColor Yellow
    exit 0
}

python -m pclink check-config
if ($LASTEXITCODE -ne 0) { exit 1 }
# Home Assistant reaches the PC only through the local API: set it up when it is not (a working setup is kept)
$apiNow = $null
try { $apiNow = (python -m pclink api-info) | ConvertFrom-Json } catch { }
if ($apiNow -and ($apiNow.PSObject.Properties.Name -contains "lan") -and -not $apiNow.lan) {
    python -m pclink setup-api --no-restart | Out-Null
}
python -m pclink install
if ($LASTEXITCODE -ne 0) { Write-Host "Installing the service failed (see above)." -ForegroundColor Red; exit 1 }
python -m pclink start
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host "Installed and started. Manage with: python -m pclink stop|restart|remove" -ForegroundColor Green

# Home Assistant integration: open the API port while the API is enabled for the LAN, for the
# service's program only.  The settings come from PC Link itself, which reads the configuration
# properly.
$ruleName = "PC Link API"
$api = $null
try { $api = (python -m pclink api-info) | ConvertFrom-Json } catch { }
if ($api -and ($api.PSObject.Properties.Name -contains "enabled")) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if ($api.lan) {
        # the program the service manager runs: "C:\...\pythonservice.exe" (quoted) or a plain path
        $program = $null
        $svc = Get-CimInstance Win32_Service -Filter "Name='PCLink'" -ErrorAction SilentlyContinue
        if ($svc -and $svc.PathName -match '^\s*"([^"]+)"') { $program = $Matches[1] }
        elseif ($svc -and $svc.PathName -match '^\s*(\S+\.exe)') { $program = $Matches[1] }
        if (-not ($program -and (Test-Path -LiteralPath $program -PathType Leaf))) {
            # Never a rule for the port alone: any program on the PC could then take the port when the
            # service is not running and be reachable from the network.
            Write-Host "The service's program could not be found ($($svc.PathName)); no firewall rule was made." -ForegroundColor Red
            Write-Host "Home Assistant cannot reach this PC until there is one: run this script again." -ForegroundColor Red
            exit 1
        }
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort ([int]$api.port) -Program $program -Action Allow -Profile Private | Out-Null
        Write-Host "Firewall: allowed TCP $($api.port) for $program (PC Link API) on private networks." -ForegroundColor Green
    }
} else {
    Write-Host "Note: the API settings could not be read; the firewall rule was left as it is." -ForegroundColor Yellow
}

Write-Host ""
python -m pclink setup-api --show
