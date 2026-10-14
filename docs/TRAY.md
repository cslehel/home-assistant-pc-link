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

## Asking for more time
When the service offers it (a user with a time limit, and
`time_limits.time_requests.enabled`), the right-click menu has **Ask for more
time** with one entry per configured amount ("15 minutes", "30 minutes",
"1 hour"). Picking one sends the request to the service, which raises the
`time_requested` event in Home Assistant; the parent's answer (approved,
denied, or "ask again later" when the cooldown or the daily maximum applies)
comes back as an ordinary notification. The entry is not shown for users
without a limit, when requests are switched off, or while the agent has no
connection to the service. A second click within five seconds is not sent
again; how often a child may ask is decided by the service
(`cooldown_minutes`, `max_per_day`).

## Behaviour
- A left-click pops the current status as a balloon; a right-click opens the
  menu (status line, "Time remaining", and "Ask for more time" when offered).
- The icon comes back by itself when Explorer restarts or was not ready yet at
  logon (the agent listens for the taskbar being created and retries every
  10 seconds while the icon is missing).
- The icon turns green ("Connected", then the status line) as soon as the
  service is reached, also after a reconnect or when the agent was restarted.
- Balloons that arrive together are shown one after the other (a few seconds
  each), never one replacing the other.
- A notification that cannot be shown as a balloon (no icon at that moment, or
  `agent.tray_icon: false`) is shown as a message box instead. Message boxes
  open on their own thread, at most three at a time, so an unanswered one never
  stops the agent from locking the screen or closing an app. A box nobody
  answers closes by itself after two minutes (five for the last warning).
- **Warnings that cannot be missed.** Windows keeps balloons off the screen
  during a full-screen game or video, in presentation mode and with "do not
  disturb" on; they only land in the notification centre. A *warning* (time
  running out, the grace period before logoff, an app about to be closed) is
  then also shown as a message box on top of the game. It does not take the
  keyboard away from the game; only the last warning before a logoff is
  system-modal. Plain information (a break reminder, "a screenshot was taken")
  stays a quiet balloon. The agent tells the service how each notification
  reached the user (balloon, box, quiet, none).
- The agent behind the icon also carries out the **display** switch from Home
  Assistant (monitor off / on for this seat); nothing is shown for it.
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

## Troubleshooting
The agent writes its log to `%LOCALAPPDATA%\PCLink\logs\agent-<session>.log`
in the user's own profile (`tray icon added`, `connected to service`, which
now-playing source is used). Window titles, web addresses and the text of
commands are not written to it at the normal log level. `pclink-agent.exe` is
started by the service with a one-time token; started by hand it logs
"launched without session/token environment" and exits. The agent proves it
knows that token, and only accepts a service that proves the same (the token
itself is never sent); "cannot prove it is the service" in the log means
something else answered on the agent port. An agent whose service has ended
(the service process is watched) exits by itself; a new service starts its
own.

If the agent's activity sampling stops for a minute (a call into another
program - the media controls, a browser - never returned), the agent logs
"no activity sample completed" and exits; the service starts a fresh one
within seconds. Reading a browser's address bar has its own 20-second limit:
a browser that does not answer is given up on, and after three such cases (or
twenty failed reads in a row) the agent takes websites from window titles for
ten minutes, then tries again - for twice as long after each streak that did
not end, up to four hours.

Requests from the service (close an app, lock, volume, a screenshot) are
carried out one after the other on a thread of their own. Only one screenshot
is taken at a time (another request meanwhile is answered "busy"), at most
3840 pixels wide.
