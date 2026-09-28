# Roadmap / feature ideas

Everything from the original roadmap is now implemented. The list is kept as
a map of where each feature lives; the unchecked items at the end are new
ideas that came up while building these.

## Time & rules
- [x] Weekly budgets and unused-minute rollover — `weekly_limit_minutes`,
      `rollover_max_minutes` (`timekeeper.py`; HA `weekly limit` number,
      `time used this week`, `rolled-over minutes` sensors).
- [x] Per-app / per-website daily limits and blocklists — `rules:` per user or
      shared under `time_limits` (`rules.py`; one sensor + enable switch +
      "limit today" number per rule; the app/tab is closed via the agent).
- [x] Focus / homework mode (allowlist-only for a period) — `focus:` per user
      (`focus mode` switch, `focus duration` number, optional `schedule`;
      homework time can be free of the budget).
- [x] Separate bedtime schedule; auto-extend on parent presence (HA) —
      `bedtime` / `wake_time` / `bedtime_parent_extension_minutes`, the
      `Parent at home` switch, `bedtime extension` number and `+30 min` button.
- [x] "Earn minutes" via an HA input (chore done → bonus) — `rewards:` buttons
      with a daily cap, the `add minutes` number and the `earn` JSON action.
- [x] Parent-PIN confirmation (HA `text`) before granting time —
      `parent_pin` / `parent_pin_sha256` (`pin.py`; password text entity,
      `Parent PIN unlocked` sensor, inline `"pin"` in JSON commands, lockout).

## Presence & wellbeing
- [x] Break reminders (20-20-20) as tray balloons — `breaks:` (`wellbeing.py`;
      `break reminders` switch, `continuous screen time` sensor, `break_due`
      trigger).
- [x] Treat monitor-sleep as idle for a seat — and much more: see
      [PRESENCE.md](PRESENCE.md) (`presence.py`, `agent/win_presence.py`):
      controller input, display state, screensaver, Windows user presence,
      full-screen D3D / display-required media, now-playing.

## Telemetry & control
- [x] Per-process top-N CPU/RAM sensor — `telemetry.top_processes: N`.
- [x] Now-playing media, USB device list, printer status —
      `activity.now_playing` (`agent/media.py`), `telemetry.usb_devices`,
      `telemetry.printers` (`devices.py`).
- [x] Privacy-gated, parent-only screenshot-on-demand — `screenshots:`
      (button + `camera` entity per managed user, PIN-gated, user notified,
      parents never captured; `agent/capture.py`).
- [x] Windows Update / pending-reboot status and triggered installs —
      `windows_update:` (`winupdate.py`; `Check for updates` /
      `Install updates` buttons).
- [x] Volume/mute as HA `number`/`switch`; launch/close apps from HA —
      `agent.volume_control` (`agent/coreaudio.py`) and `apps:` (launch/close
      buttons, `launch` / `close_app` JSON actions).

## Integration & robustness
- [x] Native HA Device Triggers ("user ran out of time") —
      `device_automation` discovery for every user and the PC
      (`entities.py: USER_TRIGGERS / PC_TRIGGERS`, topic `<base>/event/...`).
- [x] Optional local REST/WebSocket API alongside MQTT — `api:`
      (`api.py`; see [API.md](API.md)).
- [x] Hot config reload; a small local setup web page — the service watches
      `config.yaml` (plus a `Reload configuration` button and the `reload`
      action); the page is served by the API at `http://127.0.0.1:47392/`.
- [x] Multi-PC roll-up dashboard — `fleet:` (`fleet.py`): a virtual
      "PC Link (all PCs)" device with per-user totals across PCs, plus
      `time_limits.shared_budget` to make one budget span all PCs.
- [x] Native Home Assistant integration (no broker needed) —
      `custom_components/pclink`: HACS install, UI config flow with mDNS
      discovery, entities from the PC's catalog (`GET /api/entities`),
      `pclink.*` actions, device triggers, `pclink_event`; tested against the
      newest and the oldest supported Home Assistant release.

## Ideas for later
- [ ] Per-rule weekly budgets.
- [ ] "All PCs" roll-up computed inside the integration (today it needs MQTT
      between the PCs).
- [ ] Browser-extension based URL blocking (instead of closing the tab).
- [ ] Per-seat display power control (turn a seat's monitor off from HA).
- [ ] Localised tray messages.

Contributions welcome — the pure-logic pieces (`timekeeper`, `rules`,
`presence`, `wellbeing`, `fleet`, `entities`) are easy to extend with tests
that run on any OS.
