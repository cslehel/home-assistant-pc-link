# The system-tray icon

Each logged-in user sees a tray icon (one per seat) provided by the per-session
agent. It is drawn natively via `Shell_NotifyIcon` (no extra GUI dependency, so
a smaller footprint and less for antivirus to look at).

## What it shows
- **Colour = state**
  - green: connected to the service, time remaining;
  - amber: 15 minutes or less remaining;
  - blue: focus / homework mode is on;
  - red: out of time, outside allowed hours, past bedtime, or access paused;
  - grey: not currently connected to the service.
- **Tooltip / menu**: the current status line, e.g. "1 h 20 min left today",
  "No time limit", "Outside allowed hours", "Bedtime", "Computer access
  paused", "Focus mode (40 min left)".
- **Balloon notifications**: time-warning countdowns, the grace-period warning
  before logoff/lock, break reminders, "this app is not allowed / will be
  closed" notices from rules and focus mode, bonus-time confirmations, a note
  when a parent took a screenshot (`screenshots.notify_user`), and any message
  pushed from Home Assistant.

## Behaviour
- Left- or right-click shows the status; clicking "Time remaining" pops the
  current status as a balloon.
- The tray icon is display-only. It has **no Exit item** for the monitored user
  (see [SECURITY.md](SECURITY.md)); closing or killing the agent does not grant
  time and the service relaunches it.
- Disable it per PC with `agent.tray_icon: false`, or disable the whole agent
  with `agent.enabled: false` (activity stats, presence signals, URL tracking,
  rules enforcement, volume and screenshots then stop, but time limits and
  telemetry continue).

## Requirements
`pywin32` (installed via `requirements-windows.txt`). If it is missing the tray
degrades to a no-op and warnings fall back to a system message box.
