# Home Assistant integration

There are two ways to connect a PC to Home Assistant. Pick one per PC.

| | **PC Link integration** (recommended) | **MQTT discovery** (classic) |
|---|---|---|
| Installed in Home Assistant | yes: a HACS custom integration with a UI setup flow | nothing (uses the MQTT integration) |
| Needs an MQTT broker | no | yes (Mosquitto add-on) |
| Transport | the PC's local API: WebSocket push + REST | MQTT |
| Discovery | the PC is found on the LAN automatically (mDNS) | entities appear via MQTT discovery |
| Actions (`pclink.add_minutes`, `pclink.focus`, ...) | yes, with proper fields and a device picker | no (JSON to a command topic) |
| Device triggers ("Kevin ran out of time") | yes | yes |
| Multi-PC roll-up device / shared budgets across PCs | needs MQTT between the PCs (see below) | yes |

Both produce the same **device** with the same entities and the same events;
the entity list further down applies to either.

---

## Option A: the PC Link integration

### 1. On the PC: enable the local API

```powershell
python -m pclink api-token
```

prints a fresh token and the block to paste into `%ProgramData%\PCLink\config.yaml`:

```yaml
api:
  enabled: true
  host: 0.0.0.0          # reachable from Home Assistant
  port: 47392
  token: <the generated token>
```

Then restart the service (`python -m pclink restart`) and allow the port
through the Windows firewall (elevated PowerShell):

```powershell
New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort 47392 -Action Allow -Profile Private
```

If you do **not** also want MQTT, set `mqtt: {enabled: false}` (no broker is
contacted at all). If you keep MQTT for other PCs, set `mqtt: {discovery: false}`
so the same entities are not created twice.

### 2. In Home Assistant: install the integration

**HACS:** HACS → Integrations → ⋮ → *Custom repositories* → add
`https://github.com/cslehel/home-assistant-pc-link` as *Integration* → install
**PC Link** → restart Home Assistant.

**Manual:** copy `custom_components/pclink/` from this repository into your
Home Assistant `config/custom_components/` folder and restart.

### 3. Add the PC

Settings → Devices & services. If the PC is on the same network it is already
listed as **Discovered** (the service announces itself with mDNS): click
*Configure*, enter the token, done. Otherwise *Add integration* → **PC Link**,
enter the PC's IP address or host name, the port and the token.

A device named after the PC appears with every entity; new users, rules,
apps or hardware sensors you add to `config.yaml` later show up by themselves
after the PC reloads its configuration (no restart of Home Assistant).

Token changed on the PC? Home Assistant asks for the new one
(*Reconfigure* also lets you change the address).

### Actions

Every command is an action with a device picker (`device_id` may be omitted
when only one PC is configured) and an optional `pin` field for the parent PIN:

| Action | Fields |
|---|---|
| `pclink.add_minutes` | `user`, `minutes` |
| `pclink.earn` | `user`, `minutes`, `reason` |
| `pclink.reward` | `user`, `name` |
| `pclink.set_limit` | `user`, `scope` (today / weekday / weekend / weekly), `minutes` (empty = unlimited) |
| `pclink.clear_overrides` | `user` |
| `pclink.block` / `pclink.unblock` | `user` |
| `pclink.focus` / `pclink.focus_off` | `user`, `minutes`, `allow` |
| `pclink.extend_bedtime` | `user`, `minutes` |
| `pclink.rule_enabled` / `pclink.rule_limit` | `user`, `rule`, `enabled` / `minutes` |
| `pclink.breaks` | `user`, `enabled` |
| `pclink.notify` | `message`, `title`, `user`, `level` |
| `pclink.logoff` / `pclink.lock` | `user` (optional) |
| `pclink.volume` | `user`, `level`, `muted` |
| `pclink.screenshot` | `user` |
| `pclink.launch` / `pclink.close_app` | `name` or `command` / `process`, `user` |
| `pclink.power` | `action` (shutdown / reboot / sleep / hibernate / cancel_shutdown), `delay`, `message` |
| `pclink.wake_on_lan` | `mac`, `broadcast`, `port` |
| `pclink.pin` / `pclink.pin_lock` | `pin` |
| `pclink.parent_present` | `present` |
| `pclink.reload_config` | |
| `pclink.command` | `data`: any JSON command from the table at the end of this page (returns the PC's response) |

```yaml
# Grant Kevin 20 more minutes (PIN inline when one is configured)
action: pclink.add_minutes
data:
  user: kevin
  minutes: 20
  pin: "1234"
```

A refused action (for example "parent pin required") fails with that message,
so it is visible in the automation trace or the UI.

### Events

Every event the PC reports is fired on the bus as `pclink_event` with
`device_id`, `pc_id`, `pc_name`, `event` and, for user events, `user`
(plus event-specific fields such as `minutes` or `reward`). The **device
triggers** below are built on these.

### Switching from MQTT discovery

Entity ids follow the same pattern (`sensor.<pc>_<user>_time_remaining`), but
the integration's entities are new registry entries, so dashboard cards and
automations that reference entity ids keep working while customised names,
areas and long-term statistics of the old MQTT entities do not carry over.
Set `mqtt: {discovery: false}` (or `enabled: false`) on the PC and remove the
old MQTT device in Home Assistant once the new one is in place.

### Several PCs

Add each PC as its own integration entry. Shared budgets and the virtual
*"PC Link (all PCs)"* device are computed by the PCs themselves over MQTT, so
for those keep `mqtt.enabled: true` (with `mqtt.discovery: false`) on every PC.
Everything else works without a broker.

---

## Option B: MQTT discovery

The service talks to Home Assistant over **MQTT** using MQTT *discovery*; once
it connects to your broker, a new **device** with all its entities appears in
Home Assistant by itself.

One-time setup on the Home Assistant side (about 5 minutes):

1. **Install an MQTT broker** (skip if you already have one).
   Settings → Add-ons → Add-on store → **Mosquitto broker** → Install → Start
   (enable *Start on boot*).
2. **Create a login for the PC.**
   Settings → People → Users → **Add user**, e.g. username `pclink` with a
   strong password; leave *Administrator* **off**. Mosquitto accepts Home
   Assistant users as MQTT credentials.
3. **Add the MQTT integration.**
   Settings → Devices & services → **Add integration** → *MQTT*. With the
   Mosquitto add-on it is discovered automatically; accept the defaults
   (broker `core-mosquitto`, discovery **on**, prefix `homeassistant`).
4. **Point the PC at it.** In `%ProgramData%\PCLink\config.yaml` on the PC:
   ```yaml
   mqtt:
     host: 192.168.1.10        # your Home Assistant IP or hostname
     port: 1883
     username: pclink
     password: <the password from step 2>
     discovery_prefix: homeassistant
   ```
   Test it from the PC: `python -m pclink test-mqtt`.
5. **Start the service** (`python -m pclink run` to try, then
   `python -m pclink install` + `start`).
6. **Find the device.** Settings → Devices & services → **MQTT** → *devices*:
   a device named after your PC lists every sensor, switch, number and button.

If nothing appears: check the service log in `%ProgramData%\PCLink\logs`,
confirm the broker port (1883) is reachable from the PC, and in Home Assistant
open Settings → Devices & services → MQTT → *Configure* → **Listen to a topic**
and subscribe to `pclink/#` to see whether messages arrive.

---

## Entities

Everything is grouped under one HA **device** named after the PC. Its entities:

## PC entities
- `sensor` CPU usage, CPU frequency, memory usage/used, swap, per-disk usage &
  free, disk read/write, network up/down, GPU load/memory/temp/power, battery,
  last boot, uptime, processes, IP, MAC, logged-in users, active sessions.
- `sensor` **Top process (CPU / memory)** with the top-N list as attributes
  (`telemetry.top_processes: N`).
- `sensor` **USB devices** (list as attribute), **Removable drives**,
  **Printer** (per-printer states as attributes), **Print jobs**
  (`telemetry.usb_devices`, `telemetry.printers`).
- `sensor` **Windows updates pending** (titles as attributes), `binary_sensor`
  **Reboot pending**, `sensor` last update check; `button` **Check for
  updates** and, with `allow_install`, **Install updates** (`windows_update:`).
- `binary_sensor` power (on/off via availability), someone using the PC,
  **someone at the PC** (presence), battery charging, **Parent PIN unlocked**.
- `text` **Parent PIN** (password field; see *Parent PIN* below), broadcast
  message to everyone.
- `switch` time limits (global on/off), **Parent at home** (auto-extends
  bedtimes; set it from a presence automation).
- `button` shut down, reboot, sleep, hibernate, cancel shutdown, lock all
  sessions, **Reload configuration**, **Launch/Close <app>** for each entry in
  `apps:`, plus any custom commands.
- Hardware sensors (temps/fans/power) appear as individual `sensor` entities.

## Per-user entities (each managed user)
- `sensor` status, **presence** (`active` / `passive` / `idle` / `away`, with
  the reason and raw signals as attributes — see [PRESENCE.md](PRESENCE.md)),
  activity, current app, current website, **now playing**, time used today,
  gaming today, browsing today, **time used this week**, time remaining,
  daily limit, **weekly time remaining**, **rolled-over minutes**,
  **earned today**, **bedtime tonight**, **focus time remaining**,
  **continuous screen time**, and one sensor per rule (`ok` / `used_up` /
  `blocked` / `outside_hours` / `off`, details as attributes) plus a
  `<rule> remaining` sensor for rules with a limit.
- `binary_sensor` logged in, login blocked, within allowed hours,
  **past bedtime**.
- `number` limit today, weekday limit, weekend limit, **weekly limit**, bonus
  today, **add minutes** (adds and resets to 0), **bedtime extension**,
  **focus duration**, **volume**, `<rule> limit today`.
- `button` +15 min, +30 min, one **reward** button per `rewards:` entry,
  **bedtime +30 min**, **take screenshot** (when `screenshots.enabled`), log
  off, lock.
- `switch` limit enabled, access paused, **focus mode**, **break reminders**,
  **muted**, `<rule> rule` (enable/disable a rule).
- `camera` **screenshot** — the last screenshot taken (when enabled).
- `text` message to this user.

Unlimited users (e.g. parents) get the statistics entities (including
presence, now playing and volume) but no limit controls, and are never
captured by the screenshot feature.

## Device triggers

Every user and the PC get native **device triggers** (Settings → Automations →
*Device* → pick the PC): *"Kevin ran out of time"*, *"is about to run out of
time"*, *"entered the grace period"*, *"logged in / logged off"*, *"got login
blocked / unblocked"*, *"reached bedtime"*, *"started / ended focus mode"*,
*"is due for a break"*, *"hit an app or site limit"*, *"had an app or tab
closed"*, *"earned bonus minutes"*, *"had a screenshot taken"*, *"left the
seat"*, *"came back to the seat"*; for the PC: *"service started"*,
*"configuration reloaded"*, *"parent PIN entered"*, *"wrong parent PIN"*,
*"needs a reboot for updates"*, *"has Windows updates pending"*, *"USB
devices changed"*, *"printer reported an error"*.

The same events are published as JSON to `pclink/<device_id>/events` for
advanced automations (`{"ts", "user", "event", ...}`).

## Parent PIN

With `time_limits.parent_pin` (or `parent_pin_sha256`) set, Home Assistant can
only **loosen** a limit after the PIN was entered in the **Parent PIN** text
entity (valid for `pin_timeout_seconds`, default 5 minutes) or passed as
`"pin"` in a JSON command. Loosening means granting or earning minutes,
raising today's / weekly / rule limits, pausing a limit, un-pausing access,
ending focus mode early, extending bedtime and taking screenshots. Tightening
(pausing access, lowering limits, starting focus mode, logging off) never
needs it. Wrong attempts are rate limited and raise the *"wrong parent PIN"*
trigger; refused actions are shown in `time remaining` → `limit_details` →
`last_refusal`.

## The "all PCs" device

With several PCs publishing under the same `mqtt.base_topic`, one of them (the
online PC with the smallest device id) also maintains a virtual device
**PC Link (all PCs)**: *PCs online*, *PCs in use*, *Any PC in use* and, per
user, *total time today (all PCs)* (per-PC minutes as attributes), *status
(all PCs)*, *active on* and *logged in anywhere*. Set
`time_limits.shared_budget: true` on every PC to make each user's daily and
weekly limits span all of them.

## Attributes worth using
- `time used today` carries a **7-day history** attribute (for a chart).
- `gaming today` / `browsing today` carry **top games** / **top sites** maps.
- `time remaining` carries a `limit_details` attribute (schedule, allowed
  hours, bedtime schedule, warn thresholds, action, rewards, last refusal).
- `presence` carries the reason and every raw signal (input idle, controller,
  audio, display state, Windows presence, ...).
- `status` carries the list of that user's sessions (seat, state, idle, lock,
  presence).
