# Presence: telling "watching a film" from "went to bed"

"No keyboard or mouse input for two minutes" is a poor proxy for "nobody is
at this seat". It misfires in both directions: a child watching Netflix or
playing with a game controller produces no keyboard input, while a PC left on
with the monitor asleep looks exactly like someone reading a long page. This
project therefore combines several cheap, local signals — no webcam, no
network calls — into one presence verdict per seat.

## The four states

| State | Meaning | Counts toward the budget? |
|---|---|---|
| `active` | human input (keyboard, mouse **or game controller**) within `activity.idle_seconds` | yes |
| `passive` | no recent input but the seat is clearly in use: audio is playing, a full-screen video/game keeps the display awake, or media is playing | yes, unless `count_passive: false` |
| `idle` | no input past the idle threshold and nothing playing | only while under the user's `idle_threshold_minutes`, or if `count_idle: true` |
| `away` | definitely nobody there: session locked, screensaver running, **monitor asleep**, Windows itself reports the user absent, or no input for `activity.away_seconds` | only if `count_away: true` (default: same as `count_idle`) |

The verdict, its reason and the raw signals are published per user as the
`presence` sensor (attributes `presence_details`) and per session in the
`status` sensor's `sessions` attribute. A PC-level binary sensor
`Someone is at the PC` is on when any seat is `active` or `passive`.

## How long: idle and away time

The time spent in each state is recorded per clock hour (kept for 14 days):

- the sensors **idle time today** and **away time today** (minutes since
  midnight; `locked_minutes_today` is in the user payload as well);
