# Home Assistant integration

The service uses **MQTT discovery**, so entities appear automatically once the
service connects to your broker. No Home Assistant YAML is required.

Requirements: the **MQTT integration** configured in Home Assistant and an MQTT
broker (the *Mosquitto broker* add-on is the usual choice). Point `mqtt.host`
in the config at the broker and set credentials.

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

## Free-form command topic

Anything not exposed as an entity can be sent as JSON to
`pclink/<device_id>/cmd` (or `POST /api/command`, see [API.md](API.md)):

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
