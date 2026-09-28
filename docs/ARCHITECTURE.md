# Architecture

## Processes

| Process | Runs as | Responsibility |
|---|---|---|
| **Service** (`PCLink`) | LocalSystem, session 0 | Time accounting, enforcement, login blocking, telemetry, hardware sensors, power, MQTT, agent supervision. |
| **Agent** (one per active seat) | the session's user | Foreground window, idle time, lock state, active browser URL, audio activity, tray icon. Reports to the service; holds no enforcement power. |

The service cannot observe a user's desktop from session 0, so it launches one
agent per active session via `CreateProcessAsUser` with that session's token
(`pclink/launcher.py`). Agents connect back over `127.0.0.1` and authenticate
with a one-time token the service generated for that launch
(`pclink/ipc.py`), so a process in one session cannot impersonate another.

## The main loop (`pclink/core.py`)

Every tick (default 5 s; faster in console mode):

1. Enumerate Windows sessions (`winsessions.py`) and merge each session's most
   recent agent report (idle, lock, foreground, URL, audio).
2. Supervise agents: launch for new active sessions, relaunch crashed ones with
   exponential backoff, drop ended sessions.
3. `TimeKeeper.tick()` accrues used time for counting sessions and returns
   **actions** (warn / logoff / lock / shutdown / block / unblock).
4. Execute actions (notify via agent tray or `WTSSendMessage`; log off / lock /
   shut down; apply or remove the login block).
5. Attribute activity to gaming / browsing / app / site categories and add to
   the per-user daily counters.
6. Reconcile the desired login-block state with the account's real state
   periodically (self-heals if something changed it).
7. Push tray status to agents; publish PC + per-user state to MQTT (only when
   changed, or at least once a minute).
8. Flush counters to SQLite every ~30 s; purge old data daily.

Windows backends are **injected**, so the whole loop runs against in-memory
fakes in the test suite on any OS (`tests/test_orchestrator.py`).

## Enforcement lives in the service, not the agent

All time decisions are made by `TimeKeeper` inside the SYSTEM service. The
agent only *reports* and *displays*. A user killing or tampering with their
agent gains nothing: the service still counts their active time (falling back
to `WTSSendMessage` for warnings) and still logs them off / blocks login.

## Performance

Designed to stay in the noise on CPU:

- psutil counters are read non-blocking (no `interval=` sleeps).
- Hardware sensors run on their own thread; slow SMART reads are throttled to
  every few minutes and never block the main loop.
- The agent samples cheap Win32 calls (`GetForegroundWindow`,
  `GetLastInputInfo`) on a 2 s poll and only walks UI Automation for the URL
  when the foreground window actually changes.
- MQTT payloads are published only on change (plus a 60 s heartbeat).
- Statistics are approximate by design: precision is never bought with CPU.

## Storage (`pclink/store.py`)

SQLite in `%ProgramData%\PCLink\pclink.db` (WAL mode). Daily per-user totals,
per-app and per-site seconds, HA-set overrides (limits, bonus, enable flags)
and an event log. Writes are batched in memory and flushed periodically.

## Files

```
pclink/
  __main__.py     CLI (run, install, harden, check-config, init-config, ...)
  service.py      pywin32 service wrapper + install-time hardening
  core.py         orchestrator / main loop
  timekeeper.py   time accounting + enforcement decisions (pure logic)
  activity.py     gaming/browsing/site classification (pure logic)
  store.py        SQLite persistence
  config.py       YAML schema + validation
  ha.py           MQTT connection + discovery
  entities.py     Home Assistant discovery payload builders
  telemetry.py    psutil metrics
  gpu.py          NVML / Windows perf-counter GPU backends
  hwsensors.py    LibreHardwareMonitor backends (DLL / HTTP)
  power.py        shutdown/reboot/sleep/hibernate + Wake-on-LAN
  winsessions.py  Terminal Services session enumeration + control
  logonblock.py   login prevention (logon hours / disable account)
  launcher.py     start agents in user sessions
  ipc.py          localhost socket transport (token-authenticated)
  hardening.py    service ACL + recovery (tamper resistance)
  agent/          per-session agent: win_activity, browser_url, tray, main
```
