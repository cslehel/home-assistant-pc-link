# Architecture

## Processes

| Process | Runs as | Responsibility |
|---|---|---|
| **Service** (`PCLink`) | LocalSystem, session 0 | Time accounting, rules, enforcement, login blocking, telemetry, hardware sensors, Windows Update / devices, power, MQTT, local API, agent supervision. |
| **Agent** (one per active seat) | the session's user | Foreground window, idle time, lock state, presence signals (controller, display state, screensaver, media), active browser URL, audio / volume, tray icon, and a few harmless actions on request (close an app or tab, set volume, take a screenshot). Holds no enforcement power. |

The service cannot observe a user's desktop from session 0, so it launches one
agent per active session via `CreateProcessAsUser` with that session's token
(`pclink/launcher.py`). Agents connect back over `127.0.0.1` and authenticate
with a one-time token the service generated for that launch
(`pclink/ipc.py`), so a process in one session cannot impersonate another.

## The main loop (`pclink/core.py`)

Every tick (default 5 s; faster in console mode):

1. Check `config.yaml` for changes (hot reload) and drain queued commands
   (MQTT, API, agent replies).
2. Enumerate Windows sessions (`winsessions.py`), merge each session's most
   recent agent report and work out its **presence** (`presence.py`:
   active / passive / idle / away).
3. Supervise agents: launch for new active sessions, relaunch crashed ones with
   exponential backoff, drop ended sessions.
4. `TimeKeeper.tick()` accrues used time for counting sessions (presence-aware)
   against the daily, weekly, allowed-hours and bedtime budgets and returns
   **actions** (warn / logoff / lock / shutdown / block / unblock).
5. Execute actions (notify via agent tray or `WTSSendMessage`; log off / lock /
   shut down; apply or remove the login block) and emit device-trigger events.
6. Attribute activity to gaming / browsing / app / site categories and to the
   per-app **rules**; `RuleEngine.tick()` decides whether a limit, blocklist
   entry or **focus mode** requires closing the current app or tab.
7. **Break reminders** (`wellbeing.py`) and transition events (logged in/off,
   presence away/back, focus started/ended).
8. Reconcile the desired login-block state with the account's real state
   periodically (self-heals if something changed it).
9. Push tray status to agents; publish PC + per-user state to MQTT (only when
   changed, or at least once a minute); publish the fleet roll-up when this PC
   is the leader; push state to WebSocket clients of the local API.
10. Flush counters to SQLite every ~30 s; purge old data daily.

Windows backends are **injected**, so the whole loop runs against in-memory
fakes in the test suite on any OS (`tests/test_orchestrator.py`).

## Enforcement lives in the service, not the agent

All time and rule decisions are made by `TimeKeeper` / `RuleEngine` inside the
SYSTEM service. The agent only *reports*, *displays* and carries out small
requests inside the user's own desktop (closing the user's own process or tab,
volume, screenshot). A user killing or tampering with their agent gains
nothing: the service still counts their active time (falling back to the
Terminal Services idle timer and `WTSSendMessage` for warnings) and still logs
them off / blocks login. Grants from Home Assistant can additionally be gated
behind a **parent PIN** (`pin.py`).

## Multi-PC

Every PC subscribes to the other PCs' retained availability and user-state
topics under the same `base_topic` (`fleet.py`). That gives shared budgets
(`time_limits.shared_budget`) and a roll-up device that exactly one PC (the
online peer with the smallest device id) publishes, so nothing is ever
published twice.

## Performance

Designed to stay in the noise on CPU:

- psutil counters are read non-blocking (no `interval=` sleeps).
- Hardware sensors, Windows Update searches and device enumeration run on their
  own threads / slow intervals and never block the main loop.
- The agent samples cheap Win32 calls (`GetForegroundWindow`,
  `GetLastInputInfo`, XInput state, `SHQueryUserNotificationState`) on a 2 s
  poll, receives display-state changes as notifications rather than polling,
  and only walks UI Automation for the URL when the foreground window changes.
- MQTT payloads are published only on change (plus a 60 s heartbeat).
- Statistics are approximate by design: precision is never bought with CPU.

## Storage (`pclink/store.py`)

SQLite in `%ProgramData%\PCLink\pclink.db` (WAL mode). Daily per-user totals,
per-app, per-site and per-rule seconds, HA-set overrides (limits, bonus,
rollover, bedtime extensions, focus timers, reward claims, enable flags) and an
event log. Writes are batched in memory and flushed periodically.

## Files

```
pclink/
  __main__.py     CLI (run, install, harden, check-config, init-config, pin-hash, ...)
  service.py      pywin32 service wrapper + install-time hardening
  core.py         orchestrator / main loop
  timekeeper.py   daily/weekly/bedtime accounting + enforcement decisions (pure logic)
  rules.py        per-app / per-site limits, blocklists, focus mode (pure logic)
  presence.py     active / passive / idle / away from the agent's signals (pure logic)
  wellbeing.py    break reminders (pure logic)
  pin.py          parent PIN gate (pure logic)
  fleet.py        multi-PC roll-up + shared budgets (pure logic)
  activity.py     gaming/browsing/site classification (pure logic)
  store.py        SQLite persistence
  config.py       YAML schema + validation
  ha.py           MQTT connection + discovery + peer subscriptions
  entities.py     Home Assistant discovery payload builders (incl. device triggers, camera, fleet)
  api.py          optional local REST / WebSocket API
  webui.py        the setup / status page served by the API
  telemetry.py    psutil metrics + top processes
  devices.py      USB devices, removable drives, printers
  winupdate.py    Windows Update status + triggers
  gpu.py          NVML / Windows perf-counter GPU backends
  hwsensors.py    LibreHardwareMonitor backends (DLL / HTTP)
  power.py        shutdown/reboot/sleep/hibernate + Wake-on-LAN
  winsessions.py  Terminal Services session enumeration + control
  logonblock.py   login prevention (logon hours / disable account)
  launcher.py     start agents in user sessions
  ipc.py          localhost socket transport (token-authenticated)
  hardening.py    service ACL + recovery (tamper resistance)
  agent/          per-session agent: win_activity, win_presence, coreaudio, media,
                  capture, browser_url, tray, main
```
