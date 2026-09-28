# Tamper resistance & the security model

You asked for the service to be "impossible to close/stop". This document
explains exactly what is implemented, why it stops short of literally
impossible, and how to set the PC up so a child cannot bypass their limit.

## The goal, stated precisely

The thing that matters for screen-time limits is: **a monitored user must not
be able to stop the enforcement to give themselves more time.** A monitored
user, in Windows terms, should be a **standard (non-administrator) account** —
that is the correct configuration for any parental-control setup, and it is the
real security boundary the operating system gives you.

The service is designed so that a standard user cannot:

- stop, pause or restart the service (Services console, `sc stop`, `net stop`,
  `taskkill`);
- disable it or change its start type;
- delete it or reconfigure it;
- gain time by killing the tray **agent** (the service relaunches it and keeps
  counting regardless).

An **administrator** can always do all of the above. That is intentional.

## How it is enforced

Two standard Windows mechanisms, the same ones commercial parental-control
products use, applied automatically at `python -m pclink install`:

1. **A restrictive service security descriptor (DACL).** Stop, pause,
   change-config and delete rights are removed for Interactive, Service and
   Authenticated users, leaving them query/read access only. Administrators and
   LocalSystem keep full control. Attempts to stop/reconfigure/delete are
   audited (SACL). See `pclink/hardening.py` (`HARDENED_SDDL`).

   Worth knowing: Windows' *default* service permissions already deny
   standard users the right to stop services. Hardening re-asserts this
   explicitly (in case another tool loosened it), adds auditing, and is what
   makes the auto-restart below meaningful.

2. **Automatic restart on failure.** If the process is killed (which requires
   admin rights anyway) or crashes, the Service Control Manager restarts it
   after 5 seconds. See `set_recovery()`.

Manage it explicitly if you want:

```powershell
python -m pclink harden      # apply DACL + recovery (done by install already)
python -m pclink unharden    # restore default permissions (done by remove)
sc.exe sdshow PCLink         # inspect the current descriptor
```

`python -m pclink install --no-harden` installs without the DACL restriction.

## Why not literally unkillable

Making the service impossible to stop **even by an administrator** would
require techniques that are indistinguishable from malware, and each has a
serious downside for you, the owner:

| Technique | Why it's rejected |
|---|---|
| Marking the process "critical" (`RtlSetProcessIsCritical`) | Killing it **blue-screens the machine** (`CRITICAL_PROCESS_DIED`). Dangerous and user-hostile. |
| Blocking Task Manager / Services console | Breaks legitimate administration; trivially reverted by an admin anyway. |
| Hiding the process / rootkit-style concealment | This is malware behavior and **guarantees antivirus flags you** — the opposite of your other request. |
| A watchdog pair that resurrects each other | Anti-tamper "process protection" pattern; heuristic AV engines specifically hunt for it. |

Beyond the AV problem, any of these would leave **you** unable to update,
reconfigure or uninstall your own software, and a critical-process trick can
make a machine unbootable. The standard-user boundary achieves the actual goal
(the kid can't bypass it) without any of that risk.

## Checklist for a tamper-resistant setup

1. The monitored user is a **Standard** account (Settings → Accounts, or
   `net localgroup Administrators` should **not** list them).
2. There is a **separate administrator** account with a password the monitored
   user does not know.
3. The service is installed and **hardened** (the default).
4. `block_method: logon_hours` (default) or `disable_account` is set so the
   user also can't simply log back in after being logged off.
5. Optionally set a BIOS/UEFI password and disable booting from USB, so the
   account can't be reset offline. (Beyond the scope of this software, but it's
   the last mile of the same threat model.)

If the monitored user is a local administrator, no user-space software can
reliably constrain them — the answer is to make them a standard user, not to
escalate into anti-forensic techniques.
