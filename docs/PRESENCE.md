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

## Signals (gathered by the per-session agent)

| Signal | Source | Used for |
|---|---|---|
| Keyboard / mouse idle | `GetLastInputInfo` (per session) | active vs. idle |
| Game controller input | XInput state (buttons, triggers, sticks past the dead zone) and `joyGetPosEx` for legacy pads | counts as input; a controller resting on the desk does **not** |
| Audio playing | Core Audio peak meter on the session's default output | passive |
| Now playing | Windows media transport controls (`winsdk`, optional) or window titles | passive; the `now playing` sensor |
| Display-required request | `CallNtPowerInformation(SystemExecutionState)` — set while a video player, browser video or game asks Windows to keep the screen on | passive, only when the foreground app is a media app / full-screen |
| Full-screen D3D / presentation mode | `SHQueryUserNotificationState` | passive |
| Display state | `RegisterPowerSettingNotification(GUID_SESSION_DISPLAY_STATE)` — per session, so correct for ASTER multiseat; console state as fallback | away (once no input for `display_off_min_idle_seconds`) |
| Windows user presence | `GUID_SESSION_USER_PRESENCE` — Windows' own input-based verdict | idle earlier than our threshold |
| Screensaver | `SPI_GETSCREENSAVERRUNNING` | away |
| Lock state | `OpenInputDesktop` | away |

Without an agent (killed, or `agent.enabled: false`) the service falls back
to the Terminal Services idle timer and lock flag of each session.

## Tuning

```yaml
activity:
  idle_seconds: 120              # no input for this long = idle
  away_seconds: 600              # ...and nothing playing = away
  audio_keeps_active: true
  media_keeps_active: true       # full-screen video / display-required apps
  controller_input: true
  display_off_is_idle: true
  display_off_min_idle_seconds: 30   # ignore another seat's monitor going dark right after input
  screensaver_is_idle: true
  windows_presence: true
  media_apps: [vlc.exe, spotify.exe, ...]   # apps whose display-required request counts as "watching"

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
