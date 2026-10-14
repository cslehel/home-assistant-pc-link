# Home Assistant PC Link

A Python **Windows service** plus a **Home Assistant integration** that connect a
PC to **Home Assistant**. It enforces per-user screen-time limits (daily, weekly, bedtime, per app and
per website), blocks logins when time runs out, knows whether somebody is
really sitting at each seat, and publishes rich PC telemetry, hardware sensor
readings, power controls and activity statistics (gaming vs. browsing, per
website, now playing).

It is built for machines running **ASTER multiseat**, where several users are
logged in and active at the same time: every Windows session (seat) is tracked
and limited independently.

> **Platform:** the service runs on Windows 10/11. The code imports and its
> full test suite runs on any OS, but session control, login blocking and the
> tray agent are Windows-only.

---

## What it does

### Time control (per user, per seat)
- **Daily** budgets with different limits for weekdays / weekends / individual
  days, an optional **weekly** budget, and **rollover** of unused minutes (with
  a cap).
- **Allowed-hours** windows (e.g. only 07:00–20:00, wrapping past midnight) and
  a separate **bedtime** schedule that can be extended from Home Assistant and
  auto-extends while a parent is home.
- Warnings shown in the user's tray before time runs out, a grace countdown,
  then **log off**, **lock**, or **shut down** (configurable per user).
- **Login blocking** when out of time or outside allowed hours, via the
  account's logon-hours (default), the account-disabled flag, or logoff-only.
