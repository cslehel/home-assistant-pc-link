# Architecture

## Processes

| Process | Runs as | Responsibility |
|---|---|---|
| **Service** (`PCLink`) | LocalSystem, session 0 | Time accounting, rules, enforcement, login blocking, telemetry, hardware sensors, Windows Update / devices, power, local API, agent supervision. |
| **Agent** (one per active seat) | the session's user | Foreground window, idle time, lock state, presence signals (controller, display state, screensaver, media), active browser URL, audio / volume, tray icon, and a few harmless actions on request (close an app or tab, set volume, turn the display off or on, take a screenshot). Holds no enforcement power. |

The service cannot observe a user's desktop from session 0, so it launches one
agent per active session via `CreateProcessAsUser` with that session's token
(`pclink/launcher.py`). Agents connect back over `127.0.0.1` and authenticate
with a one-time token the service generated for that launch
(`pclink/ipc.py`), so a process in one session cannot impersonate another.
The authentication is mutual and the token never travels: the agent sends a
nonce, the service answers with its own nonce and an HMAC of both made with
the token, and only after checking it does the agent answer with its HMAC.
An agent therefore obeys nothing (`run`, `close`, `screenshot`, `lock`) from a
program that took the port while the service was not holding it, and learns
it nothing it could replay. An agent of an older version (which sent the
token itself) is rejected, exits and is replaced by a current one. Each agent
also watches the service process that started it and exits when it ends.
Requests from the service run on a command thread of the agent, never on the
thread reading the connection; every `close` request gets a `closed` answer
(`failed bad request` when it could not be read).

## The main loop (`pclink/core.py`)

Every tick (default 5 s; faster in console mode):

1. Check `config.yaml` for changes (hot reload) and drain queued commands
   (API, agent replies).
2. Enumerate Windows sessions (`winsessions.py`), merge each session's most
   recent agent report and work out its **presence** (`presence.py`:
   active / passive / idle / away).
3. Supervise agents: launch for new active sessions, relaunch crashed ones with
   exponential backoff, replace one that is still running but has been silent
   for `agent.watchdog_seconds`, end the agent of a session that ended.
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
9. Push tray status to agents; push state to WebSocket clients of the local
   API.
10. Flush counters to SQLite every ~30 s; purge old data daily.

Every step runs on its own: one that raises is logged and counted, and the rest
of the tick - above all counting time, enforcing and the flush, which happens
whatever went before - still runs. Three ticks in a row with a failed step, or
a part that could not start (the agent port, the API; each is tried again
every minute), make the PC payload say `degraded` (with `tick_failures`,
`last_good_tick` and the details under `health`) and raise the `degraded`
event.

Two clocks are kept apart: the wall clock decides which day and hour it is,
the monotonic clock answers every "has it been N seconds" (publish intervals,
agent backoff and watchdog, block reassert, config polling), so setting the
PC's clock stalls nothing. Within a tick a user's `TimeStatus` and state
payload are worked out once and shared by all steps. The full state goes to
the WebSocket clients every `api.push_interval_seconds` (5 s by default), and
on the next tick after a parent PIN entry, the "parent at home" switch or a
configuration reload.

### Asking for more time, profiles, the weekly summary

- **Time requests.** The tray status tells an agent which amounts can be asked
  for (`time_limits.time_requests`); the agent sends `time_request`. The service
  takes one pending request per managed user (cooldown, a daily maximum, an
  expiry), shows it in the user payload (`time_request`,
  `time_request_minutes`) and raises `time_requested`. The answer is the JSON
  action `time_request_answer` (or the approve / deny buttons): approving is a
  grant (parent PIN) and adds bonus minutes. Every outcome - also a refused
  request - is told to the child with a notification.
- **Profiles.** `TimeKeeper.policy_for` returns the policy with the active
  profile applied; the orchestrator offers a `select` per user that has
  profiles and the JSON action `set_profile` (without a user: everybody who has
  that profile). A switch that gives more needs the parent PIN;
  `profile_changed` is raised when the profile really changed.
- **Shared bonus.** With `shared_budget` the timekeeper adds the bonus and the
  bedtime extension other PCs granted (`FleetTracker.external_extras`). The
  payload has both views: `bonus_minutes` is what counts (all PCs),
  `bonus_local_minutes` / `bedtime_extension_local_minutes` what this PC
  granted - that is what the number entities show and set, and what the other
  PCs read.
- **Weekly summary.** On the first tick of a new week (and at start when it is
  missing) the week that ended is summed up per managed user from the daily
  counters - minutes per day, the busiest day, top programs / games / websites,
  how often time ran out (`time_up` rows of the event log), bonus granted -
  stored as a setting, published as `weekly_summary` / `last_week_minutes` and
  announced with the `weekly_summary` event.

