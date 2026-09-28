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
- `binary_sensor` power (on/off via availability), someone using the PC,
  battery charging.
- `button` shut down, reboot, sleep, hibernate, cancel shutdown, lock all
  sessions, plus any custom commands.
- `switch` time limits (global on/off).
- `text` broadcast message to everyone.
- Hardware sensors (temps/fans/power) appear as individual `sensor` entities.

## Per-user entities (each managed user)
- `sensor` status, activity, current app, current website, time used today,
  gaming today, browsing today, time remaining, daily limit.
- `binary_sensor` logged in, login blocked, within allowed hours.
- `number` limit today, weekday limit, weekend limit, bonus today.
- `button` +15 min, +30 min, log off, lock.
- `switch` limit enabled, access paused.
- `text` message to this user.

Unlimited users (e.g. parents) get the statistics entities but no limit
controls.

## Attributes worth using
- `time used today` carries a **7-day history** attribute (for a chart).
- `gaming today` / `browsing today` carry **top games** / **top sites** maps.
- `time remaining` carries a `limit_details` attribute (schedule, allowed
  hours, warn thresholds, action).
- `status` carries the list of that user's sessions (seat, state, idle, lock).

## Free-form command topic

Anything not exposed as an entity can be sent as JSON to
`pclink/<device_id>/cmd`:

```yaml
# Grant Kevin 20 more minutes today
service: mqtt.publish
data:
  topic: pclink/family_pc/cmd
  payload: '{"action": "add_minutes", "user": "kevin", "minutes": 20}'
```

Supported actions: `shutdown`, `reboot`, `sleep`, `hibernate`,
`cancel_shutdown`, `logoff`, `lock`, `notify`, `wol`, `add_minutes`,
`set_bonus`, `set_limit` (`scope: today|weekday|weekend`), `clear_overrides`,
`block`, `unblock`, `limit_enabled`, `limits` (global), `run`, `refresh`.

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

## Example: bonus-time button on a dashboard

```yaml
type: entities
title: Kevin's PC time
entities:
  - entity: sensor.kevin_time_remaining
  - entity: sensor.kevin_time_used_today
  - entity: number.kevin_limit_today
  - entity: button.kevin_15_min
  - entity: switch.kevin_access_paused
  - entity: sensor.kevin_activity
```

## Example: WOL another PC through this one

```yaml
service: mqtt.publish
data:
  topic: pclink/family_pc/cmd
  payload: '{"action": "wol", "mac": "AA:BB:CC:DD:EE:FF"}'
```
