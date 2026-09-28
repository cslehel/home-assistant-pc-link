# Installation & operation

## Requirements
- Windows 10 or 11.
- Python 3.9+ (3.11+ recommended), installed **for all users**.
- Either the **PC Link integration** in Home Assistant (talks to the PC's local
  API; no broker) **or** an MQTT broker reachable from the PC (typically the
  Mosquitto add-on) with the MQTT integration enabled. See
  [HOMEASSISTANT.md](HOMEASSISTANT.md).
- Optional: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  for CPU/GPU/motherboard temperatures and fan speeds.
- Optional: `nvidia-ml-py` (in `requirements-windows.txt`) for NVIDIA GPU stats.

## Install

```powershell
pip install -r requirements-windows.txt
python -m pclink init-config
notepad %ProgramData%\PCLink\config.yaml
python -m pclink check-config
python -m pclink run            # foreground test; Ctrl+C to stop
```

Then, from an **elevated** prompt:

```powershell
python -m pclink install        # installs + hardens + sets auto-restart
python -m pclink start
```

Or run `scripts\install-service.ps1` which does the whole sequence.

## Operating the service

| Command | Effect |
|---|---|
| `python -m pclink start` / `stop` / `restart` | control the service (admin) |
| `python -m pclink remove` | uninstall (restores default ACL first) |
| `python -m pclink harden` / `unharden` | toggle tamper resistance |
| `python -m pclink check-config` | validate the config file |
| `python -m pclink pin-hash 1234` | print a `parent_pin_sha256` value for the config |
| `python -m pclink unblock-all` | emergency: restore logon rights for all managed users |
| `python -m pclink test-mqtt` | verify broker connectivity |
| `python -m pclink api-token` | print a random API token + the `api:` block for the Home Assistant integration |

Configuration changes are picked up automatically (hot reload); only `mqtt`,
`storage`, `logging`, the agent's IPC port and the `api` section need a
restart. With `api.enabled: true` a local status/setup page is available at
`http://127.0.0.1:47392/` (see [API.md](API.md)).

## Firewall (Home Assistant integration)

The integration connects to the PC on `api.port` (47392). Allow it once, from
an elevated PowerShell prompt:

```powershell
New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort 47392 -Action Allow -Profile Private
```

mDNS discovery (so Home Assistant lists the PC automatically) uses UDP 5353,
which Windows already allows for its own network discovery on private networks.

## Optional extras
- `Pillow` — JPEG screenshots (`screenshots.format: jpeg`); without it
  screenshots are PNG.
- `winsdk` — exact "now playing" information from Windows' media controls;
  without it the title is read from the player's window title.

```powershell
pip install Pillow winsdk
```

## Where things live
- Config: `%ProgramData%\PCLink\config.yaml`
- Database: `%ProgramData%\PCLink\pclink.db`
- Logs: `%ProgramData%\PCLink\logs\` (service and per-session `agent-*.log`)
- Screenshots (only with `screenshots.save_to_disk`): `%ProgramData%\PCLink\screenshots\`

## Emergency recovery
If a user is wrongly locked out:

```powershell
python -m pclink unblock-all    # restores logon hours / re-enables accounts
```

or in Home Assistant toggle the **Time limits** switch off, or send
`{"action":"unblock","user":"<name>"}` to `pclink/<device_id>/cmd`.

## Updating
Stop the service, `git pull` (or reinstall the package), then start it again.
Config and database are preserved.

## Antivirus
See [ANTIVIRUS.md](ANTIVIRUS.md). For a home install, running from a normal
Python installation plus a Defender exclusion for the install folder is the
smoothest path.
