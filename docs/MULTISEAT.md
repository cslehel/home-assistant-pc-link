# Multiseat (ASTER, Hydra Multi-Seat)

Multiseat software lets several people use one PC at the same time, each with
their own monitor, keyboard and mouse. PC Link supports
[ASTER](https://www.ibik.ru/) and [Hydra Multi-Seat](https://hydramultiseat.com).
To Windows, each seat is a normal logon **session** with its own session id,
so almost everything works the same with either:

- **Enumeration.** `WTSEnumerateSessions` returns every session, and each is
  tracked independently. Seen from the service, only the first seat is listed
  as active (station `Console`); every further seat is listed as
  *disconnected*. PC Link takes such a session as an active seat (see below
  for how it tells one), and leaves whether it is locked to that seat's agent.
- **Per-user accounting.** Time is accrued per user from the sessions that
  belong to them. Two children on two seats burn their own budgets in parallel.
- **Per-seat agents.** One agent is launched per active session, so the correct
  foreground window / idle / URL / tray icon is reported for each seat. Tokens
  are per session, so reports can't cross seats. An agent stays with its
  session through a disconnect (switch user) and is ended when the session
  ends; one that stops reporting is replaced after `agent.watchdog_seconds`.
- **Two accounts, one id.** Topics, entity ids and commands use a short id made
  from the account name. When two names boil down to the same id (`kevin.b` and
  `kevin-b`), the first keeps it and the other gets a suffix made from its name,
  so one seat's entities and buttons never act on the other's user.
- **Enforcement is per session.** Logoff and lock target a specific session id,
  so one child being logged off never disturbs another seat.

## Which sessions are seats

```yaml
multiseat:
  mode: auto            # auto | aster | hydra | off
  lock_fallback: logoff # logoff | none
```

### ASTER

ASTER names every further workplace's session `Multiseat`. A session that is
disconnected without that name (switch user) is still a disconnected one.

### Hydra Multi-Seat

Hydra gives the sessions of its further seats no telling name. With
`mode: auto` PC Link checks, at most once a minute, whether Hydra is on: the
file `%SystemRoot%\hydra-armed.flag` (present while Hydra's seats are on) or
its running service `hydra-service`. While it is on, **every** disconnected
session with a user in it is taken for a seat in use, except remote desktop
sessions (station `RDP-…`). `mode: hydra` does this without checking.

- **Switch user.** A session left by *Switch user* is also listed as
  disconnected, and from the service it cannot be told apart from a Hydra
  seat. Under Hydra it is counted as a seat in use (its agent then reports it
  as idle or away; with `count_idle: true` it still uses that child's time).
  Each person has a seat of their own with Hydra, so avoid *Switch user* on
  such a PC, or set `count_idle: false` for the children.
- Hydra Multi-Seat was added from its documentation and its installer (Hydra
  1.1, November 2026) and has not yet been tested on a live Hydra PC. The
  service info (`multiseat` in the Service sensor's attributes) shows what was
  detected: `aster`, `hydra` or nothing.
- `mode: off` treats no disconnected session as a seat (also not ASTER's).

## A seat that cannot be locked

With `action: lock` the agent locks the seat (`LockWorkStation`). An ordinary
session that stays unlocked is **disconnected**, which shows the Windows lock
screen. A multiseat seat is never disconnected: on ASTER that does nothing (the
seat stays usable), and Hydra takes a disconnected seat for a fault and
repairs it. So when the lock enforces a limit (time is up, or a window that
cannot be closed) and the agent says it could not lock the seat, or the seat
still reads unlocked at the next try, the seat is **logged off** after a
critical notice (`lock_fallback: logoff`, the default). Every such case is
written to the event log as `lock_failed` and sent to Home Assistant as the
device trigger *"… had a seat that could not be locked"*.

With `lock_fallback: none` the seat is only reported (once) and the lock is
asked for again about every 30 seconds. A lock pressed in Home Assistant never
logs a seat off; it only reports `lock_failed`.

## Shutdown with multiple users

A per-user `action: shutdown` is downgraded to a **logoff** of that user's
sessions whenever *another* user is still logged in — active at another seat or
in a disconnected session (switch user), where unsaved work would be lost just
the same. One child's timer must not power off the machine under a sibling or
parent. A global shutdown from Home
Assistant still powers the whole PC off (it warns every active session first).

## Login blocking with shared accounts

Login blocking works on **accounts**. If two seats share one Windows account,
blocking that account affects both seats — give each person their own account
for independent limits (also required for per-user statistics to make sense).

## Notes

- Each user has a **display** switch in Home Assistant that turns the monitors
  of that user's seat off and wakes them again; it is carried out by the
  seat's own agent, so it needs `agent.display_control` (on by default) and a
  running agent. Which monitors Windows powers down for a seat depends on how
  the seats are set up - try it on your hardware. Waking injects a tiny mouse
  movement, which also ends that seat's idle time.
- The lock action asks the agent to lock that specific session
  (`LockWorkStation`); if no agent is present - or the session is still
  unlocked when the lock is asked for again later - it falls back to
  disconnecting the session, or for a multiseat seat to what
  `multiseat.lock_fallback` says (above). Two requests right after each other
  count as one.
- `manage_unknown_users: true` plus a `default_policy` applies a limit to any
  account that logs in without being listed — handy if seats are handed out
  dynamically.
