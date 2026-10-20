# ASTER multiseat

[ASTER](https://www.ibik.ru/) lets several people use one PC at the same time,
each with their own monitor, keyboard and mouse. To Windows, each seat is a
normal logon **session** with its own session id, so this project handles it
without anything ASTER-specific:

- **Enumeration.** `WTSEnumerateSessions` returns every session, and each is
  tracked independently. Seen from the service, ASTER lists only the first
  workplace as active (station `Console`); every further workplace is listed
  as *disconnected* with the station name `Multiseat`. PC Link takes such a
  session as an active seat, and leaves whether it is locked to that seat's
  agent. A session that is disconnected without that name (switch user) is
  still a disconnected one.
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
  disconnecting the session. Two requests right after each other count as one.
- `manage_unknown_users: true` plus a `default_policy` applies a limit to any
  account that logs in without being listed — handy if seats are handed out
  dynamically.