A service that had to start from the last working configuration (config.yaml
could not be read) says so: `config_problem` in the PC payload, `degraded`, and
the `config_invalid` event - until a configuration that works is applied.

### Signs of tampering

The service notes, per managed user, when something looks like an attempt to
get around it, shows it in the user payload (`tamper`, `tamper_reason`,
`tamper_at`), raises the `tamper` event and keeps the flag in the database
until a parent clears it (`clear_tamper`, PIN-gated like switching a limit
off):

| Reason | What was seen |
|---|---|
| `agent_silent` | the agent is running but has not reported for `agent.watchdog_seconds` (hung or suspended); it is terminated and started again |
| `agent_killed` | the agent exited more than twice within ten minutes while the session stayed active |
| `report_rejected` | a report with a field of the wrong type or a number that is not finite - the real agent never sends one |
| `clock_changed` | wall clock and monotonic clock moved apart by more than a minute between two ticks (a long hole is sleep and is not flagged) |
| `time_zone_changed` | the PC's UTC offset changed and it was not the same zone switching to or from daylight-saving time (remembered across restarts) |

Reports are validated in `AgentReport.from_dict` (`models.py`): numbers are
clamped, text is cut to a sane length, a report is dated by its arrival. Other
messages from an agent are limited in kind and number, and the names of
programs and websites an agent can make the service record are capped per user
and day (the rest is booked on `other`).

Besides the daily totals, step 6 records per clock hour which application,
game and website was in front and which presence state the seat was in; that
is what the "last N hours" usage report and the idle / away counters are built
from. Items blocked from Home Assistant are stored as settings and turned into
always-blocked rules next to the configured ones. Rules made or changed from
Home Assistant are stored the same way, as rule definitions per user: one
with the name of a configured rule replaces it, the others are added, and
both get entities like any configured rule.

"Now" comes from one clock (`util.make_clock`): the time in
`time_limits.time_zone`, or - without it - in the zone the service first ran in,
which it stores and keeps (`time_zone_pinned`), so that every day boundary,
allowed-hours window and bedtime is independent of the PC's time zone setting.
Only when no zone can be worked out at all is it the PC's local time.

A separate thread (`pclink/updater.py`) looks for new releases and, when asked
to, downloads one, checks its checksum and code signature (`codesign.py`) and
installs it.

Windows backends are **injected**, so the whole loop runs against in-memory
fakes in the test suite on any OS (`tests/test_orchestrator.py`).

## Enforcement lives in the service, not the agent

All time and rule decisions are made by `TimeKeeper` / `RuleEngine` inside the
SYSTEM service. The agent only *reports*, *displays* and carries out small
requests inside the user's own desktop (closing the user's own process or tab,
volume, display power, screenshot). A user killing their agent gains nothing: the service
still counts their active time (falling back to the Terminal Services idle
timer and `WTSSendMessage` for warnings) and still logs them off / blocks
login. A window that a rule wants closed and that is still there half a minute
later - or that the agent reports it could not close - gets the next step: the
whole program instead of the tab, then a lock of the seat (`app_close_failed`). What the agent *reports* is taken on trust, though - it runs as the
user. With `count_idle: false` / `count_away: false` a modified agent that
claims to be idle is not counted, and per-app rules see the app name it
reports; keep the defaults (idle time counts) where that matters. Grants from Home Assistant can additionally be gated
behind a **parent PIN** (`pin.py`).

## Home Assistant side

Every entity is defined once in `pclink/entities.py` and served as a
**catalog entry** by the local API (`GET /api/entities`). The **PC Link integration** (`custom_components/pclink`)
consumes the catalog: it opens the API's WebSocket for pushed state and events
(falling back to REST polling), creates one entity per catalog entry
(`sensor`, `binary_sensor`, `switch`, `number`, `button`, `text`, `camera`),
sends entity commands as the JSON `set` action, fires every event as
`pclink_event` (which its device triggers match on) and exposes the JSON
commands as `pclink.*` actions. New catalog entries (a reloaded config, a new
user) become entities at runtime, changed ones are updated in place and
entries that disappeared are removed after ten minutes. The integration keeps
the last catalog and the PC's MAC address in Home Assistant's storage, so it
loads with unavailable entities while the PC is off and can wake it
(Wake-on-LAN is sent by Home Assistant, not through the PC). The PC announces the API with mDNS
(`pclink/discovery.py`, `_pclink._tcp`) so the config flow can offer it.

