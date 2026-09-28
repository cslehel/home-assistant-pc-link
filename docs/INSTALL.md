# Installation & operation

## Requirements
- Windows 10 or 11.
- Python 3.9+ (3.11+ recommended), installed **for all users**.
- An MQTT broker reachable from the PC (typically the Mosquitto add-on on your
  Home Assistant host) and the MQTT integration enabled in Home Assistant.
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
| `python -m pclink unblock-all` | emergency: restore logon rights for all managed users |
| `python -m pclink test-mqtt` | verify broker connectivity |

## Where things live
- Config: `%ProgramData%\PCLink\config.yaml`
- Database: `%ProgramData%\PCLink\pclink.db`
- Logs: `%ProgramData%\PCLink\logs\` (service and per-session `agent-*.log`)

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