- rule sensors carry used / limit / remaining minutes and whether the rule is
  active right now.

## Free-form JSON commands

Anything not exposed as an entity can be sent as JSON: with the integration
through the `pclink.command` action, over MQTT to `pclink/<device_id>/cmd`, or
with `POST /api/command` (see [API.md](API.md)):

```yaml
# Grant Kevin 20 more minutes today (PIN inline when one is configured)
service: mqtt.publish
data:
  topic: pclink/family_pc/cmd
  payload: '{"action": "add_minutes", "user": "kevin", "minutes": 20, "pin": "1234"}'
```

Supported actions:

| Action | Fields | Effect |
|---|---|---|
| `shutdown`, `reboot`, `sleep`, `hibernate`, `cancel_shutdown` | `delay`, `message` | power |
| `logoff`, `lock`, `notify` | `user`, `session`, `title`, `message`, `level` | sessions |
| `wol` | `mac`, `broadcast`, `port` | wake another PC |
| `add_minutes`, `set_bonus` | `user`, `minutes` | bonus minutes |
| `earn` | `user`, `minutes`, `reason` | bonus minutes recorded as earned |
| `reward` | `user`, `name` | claim a configured reward |
| `set_limit` | `user`, `minutes`, `scope: today\|weekday\|weekend\|weekly` | limits |
| `clear_overrides` | `user` | back to the configured limits |
| `block`, `unblock`, `limit_enabled` | `user`, `enabled` | pause / enable |
| `limits` | `enabled` | global on/off |
| `focus`, `focus_off` | `user`, `minutes`, `allow: [...]` | focus mode |
| `extend_bedtime`, `bedtime_extension` | `user`, `minutes` | bedtime |
| `rule_enabled`, `rule_limit` | `user`, `rule`, `enabled` / `minutes` | per-app rules |
| `breaks` | `user`, `enabled` | break reminders |
| `volume`, `mute` | `user`, `level` / `muted` | audio in the user's session |
| `screenshot` | `user` | screenshot to the camera entity |
| `launch`, `close_app` | `name` or `command` / `process`, `user` | apps |
| `parent_present` | `present` | same as the switch |
| `pin`, `pin_lock` | `pin` | unlock / re-lock the parent PIN |
| `check_updates`, `install_updates` | | Windows Update |
| `run` | `name` | custom command |
| `reload`, `refresh` | | reload config / republish discovery |
| `set` | `entity`, `value` | any entity command by its key, e.g. `{"action":"set","entity":"user_kevin_blocked","value":"ON"}` |