- `active_minutes`, `passive_minutes`, `idle_minutes`, `away_minutes` and
  `locked_minutes` in the [usage report](HOMEASSISTANT.md#example-usage-report-applications-and-websites-in-the-last-10-hours)
  for its window.

A locked session is counted as `locked`, not as away. Time is only recorded
while the user has an active session; a disconnected or logged-off user
accumulates nothing. Idle and away time is not credited to the application or
website that happens to be in front, whatever `count_idle` says about the time
budget. After the PC slept (a gap of more than two minutes between two
measurements) only a few seconds are recorded for the gap, in whatever state
the seat is in when it wakes.

## Signals (gathered by the per-session agent)

| Signal | Source | Used for |
|---|---|---|
| Keyboard / mouse idle | `GetLastInputInfo` (per session) | active vs. idle |
| Game controller input | XInput state changes (buttons pressed or released, triggers and sticks moved past the dead zone) and `joyGetPosEx` for legacy pads | counts as input; a controller resting on the desk - or with a button held or stuck - does **not** |
| Audio playing | Core Audio peak meter on the session's default output | passive |
| Now playing | Windows media transport controls (the `winrt` packages; included in the standalone build) - also music or video in a background browser tab; otherwise the foreground window's title | passive; the `now playing` sensor |
| Display-required request | `CallNtPowerInformation(SystemExecutionState)` — set while a video player, browser video or game asks Windows to keep the screen on | passive, only when the foreground app is a media app / full-screen |
| Full-screen D3D / presentation mode | `SHQueryUserNotificationState` | passive |
| Display state | `RegisterPowerSettingNotification(GUID_SESSION_DISPLAY_STATE)` — per session, so correct for ASTER multiseat; console state as fallback. Also shown by each user's **display** switch in Home Assistant, which can turn the seat's monitor off and on | away (once no input for `display_off_min_idle_seconds`) |
| Windows user presence | `GUID_SESSION_USER_PRESENCE` — Windows' own input-based verdict | idle earlier than our threshold |
| Screensaver | `SPI_GETSCREENSAVERRUNNING` | away |
| Lock state | `OpenInputDesktop` | away |

Without an agent (killed, or `agent.enabled: false`) the service falls back
to the Terminal Services idle timer and lock flag of each session.

The signals are reported by the agent, which runs as the user. A user who
replaces their agent with one that claims to be idle is not counted when
`count_idle: false` or `count_away: false` is set; with the defaults (idle time
counts) nothing is gained. Focus mode pauses the time budget only while an
agent is reporting: a seat without a report for a minute is counted normally,
and stays counted for the rest of the focus period - an agent that comes back
(the watchdog restarts a suspended one) gives the pause back only after ten
minutes of reports without a break.

Details that matter when a reading looks wrong:

- *Full screen* means the window covers its monitor exactly. A maximised
  window is not full screen, even with the taskbar hidden.
- A legacy joystick counts as input when an axis moved by more than a dead
  zone since the last movement, so a throttle or wheel at rest does not keep
  the seat active. An XInput pad likewise counts only when its state changed
  (XInput's packet number moved on and a button, trigger or stick differs
  from the last accepted input), so a held or stuck button stops counting.
- Turning a monitor **on** from Home Assistant wakes it with a tiny injected
  mouse movement, which Windows takes for input. The agent knows it was its
  own: until real keyboard or mouse input follows, the idle time goes on as if
  the movement had not happened, so waking the screen does not make the seat
  "active" or raise "came back to the seat".
- The audio device is looked up again every 20 seconds, so switching the
  default output (speakers to headset) is followed.
- The browser address is read from the address bar itself (Chrome, Edge and
  the other Chromium browsers, and Firefox), at most once every 4 seconds per
  window; while that limit applies no address is reported rather than the
  previous tab's. Only the browser's own tool bar is searched, never the page,
  so reading the address does not switch on the browser's accessibility
  support for web content. When the browser keeps not answering, the domain
  comes from window titles for a while (ten minutes, longer after repeated
  trouble) and the address bar is tried again after that.
- A browser window without an address bar is read differently. This covers an
  installed web app (PWA), an `--app` window, and a full-screen page. The agent
  reads the address from the page's top-level Document element, which Chromium
  and Firefox expose with the page's address as its value. Only that one
  element is read, never anything inside the page. Chromium lists the
  Document only once its accessibility support is on, so for such windows (and
  only for them) the agent asks the page's own window for it, which switches
  that support on in the browser. Site rules, block lists and focus allow-lists
  then apply to the web app as to any tab.
- When that address cannot be read, the window is reported as a web page
  whose site is unknown (`site_unknown` in the activity details). The report
  also carries the app's identity (`app_id`) when there is one: the window's
  AppUserModelID (`Chrome._crx_<id>` for an installed app) or the browser's
  `--app-id`. A domain reported with it is only a guess, taken from the
  `--app=` address the browser was started with or from the window title. The
  `--app=` address is used only when nothing marks the window as an installed
  app, because a browser process shows every window opened after it started.
  - Block lists and per-site limits match the guessed domain, if there is
    one. With no domain, nothing is blocked, and the time counts as browsing.
  - During focus mode such a window is never an allowed site, whatever its
    title says. It is not allowed through `allow_categories` either, or
    because the browser itself is in `allow_apps`. The service waits five
    seconds first, because the first read of a web app can fail or come late.
    The wait is for the same window (same browser process and app identity;
    a new title does not restart it). If a read in that time gives an allowed
    site, nothing is closed. A window that stays unknown is closed like a
    tab, with Ctrl+W first, and the usual escalation follows if it stays.
  - `focus.allow_unknown_sites: true` lets such windows stay during focus.
  - A window without any web page (the browser's own dialogs, for example
    *Save as*) is not affected.

## Tuning

```yaml
activity:
  idle_seconds: 120              # no input for this long = idle (at least 10)
  away_seconds: 600              # ...and nothing playing = away (at least idle_seconds)
  audio_keeps_active: true
  media_keeps_active: true       # full-screen video / display-required apps
  controller_input: true
  display_off_is_idle: true
  display_off_min_idle_seconds: 30   # ignore another seat's monitor going dark right after input
  screensaver_is_idle: true
  windows_presence: true
  media_apps: [vlc.exe, spotify.exe, ...]   # apps whose display-required request counts as "watching"
                                            # (full screen, they are not taken for a game either)

time_limits:
  users:
    - name: kevin
      count_idle: false          # idle does not burn budget...
      idle_threshold_minutes: 10 # ...after this long without input
      count_away: false          # monitor asleep / screensaver stops the clock at once
      count_passive: true        # films and music still count
```

`count_idle: true` (the default) keeps the strict "logged in = clock runs"
behaviour, including while away; set `count_away: false` to stop the clock as
soon as the monitor sleeps while still counting ordinary idle time.

## Events

Presence transitions raise the device triggers *"left the seat"*
(`presence_away`) and *"came back to the seat"* (`presence_back`), so an
automation can, for example, dim the room when a child walks away from a PC
that is still running.

All of the decision logic is in `pclink/presence.py` and covered by
`tests/test_presence.py`; the Windows signal collection is in
`pclink/agent/win_presence.py`.