- **Per-app / per-website limits and blocklists** ("30 min of YouTube on
  weekdays", "five hours of games a week", "Roblox only 16:00–19:00", "never
  TikTok"): the offending app or browser tab is closed, with a warning first.
  Rules can be made and changed from Home Assistant as well as in the
  configuration file.
- **Focus / homework mode**: for a period only allow-listed apps and sites are
  permitted (optionally on a schedule); homework time can be free of the budget.
- **Earn minutes**: reward buttons ("Homework done → +30 min") with a daily cap,
  and an add-minutes input for automations.
- **Ask for more time**: the child asks from the tray icon's menu; the parent
  approves or denies from Home Assistant, for example from a phone
  notification.
- **Profiles**: named sets of limits, allowed hours and bedtimes ("holiday",
  "exams") to switch between from Home Assistant, per child or for everyone.
- **Weekly summary**: last week's total, minutes per day, top apps, games and
  websites, and how often time ran out.
- **Parent PIN**: Home Assistant can only *loosen* limits after the PIN was
  entered (or passed inline in a command). Tightening never needs it.
- Days, allowed hours and bedtimes are tied to a **fixed time zone**: the one
  set in `time_limits.time_zone` or, without it, the zone the service first
  ran in (remembered), so changing the PC's time zone moves no day boundary.
  After moving to another zone, set `time_limits.time_zone`.
- Everything is controllable live from Home Assistant, and all limits, usage,
  overrides and bonus minutes survive restarts (stored in SQLite).

### Presence without a webcam
Idle detection combines keyboard/mouse idle with **game-controller input**,
**audio playback**, **full-screen video / games that keep the display on**,
**now-playing** media, the **monitor's power state**, the **screensaver** and
Windows' own **user-presence** verdict into `active` / `passive` / `idle` /
`away` per seat. A child watching a film is not idle; a PC with its monitor
asleep is. See [docs/PRESENCE.md](docs/PRESENCE.md).

### Wellbeing
**Break reminders** (the 20-20-20 rule) as tray balloons after continuous
screen use, per user, with a switch in Home Assistant. A pause without input
of `duration_seconds` (20 s by default; a film playing does not count as a
pause) or the PC sleeping starts the count anew.

### PC telemetry
CPU / memory / swap / per-disk usage and free space, disk and network I/O
rates, GPU load / memory / temperature / power, battery, uptime, process count,
**top processes by CPU and memory**, IP and MAC, **USB devices and removable
drives**, **printer status**, **Windows Update status** (pending updates,
reboot required, check / install buttons), plus which users are logged in and
whether anyone is actively using the PC.

### Hardware sensors
CPU / GPU / motherboard temperatures, fan speeds, power and voltages via
**LibreHardwareMonitor** (in-process DLL, or its web server). Storage SMART
data is polled infrequently so it never costs anything noticeable.

### Power & remote control
Shut down, reboot, sleep, hibernate, cancel a pending shutdown, lock all
sessions, **Wake-on-LAN** (this PC can wake others), broadcast a message to
everyone, log off / lock / message an individual user, **volume and mute per
user**, **turn a seat's display off and on**, **launch / close apps** in a
user's session, and run your own custom commands (as SYSTEM or inside a
user's desktop).

### Activity statistics (lightweight)
Time spent **gaming** vs. **browsing**, the current app, website and
**now-playing** title, top apps, top websites and per-category time (video /
social / education / …), with a 7-day history. Reading the active browser tab
is done read-only through UI Automation, throttled to only run when the
foreground window changes.

### Privacy-gated screenshots
Optional, off by default: a parent can request a **screenshot** of a child's
screen from Home Assistant (a `camera` entity). PIN-gated, the child is told,
and parent accounts are never captured. See `screenshots:` in the config.

### Home Assistant integration
A **PC Link integration** for Home Assistant (HACS-installable, UI setup, the
PC is discovered on the LAN) that talks to the PC's **local REST/WebSocket
API** directly: every entity, a full set of **actions**
(`pclink.add_minutes`, `pclink.focus`, `pclink.set_limit`, `pclink.notify`,
...) with a device picker, native **device triggers** ("Kevin ran out of
time", "left the seat", "is due for a break", ...) and `pclink_event` bus
events. With several PCs there is a virtual **"all PCs"** roll-up device and
optional **shared budgets** across PCs (one limit for all of them, bonus
minutes and bedtime extensions included). The service also has a
**setup/status page** and **hot reload** of the configuration file. See
[docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) and [docs/API.md](docs/API.md).

### Tray icon
Each logged-in user gets a system-tray icon showing the connection state and
time left today, with tray balloon warnings. See
[docs/TRAY.md](docs/TRAY.md).

### Tamper resistance
The monitored (standard) user cannot stop, pause, disable or delete the service
to escape their limit; administrators keep full control. A **tamper warning**
per child tells Home Assistant when the agent was killed or went silent, sent
forged reports, or the PC's clock or time zone was changed, and a **service
problem** sensor reports when the service itself is not working properly. The
local API can be closed to the other users of the PC with a read-only token,
kept apart from configuration changes with an admin token and served over
HTTPS, and a code-signed copy only updates itself to a newer release signed by
the same publisher. See
[docs/SECURITY.md](docs/SECURITY.md).

---

## How it fits together

```
Home Assistant                 PCLink service (LocalSystem, session 0)
  PC Link integration ◄─ WS/REST ─►│  time accounting · rules · presence · login block
  (entities, actions,              │  telemetry · hardware sensors · power
   triggers, events,               │  entity catalog · local API + setup page
   all-PCs roll-up)                │  shared budgets (other PCs' usage via HA)
                                   │
                                   └─ localhost socket ─► per-session agents
                                    (one per ASTER seat, run as that user)
                                    foreground · idle · controller · display state
                                    audio/volume · URL · now playing · tray
                                    close app/tab · screenshot (on request)
```

The service runs as **LocalSystem** so it can query user tokens, log sessions
off and change account logon hours. It cannot see a user's desktop, so for each
active seat it launches a small **agent** in that user's session. The agent
reports activity over `127.0.0.1` (authenticated with a per-launch token) and
shows the tray icon. The agent has no enforcement power: killing it just makes
the service relaunch it, and time keeps counting either way.

**Home Assistant is a remote control, not a dependency.** Time counting,
warnings, log-off and login blocking all run locally in the service and are
persisted on the PC. If the network or Home Assistant goes down,
limits keep being enforced, entities show as *unavailable* instead of stale,
and everything resyncs automatically on reconnect. Commands and events from
during the outage are deliberately not replayed. Details in
[docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md#what-happens-when-the-connection-to-home-assistant-drops).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and
[docs/MULTISEAT.md](docs/MULTISEAT.md) for ASTER specifics.

---

## Install (Windows)

**Standalone version (recommended): no Python needed.**

1. Download `PCLink-<version>-windows-x64.zip` from the
   [Releases](https://github.com/cslehel/home-assistant-pc-link/releases) page.
   Before extracting, right-click the zip, choose **Properties**, tick
   **Unblock** and click OK.
2. Extract it and double-click **install.cmd**, then allow the administrator
   prompt. The first run creates `C:\ProgramData\PCLink\config.yaml` and opens
   it in Notepad.
3. Enable the local API (see below) and enter your users, save, and double-click
   **install.cmd** again. The program is copied to `C:\Program Files\PCLink`,
   the service is installed with tamper resistance (see
   [docs/SECURITY.md](docs/SECURITY.md)) and started.

Upgrade by running the new version's **install.cmd**; settings and statistics
are kept. Uninstall with **uninstall.cmd**, which gives every managed user their
logon rights back. Commands such as
`"C:\Program Files\PCLink\pclink.exe" check-config` are listed by
`pclink.exe help`.

**Python install (alternative, for development):**

```powershell
pip install -r requirements-windows.txt
python -m pclink init-config
python -m pclink setup-api                   # elevated: the API for Home Assistant (token + HTTPS)
notepad %ProgramData%\PCLink\config.yaml     # set your users
python -m pclink check-config
python -m pclink run                          # try it in the foreground (Ctrl+C to stop)
python -m pclink install                      # elevated prompt: install + tamper resistance
python -m pclink start
```

Both variants use the same configuration and data in `C:\ProgramData\PCLink`,
and the standalone installer can take over an existing Python-based service.
See [docs/INSTALL.md](docs/INSTALL.md) for details.

## Connect it to Home Assistant

Home Assistant reaches the PC through the **PC Link integration** and the
PC's local API. The standalone installer sets the API up (token, HTTPS, the
firewall rule) and shows the token at the end. With a Python install:

```powershell
# on the PC (elevated): API on, token and HTTPS written into config.yaml, service restarted
python -m pclink setup-api
New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort 47392 -Action Allow -Profile Private
```

In Home Assistant, install **PC Link** from HACS (add this repository as a
custom *Integration* repository) or copy `custom_components/pclink/` into
`config/custom_components/`, restart, then Settings → Devices & services: the
PC is listed as *Discovered*; enter the token. A device with every entity
appears, plus `pclink.*` actions and device triggers.

See [docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) for the setup, the entity
list, the actions, example dashboard cards and automations.

For antivirus / SmartScreen false positives and how to ship a signed build, see
[docs/ANTIVIRUS.md](docs/ANTIVIRUS.md).

---

## Configuration

A fully commented example is in [`config.example.yaml`](config.example.yaml).
The service hot-reloads the file when it changes. The essentials:

```yaml
api:                           # for the Home Assistant integration (pclink setup-api writes it)
  enabled: true
  host: 0.0.0.0
  token: change-me
  # read_token: other-secret   # read-only; with it, reading on the PC itself needs a token too

time_limits:
  parent_pin: "1234"           # HA may only grant time after this PIN
  # time_zone: Europe/Budapest # day boundaries in this zone, whatever the PC is set to
  rules:                       # for every managed user
    - {name: TikTok, sites: [tiktok.com], blocked: true}
  users:
    - name: kevin              # the Windows login name
      daily_limit_minutes: { weekdays: 90, weekend: 180 }
      weekly_limit_minutes: 600
      rollover_max_minutes: 30
      allowed_hours: 07:00-20:00
      bedtime: { weekdays: "20:30", weekend: "21:30" }
      wake_time: "07:00"       # the night ends here (06:00 when left out)
      bedtime_parent_extension_minutes: 60
      count_idle: false        # idle / away seats do not burn budget
      action: logoff
      rules:
        - {name: YouTube, sites: [youtube.com], daily_limit_minutes: 30, weekly_limit_minutes: 150}
      focus: { allow_apps: [winword.exe], allow_sites: [khanacademy.org] }
    - name: dad
      unlimited: true          # statistics only, no limit
```

---

## Roadmap

Everything from the original roadmap is implemented; see
[docs/ROADMAP.md](docs/ROADMAP.md) for where each feature lives and for ideas
that are still open.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest            # 292 service tests, run on any OS with in-memory fakes

# Home Assistant integration tests (needs the HA test harness; see docs/DEVELOPMENT.md)
pip install pytest-homeassistant-custom-component PyTurboJPEG tzdata
pytest tests_ha
```

The platform-independent core (time accounting, rules, presence, break
reminders, PIN gate, shared budgets, activity classification, config, store,
entity catalog, IPC, the local API, the update's signature
policy) is fully unit-tested without
Windows. The integration is tested against the newest Home Assistant release
and the oldest supported one (2024.3) on every push and every week (CI).

## License

MIT — see [LICENSE](LICENSE).