## Example: notify when a child runs low

```yaml
automation:
  - alias: "Warn me when Kevin has 10 minutes left"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kevin_time_remaining
        below: 10
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Kevin has {{ states('sensor.kevin_time_remaining') }} min of PC time left."
```

## Example: device trigger + parent presence

```yaml
automation:
  - alias: "Kevin ran out of time"
    trigger:
      - platform: device
        domain: mqtt
        device_id: <the PC's device id>
        type: ran out of time
        subtype: Kevin
    action:
      - service: notify.mobile_app_phone
        data: {message: "Kevin's PC time is over."}

  - alias: "Parent at home extends bedtime"
    trigger:
      - platform: state
        entity_id: person.dad
    action:
      - service: switch.turn_{{ 'on' if trigger.to_state.state == 'home' else 'off' }}
        target: {entity_id: switch.family_pc_parent_at_home}
```

## Example: bonus-time button on a dashboard

```yaml
type: entities
title: Kevin's PC time
entities:
  - entity: sensor.kevin_time_remaining
  - entity: sensor.kevin_time_used_today
  - entity: sensor.kevin_presence
  - entity: number.kevin_limit_today
  - entity: button.kevin_15_min
  - entity: button.kevin_reward_homework_done_30_min
  - entity: switch.kevin_focus_mode
  - entity: switch.kevin_access_paused
  - entity: sensor.kevin_activity
  - entity: sensor.kevin_youtube
```

