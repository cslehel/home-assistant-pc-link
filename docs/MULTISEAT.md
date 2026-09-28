# ASTER multiseat

[ASTER](https://www.ibik.ru/) lets several people use one PC at the same time,
each with their own monitor, keyboard and mouse. To Windows, each seat is a
normal logon **session** with its own session id, so this project handles it
without anything ASTER-specific:

- **Enumeration.** `WTSEnumerateSessions` returns every session; unlike a
  single-user PC, several can be in the `active` state at once. Each is tracked
  independently.
- **Per-user accounting.** Time is accrued per user from the sessions that
  belong to them. Two children on two seats burn their own budgets in parallel.
- **Per-seat agents.** One agent is launched per active session, so the correct
  foreground window / idle / URL / tray icon is reported for each seat. Tokens
  are per session, so reports can't cross seats.
- **Enforcement is per session.** Logoff and lock target a specific session id,
  so one child being logged off never disturbs another seat.

## Shutdown with multiple users

A per-user `action: shutdown` is downgraded to a **logoff** of that user's
sessions whenever *another* user is still active — one child's timer must not
power off the machine under a sibling or parent. A global shutdown from Home
Assistant still powers the whole PC off (it warns every active session first).

## Login blocking with shared accounts

Login blocking works on **accounts**. If two seats share one Windows account,
blocking that account affects both seats — give each person their own account
for independent limits (also required for per-user statistics to make sense).

## Notes

- The lock action asks the agent to lock that specific session
  (`LockWorkStation`); if no agent is present it falls back to disconnecting the
  session.
- `manage_unknown_users: true` plus a `default_policy` applies a limit to any
  account that logs in without being listed — handy if seats are handed out
  dynamically.
