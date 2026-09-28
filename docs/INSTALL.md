# Installation & operation

## Requirements
- Windows 10 or 11 (64-bit).
- Either the **PC Link integration** in Home Assistant (talks to the PC's local
  API; no broker) **or** an MQTT broker reachable from the PC (typically the
  Mosquitto add-on) with the MQTT integration enabled. See
  [HOMEASSISTANT.md](HOMEASSISTANT.md).
- Optional: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  for CPU/GPU/motherboard temperatures and fan speeds.
- Python is **not** needed with the standalone version.

## Install the standalone version (recommended)

1. Download `PCLink-<version>-windows-x64.zip` from the
   [Releases](https://github.com/cslehel/home-assistant-pc-link/releases) page.
   Right-click it, choose **Properties**, tick **Unblock**, click OK, then
   extract it.
2. Double-click **install.cmd** and allow the administrator prompt. The first
   run creates `%ProgramData%\PCLink\config.yaml` and opens it in Notepad.
3. Fill in the configuration, save, and double-click **install.cmd** again.

The installer (`install.ps1`, which `install.cmd` runs):

- copies the program to `C:\Program Files\PCLink`. The service must run from a
  folder only administrators can change, otherwise a standard user could swap
  the executable;
- validates the configuration and stops if it has errors;
- installs the service with tamper resistance and auto-restart, and starts it;
- opens the firewall port when the local API is enabled for the LAN.

Options, from PowerShell: `.\install.ps1 -NoHarden` skips tamper resistance,
`-InstallDir <folder>` installs elsewhere (keep it admin-only).

**Upgrading:** run the new version's `install.cmd`. It stops the service,
replaces the program files, and starts it again; configuration and statistics
are kept. It also converts an existing Python-based installation.

**Uninstalling:** `uninstall.cmd` removes the service (restoring the logon
rights of every managed user first), the firewall rule and the program folder.
Configuration and statistics stay in `%ProgramData%\PCLink`; add `-Purge` when
running `uninstall.ps1` from PowerShell to delete them too.

**What is in the zip:** `pclink.exe` (command line and the service itself),
`pclink-agent.exe` (the per-user agent and tray icon, started by the service in
each session), the `_internal` folder with the bundled Python runtime and
libraries (including the optional ones: NVIDIA metrics, auto-discovery,
in-process LibreHardwareMonitor sensors, JPEG screenshots), the install and
uninstall scripts, and `defender-exclusion.ps1`. Run
`"C:\Program Files\PCLink\pclink.exe" selftest` to see which components are
available.

## Install from Python (alternative)

Needs Python 3.9+ (3.11+ recommended) installed **for all users**.

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

With the standalone version, use `"C:\Program Files\PCLink\pclink.exe"` in
place of `python -m pclink` below.

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
| `python -m pclink selftest` | list which components (optional backends) are available |

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

## Optional extras (Python install only; the standalone version includes Pillow)
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
Standalone version: run the new version's `install.cmd`. Python install: stop
the service, `git pull` (or reinstall the package), then start it again.
Config and database are preserved either way.

## Antivirus
See [ANTIVIRUS.md](ANTIVIRUS.md). If your antivirus flags the program, run
`defender-exclusion.ps1` from the zip as administrator, or add an exclusion
for `C:\Program Files\PCLink` in your antivirus product.
