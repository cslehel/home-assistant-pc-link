# Roadmap / feature ideas

Everything from the original roadmap is now implemented. The list is kept as
a map of where each feature lives; the unchecked items at the end are ideas
that are still open.

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
- [x] Native HA Device Triggers ("user ran out of time") — for every user
      and the PC (`entities.py: USER_TRIGGERS / PC_TRIGGERS`, the
      integration's `device_trigger.py`).
- [x] Local REST/WebSocket API — `api:`
      (`api.py`; see [API.md](API.md)).
- [x] Hot config reload; a small local setup web page — the service watches
      `config.yaml` (plus a `Reload configuration` button and the `reload`
      action); the page is served by the API at `http://127.0.0.1:47392/`.
- [x] Multi-PC roll-up dashboard — a virtual "PC Link (all PCs)" device
      with per-user totals across PCs, plus `time_limits.shared_budget` to
      make one budget span all PCs.
- [x] Native Home Assistant integration —
      `custom_components/pclink`: HACS install, UI config flow with mDNS
      discovery, entities from the PC's catalog (`GET /api/entities`),
      `pclink.*` actions, device triggers, `pclink_event`; tested against the
      newest and the oldest supported Home Assistant release.

## Added after the first release
- [x] Block applications and websites from Home Assistant — per-user block
      list (`block app or website` / `unblock app or website` text entities,
      `pclink.block_item` / `pclink.unblock_item`; `core.py`).
- [x] Usage report — applications, games and websites in the last N hours
      (`activity.report_hours`; hourly detail in `store.py`; sensor, action and
      `GET /api/users/<user>/report`).
- [x] Idle and away time — `idle time today` / `away time today` sensors and
      presence minutes in the usage report.
- [x] Update PC Link from Home Assistant, manually or automatically —
      `updates:` (`updater.py`; `update` entity).
- [x] Wake-on-LAN button in the integration, usable while the PC is off.
- [x] Now playing from Windows' media controls in the standalone build
      (background browser tabs included).
- [x] Icons for the executables and the integration (`packaging/make_icon.py`).
- [x] Day boundaries that do not depend on the PC's time zone setting -
      `time_limits.time_zone` (`util.make_clock`; needs the `tzdata` package,
      which the standalone build includes).
- [x] Edit full rules (limits, allowed hours) from Home Assistant, not only
      the block list - `pclink.set_rule` / `pclink.remove_rule`, the
      `rule_set` / `rule_remove` JSON actions (`core.py`; loosening is
      PIN-gated by `rules.rule_loosens`).
- [x] Check the code signature of a downloaded update - `codesign.py`
      (same publisher as the installed copy, a signed catalog of every file;
      `updates.require_signature`, `updates.trusted_signers`).
- [x] A token for reading the local API from the PC itself -
      `api.read_token` (read-only; ends token-free reads on the PC).
- [x] Per-rule weekly budgets - `weekly_limit_minutes` on a rule (`rules.py`;
      `<rule> weekly limit` number, `<rule> remaining this week` sensor).
- [x] "All PCs" roll-up computed inside the integration
      (`custom_components/pclink/rollup.py`).
- [x] Per-seat display power control - a `display` switch per user,
      `pclink.display` (`agent/win_activity.py: set_display_power`).
- [x] Shared budgets across PCs through the integration - Home Assistant
      passes every PC the usage of the others (`rollup.py`; the `peer_usage` command, `fleet.update_peers`).
- [x] The installer script of a signed update is signed and checked like the
      executables (`codesign.py`, the build workflow). The signed catalog is
      still required: the bundled Python runtime cannot be signed by us.

- [x] Ask for more time from the tray, answered from Home Assistant -
      `time_limits.time_requests` (`agent/tray.py`, `core.py`;
      `pclink.answer_time_request`, the `time_requested` trigger).
- [x] Profiles (holiday / school day) - `time_limits.profiles`, per-user
      `profiles` (`timekeeper.py`; a `profile` select, `pclink.set_profile`).
- [x] Weekly summary per user - the `time last week` sensor and the
      `weekly_summary` trigger (`store.summary_range`, `core.py`).
- [x] Bonus minutes and bedtime extensions shared across PCs with a shared
      budget (`fleet.external_extras`).
- [x] Tamper warning and service-health sensors (`core.py`: agent killed or
      silent, forged reports, clock / time-zone changes; failing tick steps).
- [x] Limits and rules for Store apps (`agent/win_activity.py`).
- [x] Warnings that show over a full-screen game (`agent/main.py`).
- [x] Events missed while Home Assistant was away are replayed; repair issues
      for a PC at a new address or a different PC answering
      (`custom_components/pclink`).
- [x] HTTPS for the local API with certificate pinning, an admin token, a
      hash-pinned build and an upgrade that rolls back (`api.py`,
      `packaging/install.ps1`).

## Ideas for later
- [ ] Browser-extension based URL blocking (instead of closing the tab).
- [ ] Localised tray messages.

Contributions welcome — the pure-logic pieces (`timekeeper`, `rules`,
`presence`, `wellbeing`, `fleet`, `entities`) are easy to extend with tests
that run on any OS.