## Example: multi-PC roll-up dashboard

```yaml
type: entities
title: Screen time (all PCs)
entities:
  - entity: sensor.pc_link_all_pcs_pcs_online
  - entity: sensor.pc_link_all_pcs_pcs_in_use
  - entity: sensor.kevin_total_time_today_all_pcs
  - entity: sensor.kevin_active_on
  - entity: sensor.sarah_total_time_today_all_pcs
  - entity: sensor.sarah_active_on
```

## Example: WOL another PC through this one

```yaml
service: mqtt.publish
data:
  topic: pclink/family_pc/cmd
  payload: '{"action": "wol", "mac": "AA:BB:CC:DD:EE:FF"}'
```

## What happens when the connection to Home Assistant drops

**Time limits keep working.** Every decision (counting time, warnings, log
off / lock, login blocking, allowed hours, bonus minutes you granted earlier)
is made locally by the service and stored in its own SQLite database. Home
Assistant is a remote control and a display, never a dependency. A child gets
neither extra time nor a lock-out because the network, the broker or Home
Assistant is down. The per-user tray icons keep working too (they talk to the
service over localhost).

**Home Assistant shows the PC as unavailable** rather than showing stale
values:

- *PC Link integration (local API):* state is pushed over a WebSocket every
  few seconds. If the socket drops, or the PC stops pushing for 90 s, the
  integration switches to polling and marks the entities unavailable until it
  can reach the PC again; it then reconnects with a backoff of 1 s to 60 s.
- *MQTT:* the broker publishes the retained *last will* (`offline`) as soon
  as the service's connection is lost, so all entities become unavailable.
  The service reconnects with a 2 s to 60 s backoff.

**On reconnect everything resyncs by itself.** The service republishes its
discovery payloads and the full current state, so entities come back with the
truth (used minutes, remaining minutes, blocked / paused switches). This also
happens when Home Assistant itself restarts (it announces itself on
`homeassistant/status`). Statistics collected during the outage are in the
database and appear in the history attributes.

**Commands sent while the PC was unreachable are not replayed.** A button
pressed or a number changed during the outage is lost, deliberately: a
"shut down" or "log off" from three hours ago must never fire when the link
returns. The same goes for events: `time_up`, `logged_off` and similar device
triggers that happened during the outage are not delivered late, so
automations never react to stale moments. Check the entity state after the
PC is back and repeat the command if you still want it. Limits set through
the config file or applied before the outage are unaffected.
