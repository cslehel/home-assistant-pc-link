# Home Assistant PC Link

A Python **Windows service** that connects a PC to **Home Assistant** over MQTT.
It enforces per-user screen-time limits (daily, weekly, bedtime, per app and
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
  weekdays", "Roblox only 16:00–19:00", "never TikTok"): the offending app or
  browser tab is closed, with a warning first.
- **Focus / homework mode**: for a period only allow-listed apps and sites are
  permitted (optionally on a schedule); homework time can be free of the budget.
- **Earn minutes**: reward buttons ("Homework done → +30 min") with a daily cap,
  and an add-minutes input for automations.
- **Parent PIN**: Home Assistant can only *loosen* limits after the PIN was
  entered (or passed inline in a command). Tightening never needs it.
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
screen use, per user, with a switch in Home Assistant.

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
user**, **launch / close apps** in a user's session, and run your own custom
commands (as SYSTEM or inside a user's desktop).

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
MQTT discovery for every entity, native **device triggers** ("Kevin ran out of
time", "left the seat", "is due for a break", ...), a virtual **"all PCs"**
roll-up device when several PCs share a broker (with optional **shared
budgets** across PCs), an optional **local REST/WebSocket API** with a
**setup/status page**, and **hot reload** of the configuration file. See
[docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) and [docs/API.md](docs/API.md).

### Tray icon
Each logged-in user gets a system-tray icon showing the connection state and
time left today, with tray balloon warnings. See
[docs/TRAY.md](docs/TRAY.md).

### Tamper resistance
The monitored (standard) user cannot stop, pause, disable or delete the service
to escape their limit; administrators keep full control. See
[docs/SECURITY.md](docs/SECURITY.md).

---

## How it fits together

```
Home Assistant  ─── MQTT ───►  PCLink service (LocalSystem, session 0)
   (entities,   ◄── MQTT ───   │  time accounting · rules · presence · login block
    triggers)                  │  telemetry · hardware sensors · power · discovery
   other PCs    ◄── MQTT ───►  │  fleet roll-up / shared budgets
   browser      ◄── HTTP ───►  │  optional local API + setup page
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and
[docs/MULTISEAT.md](docs/MULTISEAT.md) for ASTER specifics.

---

## Install (Windows)

```powershell
# 1. Install Python 3.11+ for all users, then:
pip install -r requirements-windows.txt

# 2. Create and edit the config
python -m pclink init-config
notepad %ProgramData%\PCLink\config.yaml     # set MQTT host + your users
python -m pclink check-config

# 3. Try it in the foreground first (Ctrl+C to stop)
python -m pclink run

# 4. Install as a service (auto-starts at boot; run from an elevated prompt).
#    Tamper resistance is applied automatically (see docs/SECURITY.md).
python -m pclink install
python -m pclink start
```

Manage it with `python -m pclink start|stop|restart|remove`. To uninstall,
`python -m pclink remove` (it restores the default service permissions first).

MQTT discovery creates a Home Assistant **device** with all entities
automatically — no YAML in Home Assistant required. See
[docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) for the entity list, example
dashboard cards and automations.

For antivirus / SmartScreen false positives and how to ship a signed build, see
[docs/ANTIVIRUS.md](docs/ANTIVIRUS.md).

---

## Configuration

A fully commented example is in [`config.example.yaml`](config.example.yaml).
The service hot-reloads the file when it changes. The essentials:

```yaml
mqtt:
  host: homeassistant.local
  username: pclink
  password: change-me

time_limits:
  parent_pin: "1234"           # HA may only grant time after this PIN
  rules:                       # for every managed user
    - {name: TikTok, sites: [tiktok.com], blocked: true}
  users:
    - name: kevin              # the Windows login name
      daily_limit_minutes: { weekdays: 90, weekend: 180 }
      weekly_limit_minutes: 600
      rollover_max_minutes: 30
      allowed_hours: 07:00-20:00
      bedtime: { weekdays: "20:30", weekend: "21:30" }
      bedtime_parent_extension_minutes: 60
      count_idle: false        # idle / away seats do not burn budget
      action: logoff
      rules:
        - {name: YouTube, sites: [youtube.com], daily_limit_minutes: 30}
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
pytest            # 150+ tests, run on any OS with in-memory fakes
```

The platform-independent core (time accounting, rules, presence, break
reminders, PIN gate, fleet roll-up, activity classification, config, store,
MQTT payloads, IPC, the local API) is fully unit-tested without Windows.

## License

MIT — see [LICENSE](LICENSE).
