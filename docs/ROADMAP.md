# Roadmap / feature ideas

These extend the current architecture without redesign. Not yet implemented.

## Time & rules
- [ ] Weekly budgets and unused-minute rollover.
- [ ] Per-app / per-website daily limits and blocklists (data is already
      collected by the activity tracker).
- [ ] Focus / homework mode (allowlist-only for a period).
- [ ] Separate bedtime schedule; auto-extend on parent presence (HA).
- [ ] "Earn minutes" via an HA input (chore done → bonus).
- [ ] Parent-PIN confirmation (HA `text`) before granting time.

## Presence & wellbeing
- [ ] Break reminders (20-20-20) as tray balloons.
- [ ] Treat monitor-sleep as idle for a seat.

## Telemetry & control
- [ ] Per-process top-N CPU/RAM sensor (scaffolded; `telemetry.top_processes`).
- [ ] Now-playing media, USB device list, printer status.
- [ ] Privacy-gated, parent-only screenshot-on-demand.
- [ ] Windows Update / pending-reboot status and triggered installs.
- [ ] Volume/mute as HA `number`/`switch`; launch/close apps from HA.

## Integration & robustness
- [ ] Native HA Device Triggers ("user ran out of time").
- [ ] Optional local REST/WebSocket API alongside MQTT.
- [ ] Hot config reload; a small local setup web page.
- [ ] Multi-PC roll-up dashboard.

Contributions welcome — the pure-logic pieces (`timekeeper`, `activity`,
`entities`) are easy to extend with tests that run on any OS.
