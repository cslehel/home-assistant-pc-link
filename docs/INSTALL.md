# Installation & operation

## Requirements
- Windows 10 or 11 (64-bit).
- The **PC Link integration** in Home Assistant, which talks to the PC's local
  API (`api:` in the config, off by default). See
  [HOMEASSISTANT.md](HOMEASSISTANT.md).
- Optional: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  for CPU/GPU/motherboard temperatures and fan speeds. PC Link does not ship
  `LibreHardwareMonitorLib.dll`; it loads the one of your installation
  (`hardware_sensors.lhm_dll_path`) into the SYSTEM service. Use **0.9.5 or
  later**: earlier versions install the WinRing0 driver, which lets any local
  user read and write physical memory (CVE-2020-14979); newer ones use the
  PawnIO driver instead (install PawnIO from pawnio.eu if your sensors stay
  empty). The service log warns when it loads an older version.
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
- restricts `%ProgramData%\PCLink` (configuration, database, service log) to
  administrators and SYSTEM, see [SECURITY.md](SECURITY.md#the-data-folder);
- opens the firewall port when the local API is enabled for the LAN: for
  `pclink.exe` only, on the port `api.port` says, and removes the rule again
  when the API is switched off or bound to this PC only.

Because of that restriction, open `config.yaml` from an editor started **as
administrator** (for example search for Notepad, right-click, *Run as
administrator*, then File > Open). If the installer finds files in the data
folder that were not written by an administrator (a `config.yaml`, a
`pclink.db`) it lists them, before anything else touches the folder, and
waits. When the folder is secured for the first time such files are moved
into a `quarantine-<date>-<time>` folder inside it and not used; check them
(the configuration's `commands:` and `apps:`: the service runs `commands:` as
SYSTEM) before moving anything back, and delete a database you do not know. If `%ProgramData%\PCLink` turns out to be a
link or junction to another folder, the installer stops: remove the link
(`rmdir "%ProgramData%\PCLink"`) and run it again
([why](SECURITY.md#the-data-folder)).

Options, from PowerShell: `.\install.ps1 -NoHarden` skips tamper resistance,
`-InstallDir <folder>` installs elsewhere (keep it admin-only).

**Upgrading:** run the new version's `install.cmd`; configuration and
statistics are kept. It also converts an existing Python-based installation.
An upgrade does not leave the PC without a running service: the new version is
copied to `C:\Program Files\PCLink.new` and checked (it must start, and accept
your configuration) before anything is stopped; then the folders are swapped
and the service is started. If the new version does not start, or stops again
within a few seconds, the previous one is put back and started, and the
installer says so. What happened is also written to
`%ProgramData%\PCLink\install-result.json`, which the service shows on the
*PC Link update* entity (`last_install`).

**Uninstalling:** `uninstall.cmd` removes the service (restoring the logon
rights of every managed user first), the firewall rule and the program folder.
Configuration and statistics stay in `%ProgramData%\PCLink`; add `-Purge` when
running `uninstall.ps1` from PowerShell to delete them too.

**What is in the zip:** `pclink.exe` (command line and the service itself),
`pclink-agent.exe` (the per-user agent and tray icon, started by the service in
each session), the `_internal` folder with the bundled Python runtime and
libraries (including the optional ones: NVIDIA metrics, auto-discovery,
in-process LibreHardwareMonitor sensors, JPEG screenshots, now-playing from
Windows' media controls), the install and
uninstall scripts, and `defender-exclusion.ps1`. Run
`"C:\Program Files\PCLink\pclink.exe" selftest` to see which components are
available.

## Install from Python (alternative)

Needs Python 3.9+ (3.11+ recommended) installed **for all users**.
`requirements-windows.txt` includes `tzdata`, the time zone database that
`time_limits.time_zone` needs (Windows has none that Python can read).

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

The service then runs as SYSTEM from this checkout with this Python. Keep both
in folders that only administrators can change (not in a user's profile or a
synchronised folder): whoever can change those files runs code as SYSTEM. The
standalone version takes care of this by installing into `Program Files`.

## Operating the service

With the standalone version, use `"C:\Program Files\PCLink\pclink.exe"` in
place of `python -m pclink` below.

| Command | Effect |
|---|---|
| `python -m pclink start` / `stop` / `restart` | control the service (from an administrator prompt; otherwise "Access is denied") |
| `python -m pclink remove` | uninstall (restores default ACL first) |
| `python -m pclink harden` / `unharden` | toggle tamper resistance; says what worked and ends with an error when something did not (for example without an administrator prompt) |
| `python -m pclink check-config` | validate the config file |
| `python -m pclink pin-hash` | ask for the parent PIN and print a `parent_pin_hash` value for the config (`pin-hash 1234` also works, but leaves the PIN in the command history) |
| `python -m pclink make-cert` | create a certificate for HTTPS on the local API and print the `api:` lines and its fingerprint ([API.md](API.md#https)) |
| `python -m pclink unblock-all` | emergency: restore logon rights for all managed users |
| `python -m pclink setup-api` | set up the local API for the Home Assistant integration in `config.yaml` (on for the LAN, a token, HTTPS with a new certificate), restart the service and print the token; keeps an existing token and certificate; `--show` only prints them ([HOMEASSISTANT.md](HOMEASSISTANT.md#setup)) |
| `python -m pclink api-token` | print a random API token + the `api:` block for the Home Assistant integration |
| `python -m pclink selftest` | list which components (optional backends) are available |

Configuration changes are picked up automatically (hot reload); a few
settings need a restart (`storage`, `logging`, `hardware_sensors`, the
agent's IPC port, the `api` section including both tokens, `time_limits.block_method`, `device.id` /
`device.name`, switching `windows_update`, `updates` or `fleet` on or off; the
full list is in [API.md](API.md#hot-reload)). An invalid file is rejected and
the service keeps its previous configuration. That also holds across a
restart: the configuration the service starts with is copied to
`%ProgramData%\PCLink\config.last-good.yaml`, and when `config.yaml` does not
load at the start of the service (a typing error, a deleted file) the service
runs with that copy instead of not running at all. It says so loudly - an
error in the service log and in Windows' event log, and a `config_invalid`
entry in the event list - and reads `config.yaml` again as soon as it changes.
With `api.enabled: true` a local
status/setup page is available at `http://127.0.0.1:47392/` (see
[API.md](API.md)).

## Settings worth a look after installing

- **`time_limits.time_zone`** (for example `Europe/Budapest`): days, allowed
  hours and bedtimes are reckoned in this zone instead of the PC's own
  setting, which a standard user is allowed to change. `check-config` prints
  the zone in use. A new configuration (the installer's first run,
  `init-config`) comes with it filled in: the time zone the PC is in at that
  moment, which `init-config` also prints. Check it if the PC was set up
  somewhere else. When the zone cannot be worked out, the line stays commented
  out with a note to set it. An existing `config.yaml` is never changed: if
  yours has no `time_zone`, add it.
- **`api.read_token`**: without it every user logged in on the PC can read the
  local API (who is logged in, what the others are doing). With it, reading
  needs a token on the PC too; the setup page then asks for one.
- **`agent.display_control`** (on by default): gives every user a *display*
  switch in Home Assistant that turns the monitor of their seat off and on.

All three are described in `config.example.yaml`.

## Firewall (Home Assistant integration)

The integration connects to the PC on `api.port` (47392). Both installers make
the rule themselves (and bring it up to date when run again). By hand, from an
elevated PowerShell prompt - for the service's program only, so that no other
program on the PC can receive on that port:

```powershell
New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort 47392 -Program "C:\Program Files\PCLink\pclink.exe" -Action Allow -Profile Private
```

(A Python install runs in `pythonservice.exe`; `sc.exe qc PCLink` shows its
path. `scripts\install-service.ps1` stops with an error and makes no rule
when it cannot find that program: a rule for the port alone would let any
program on the PC that takes the port be reached from the network.)

mDNS discovery (so Home Assistant lists the PC automatically) uses UDP 5353,
which Windows already allows for its own network discovery on private networks.

## Optional extras (Python install only; the standalone version includes them)
- `Pillow` — JPEG screenshots (`screenshots.format: jpeg`); without it
  screenshots are PNG.
- the `winrt` packages — exact "now playing" information from Windows' media
  controls, including music or video in a background browser tab; without them
  the title is guessed from the foreground window's title only.

```powershell
pip install Pillow winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
```

`pclink selftest` lists them as `pillow` and `now-playing`, and `tzdata` for
the time zone database.

## Where things live
- Config: `%ProgramData%\PCLink\config.yaml`
- Database: `%ProgramData%\PCLink\pclink.db`
- Service log: `%ProgramData%\PCLink\logs\pclink.log`
- Screenshots (only with `screenshots.save_to_disk`): `%ProgramData%\PCLink\screenshots\`
- A downloaded update while it is being installed: `%ProgramData%\PCLink\update\`;
  the installer's output of the last update: `%ProgramData%\PCLink\update-install.log`;
  what it reported: `install-result.json`
- Who signed the installed copy (for checking updates): `%ProgramData%\PCLink\signer.json`
- The last configuration that loaded: `%ProgramData%\PCLink\config.last-good.yaml`
- With `pclink make-cert`: `api-cert.pem` and `api-key.pem` in the same folder

All of the above are readable by administrators only.

- Agent log, one per user: `%LOCALAPPDATA%\PCLink\logs\agent-<session>.log`
  (the agent runs as the user and cannot write to the data folder)

## Emergency recovery
If a user is wrongly locked out:

```powershell
python -m pclink unblock-all    # restores logon hours / re-enables accounts
```

or in Home Assistant toggle the **Time limits** switch off, or send
`{"action":"unblock","user":"<name>"}` to `pclink/<device_id>/cmd`.

## Updating
Standalone version: run the new version's `install.cmd`, or let the PC do it:

- **From Home Assistant:** the **PC Link update** entity shows the installed
  and the latest released version and has an *Install* button; **Check for PC
  Link update** looks for a new release at once.
- **Automatically:** set `auto_install: true`.

```yaml
updates:
  enabled: true              # look for new releases (every check_interval_hours)
  check_interval_hours: 12
  allow_install: true        # Home Assistant may start the update
  auto_install: false        # true: install new releases without being asked
  # include_prereleases: false
  # repository: cslehel/home-assistant-pc-link
  # github_token: ...        # only for a private repository
  # require_signature: true  # never install an unsigned update
  # trusted_signers: ["0123...ABCD"]   # only these signing certificates (thumbprints)
```

The PC asks GitHub for the latest release of `repository`, downloads its
`PCLink-<version>-windows-x64.zip`, checks it against the SHA-256 GitHub
publishes for the file, unpacks it into the administrators-only data folder,
checks that the package is the announced version and newer than the installed
one (its `VERSION` file), checks its code signature and runs the package's own
`install.ps1`. The service restarts during the
update (the PC is unavailable in Home Assistant for a moment); configuration
and statistics are kept. A release without a published checksum is not
installed automatically. Only the standalone build can replace itself; see
[SECURITY.md](SECURITY.md#self-update) for what is trusted.

If the new version does not start, the installer puts the previous one back
(see *Upgrading* above). The *PC Link update* entity then shows the failure
(`error`, and `last_install` with `rolled_back`), and that version is not
installed again automatically; pressing *Install* tries it once more. An
installer that never reports back counts as failed after 20 minutes.

**Signatures.** If the installed copy is code-signed, an update is only
installed when its executables and installer script are signed by the same
publisher and its signed catalog (`pclink.cat`) lists exactly the files that
were downloaded; nothing needs to
be configured for that. Who the publisher is, is written down at the first
start (`signer.json`); anything that cannot be read during the check is a
refusal. An unsigned copy accepts unsigned updates unless
`require_signature: true`. When an update is refused, the reason is in the
*PC Link update* entity's attributes (`error` and `signature`) and in the
service log; install that version by hand with its
`install.cmd` if you trust it. Details:
[SECURITY.md](SECURITY.md#code-signature-of-an-update).

Python install: the entity shows the versions but has no Install button; stop
the service, `git pull` (or reinstall the package), then start it again.
Config and database are preserved either way.

## Antivirus
See [ANTIVIRUS.md](ANTIVIRUS.md). If your antivirus flags the program, run
`defender-exclusion.ps1` from the zip as administrator, or add an exclusion
for `C:\Program Files\PCLink` in your antivirus product. For a Python install
the script also excludes the `python` found first on `PATH`, but only when
its folder can be changed by administrators alone; otherwise it warns and
leaves it out (an exclusion for a Python in a user's profile would cover
whatever that user puts there).
