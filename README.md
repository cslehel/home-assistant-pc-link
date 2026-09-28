# Home Assistant PC Link

A Python **Windows service** that connects a PC to **Home Assistant** over MQTT.
It enforces per-user screen-time limits, blocks logins when time runs out, and
publishes rich PC telemetry, hardware sensor readings, power controls and
activity statistics (gaming vs. browsing, per website).

It is built for machines running **ASTER multiseat**, where several users are
logged in and active at the same time: every Windows session (seat) is tracked
and limited independently.

> **Platform:** the service runs on Windows 10/11. The code imports and its
> full test suite runs on any OS, but session control, login blocking and the
> tray agent are Windows-only.

---

## What it does

### Time control (per user, per seat)
- Daily time budgets per user, with different limits for weekdays / weekends /
  individual days.
- Allowed-hours windows (e.g. only 07:00–20:00), including windows that wrap
  past midnight.
- Warnings shown in the user's tray before time runs out, a grace countdown,
  then **log off**, **lock**, or **shut down** (configurable per user).
- **Login blocking** when out of time or outside allowed hours, via the
  account's logon-hours (default), the account-disabled flag, or logoff-only.
- Idle time optionally does not consume the budget; audio playback can keep a
  session counted as active (so a movie doesn't look idle).
- Everything is controllable live from Home Assistant: enable/disable limits,
  set today's / weekday / weekend limits, grant bonus minutes, pause access.
- Limits, usage and bonus minutes survive restarts (stored in SQLite).

### PC telemetry
CPU / memory / swap / per-disk usage and free space, disk and network I/O
rates, GPU load / memory / temperature / power, battery, uptime, process count,
IP and MAC, plus which users are logged in and whether anyone is actively using
the PC.

### Hardware sensors
CPU / GPU / motherboard temperatures, fan speeds, power and voltages via
**LibreHardwareMonitor** (in-process DLL, or its web server). Storage SMART
data is polled infrequently so it never costs anything noticeable.

### Power & remote control
Shut down, reboot, sleep, hibernate, cancel a pending shutdown, lock all
sessions, **Wake-on-LAN** (this PC can wake others), broadcast a message to
everyone, log off / lock / message an individual user, and run your own custom
commands (as SYSTEM or inside a user's desktop).

### Activity statistics (lightweight)
Time spent **gaming** vs. **browsing**, the current app and website, top apps,
top websites and per-category time (video / social / education / …), with a
7-day history. Reading the active browser tab is done read-only through UI
Automation, throttled to only run when the foreground window changes.

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
   (entities)   ◄── MQTT ───   │  time accounting · login block · telemetry
                               │  hardware sensors · power · MQTT discovery
                               │
                               └─ localhost socket ─► per-session agents
                                    (one per ASTER seat, run as that user)
                                    foreground window · idle · lock · URL · tray
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
The essentials:

```yaml
mqtt:
  host: homeassistant.local
  username: pclink
  password: change-me

time_limits:
  users:
    - name: kevin              # the Windows login name
      daily_limit_minutes: { weekdays: 90, weekend: 180 }
      allowed_hours: 07:00-20:00
      action: logoff
    - name: dad
      unlimited: true          # statistics only, no limit
```

---

## Suggested additional features

Ideas that fit this architecture, roughly by effort. Tracked in
[docs/ROADMAP.md](docs/ROADMAP.md).

**Time & rules**
- Weekly (not just daily) budgets and rollover of unused minutes.
- Per-app and per-website limits and blocklists (e.g. 30 min of YouTube/day),
  reusing the activity tracker already in place.
- "Focus/homework mode": only allowlisted apps/sites for a period.
- Bedtime schedule separate from daily budget; auto-extend when a parent is
  home (HA presence).
- "Earn time" hooks: HA gives bonus minutes when a chore/task is marked done.
- Require a parent PIN (an HA `text` entity) to grant extra time.

**Presence & wellbeing**
- Break reminders (the "20-20-20" rule) via tray balloons.
- Webcam-free presence: mark a seat idle when its monitor sleeps.

**Telemetry & control**
- Per-process CPU/RAM top-N sensor (already scaffolded, off by default).
- Now-playing media, connected USB devices, printer status.
- Screenshot-on-demand (privacy-gated, parent-only) for young children.
- Windows Update / pending-reboot status; trigger update installs.
- Volume control and mute as HA `number`/`switch`.
- Launch/close specific apps from HA.

**Integration & robustness**
- Native Home Assistant **Device Triggers** (e.g. "Kevin ran out of time").
- Optional local REST/WebSocket API in addition to MQTT.
- Config reload without a restart; a small local web page for setup.
- Multi-PC roll-up dashboard (each PC is already its own HA device).

---

## Development

```bash
pip install -r requirements-dev.txt
pytest            # 70+ tests, run on any OS with in-memory fakes
```

The platform-independent core (time accounting, activity classification,
config, store, MQTT payloads, IPC) is fully unit-tested without Windows.

## License

MIT — see [LICENSE](LICENSE).