## Multi-PC

PCs do not talk to each other. The Home Assistant integration holds every
PC's state, and `custom_components/pclink/rollup.py` combines them into the
"all PCs" device (a pure function plus a small hub that listens to each PC's
coordinator). The same hub gives shared budgets (`time_limits.shared_budget`)
their figures: it sends every PC that reports `shared_budget` the usage of the
other PCs (the `peer_usage` command), which the PC keeps in its
`FleetTracker` (`fleet.py`).

## Performance

Designed to stay in the noise on CPU:

- psutil counters are read non-blocking (no `interval=` sleeps).
- Hardware sensors, Windows Update searches and device enumeration run on their
  own threads / slow intervals and never block the main loop.
- The agent samples cheap Win32 calls (`GetForegroundWindow`,
  `GetLastInputInfo`, XInput state, `SHQueryUserNotificationState`) on a 2 s
  poll, receives display-state changes as notifications rather than polling,
  and only walks UI Automation for the URL when the foreground window changes.
- Statistics are approximate by design: precision is never bought with CPU.

## Storage (`pclink/store.py`)

SQLite in `%ProgramData%\PCLink\pclink.db` (WAL mode). Daily per-user totals,
per-app, per-site and per-rule seconds, hourly detail (`usage_hourly`: seconds
per application, game, website and presence state, kept 14 days), HA-set
overrides (limits, bonus, rollover, bedtime extensions, focus timers, reward
claims, enable flags, the per-user block list, rules made or changed from Home
Assistant, per-rule weekly budgets, the parent-PIN lockout) and an
event log. Counters are batched in memory and flushed periodically; settings
are written at once.

## Files

```
pclink/
  __main__.py     CLI (run, install, harden, check-config, init-config, pin-hash, ...)
  service.py      pywin32 service wrapper + install-time hardening
  core.py         orchestrator / main loop
  timekeeper.py   daily/weekly/bedtime accounting + enforcement decisions (pure logic)
  rules.py        per-app / per-site limits (daily, weekly), blocklists, focus mode (pure logic)
  presence.py     active / passive / idle / away from the agent's signals (pure logic)
  wellbeing.py    break reminders (pure logic)
  pin.py          parent PIN gate (pure logic)
  fleet.py        usage of other PCs from Home Assistant (shared budgets) (pure logic)
  activity.py     gaming/browsing/site classification (pure logic)
  store.py        SQLite persistence (daily totals, hourly detail, settings, events)
  updater.py      self-update from GitHub releases (check, download, verify, run the installer)
  codesign.py     code signatures of a downloaded update (read with Windows' tools; the policy is pure logic)
  config.py       YAML schema + validation
  entities.py     Home Assistant entity definitions for the API catalog (incl. device triggers, camera)
  api.py          optional local REST / WebSocket API (+ entity catalog; write token and read-only token)
  webui.py        the setup / status page served by the API
  discovery.py    mDNS announcement of the API for the Home Assistant integration
  telemetry.py    psutil metrics + top processes
  devices.py      USB devices, removable drives, printers
  winupdate.py    Windows Update status + triggers
  gpu.py          NVML / Windows perf-counter GPU backends
  hwsensors.py    LibreHardwareMonitor backends (DLL / HTTP)
  power.py        shutdown/reboot/sleep/hibernate + Wake-on-LAN
  winsessions.py  Terminal Services session enumeration + control
  logonblock.py   login prevention (logon hours / disable account)
  launcher.py     start agents in user sessions
  ipc.py          localhost socket transport (mutual challenge-response on the launch token)
  hardening.py    service ACL + recovery (tamper resistance), data-folder permissions
  agent/          per-session agent: win_activity, win_presence, coreaudio, media,
                  capture, browser_url, tray, main
custom_components/pclink/   the Home Assistant integration (HACS)
  api.py          async client for the local API (REST + WebSocket)
  coordinator.py  pushed state, catalog refresh, events, polling fallback
  entity.py       catalog entry -> entity; dynamic entity creation
  sensor.py binary_sensor.py switch.py number.py button.py text.py camera.py
  update.py       the "PC Link update" entity
  rollup.py       the "PC Link (all PCs)" device, worked out from every PC's state;
                  passes PCs with a shared budget the usage of the other PCs
  button.py       also the Wake-on-LAN button (sent by Home Assistant itself)
  brand/          the integration's icon and logo
  config_flow.py  manual / mDNS discovery / reauth / reconfigure
  services.py     pclink.* actions (services.yaml, strings.json)
  device_trigger.py diagnostics.py
tests_ha/         integration tests (pytest-homeassistant-custom-component)
```
