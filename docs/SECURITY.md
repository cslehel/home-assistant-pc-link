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
products use, applied automatically when the service is installed (`install.cmd`
for the standalone version, `python -m pclink install` for a Python install):

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

`python -m pclink install --no-harden` (standalone: `install.ps1 -NoHarden`) installs
without the DACL restriction. With the standalone version, use
`"C:\Program Files\PCLink\pclink.exe"` in place of `python -m pclink`.

The standalone installer puts the program in `C:\Program Files\PCLink` on
purpose: the service runs as SYSTEM, so its executable must live in a folder
that only administrators can change.

## The data folder

`C:\ProgramData\PCLink` holds `config.yaml` (API tokens, parent PIN), the database and the service log. Folders under `ProgramData` are
readable by every user by default, so installing, `harden` and every service
start restrict it to Administrators and LocalSystem (`secure_data_dir()` in
`pclink/hardening.py`). Edit the configuration from an elevated editor. The
per-session agent writes its own log to `%LOCALAPPDATA%\PCLink\logs`.

**When the folder cannot be made safe, nothing is installed and the service
does not start.** Before PC Link is installed every user may create things in
`C:\ProgramData`, including a *junction* called `PCLink` that leads to a
folder of their own; the configuration - and with its `commands:` the whole PC,
as SYSTEM - would then sit in a folder that user controls. So the installer,
`init-config`, `harden` and every start of the service check that the data
folder is a real folder and that restricting it worked (`ensure_data_dir()`),
and stop with an error otherwise. For the service that means: no time limits
until an administrator has looked at it. That is the lesser evil on purpose -
a service that visibly does not run (stopped in the Services console,
unavailable in Home Assistant, an error in Windows' event log) against one
that runs whatever a monitored user writes into its configuration. Once the
folder has been restricted a standard user cannot bring it back into such a
state, so this cannot be used to switch the limits off from a child's
account; Windows restarts the service every few seconds, so a passing problem
heals by itself. To recover: `rmdir "C:\ProgramData\PCLink"` (this removes a
link, not what it points to), then run the installer again.

Files that were in the folder before it was restricted and belong to another
account (a `config.yaml`, a `pclink.db` with limits and bonus time) are not
taken over. The standalone installer lists them before anything of PC Link
touches the folder and waits for you to look at them. When the folder is
secured for the first time (it is not yet owned by Administrators and closed
to everybody else), every entry that belongs neither to SYSTEM, the
Administrators group nor one of its members, and every link or hard link in it,
is moved into a `quarantine-<date>-<time>` folder inside the data folder
(restricted like the rest) and is not used; the service log says what was
moved. Look at it, then delete it or move back what you recognise. When
something cannot be moved aside (a file still open in another program), the
folder is not used, as above. In a folder that is restricted already nothing
is moved: only administrators can have put anything there, and ownership and
permissions are taken over.

**A configuration that stops loading does not switch the limits off either.**
The configuration the service starts with is kept as `config.last-good.yaml`.
When `config.yaml` cannot be loaded at a later start, the service runs with
that copy and reports the error (service log, Windows' event log, a
`config_invalid` event) until the file is corrected.

## What the agent is trusted with

The agent runs as the monitored user, so everything it sends can be forged by
that user. The service therefore:

- uses what the agent reports (idle time, foreground app, website) for
  accounting and rules, but never for file names or commands: a screenshot
  reply can only be stored as `.jpg` or `.png` in the screenshots folder;
- still counts the time when the agent is killed, and locks a session by
  disconnecting it when the agent did not lock it;
- accepts exactly one program on the agent port (the port is bound
  exclusively, so nothing else can listen on it while the service runs) and one
  agent per session, identified by a random token created for that launch.

What a modified agent *can* do is described in
[PRESENCE.md](PRESENCE.md#signals-gathered-by-the-per-session-agent): claim to
be idle when idle time is not counted, and report a different program name to
per-app rules, or a different website. Program rules and the block list match
file names; a renamed executable is not recognised. The `site_unknown` flag (a web app window whose
address the agent could not read) can only make focus mode stricter. A
modified agent that leaves it out gains nothing it could not already gain by
reporting another site.

## The parent PIN

- Use `parent_pin_hash` (`pclink pin-hash` asks for the PIN and prints the
  salted value; the older `parent_pin_sha256` is still read) or `parent_pin`; **quote**
  the PIN in YAML (`parent_pin: "0123"`), otherwise a leading zero changes it
  (a warning is logged).
- The PIN is asked for everything that gives a user more: more minutes, a
  higher or removed limit of any kind (today, weekday, weekend, weekly, per
  rule), a custom allow-list for focus mode, lifting a pause, ending focus
  mode, a later bedtime, a screenshot, removing an entry from the block list
  and switching a limit or rule off. Add `parent_present` to
  `time_limits.pin_required_for` to also protect the "parent at home" switch.
- Lowering a limit or sending the same value again needs no PIN and does not
  restart the grace period before logoff.
- After `pin_max_attempts` wrong tries the PIN is locked for
  `pin_lockout_seconds`; the lockout survives a restart of the service.
- The PIN is never written to the log (a command that cannot be parsed is
  logged by its size only, not by its text).

## Self-update

With `updates.allow_install` (the default) the service can replace itself with
a release from `updates.repository` on GitHub, on request from Home Assistant
or by itself (`updates.auto_install`). It runs as SYSTEM, so **whoever can
publish a release in that repository can run code on the PC** - unless the
releases are signed (below). What is always checked: HTTPS to GitHub only
(redirects elsewhere are refused), the SHA-256 GitHub publishes for the file,
that the archive unpacks inside its staging folder and contains a PC Link
package, and that the package is the announced version and newer than the
installed one (its `VERSION` file, which the signed catalog covers: an old
release, with its known faults, cannot be handed out under a new name and no
update ever goes backwards). Leave `repository` at the default unless you publish your own
builds, protect that GitHub account with two-factor authentication, and set
`allow_install: false` to only be told about new versions. A `github_token`
for a private repository is sent to `api.github.com` only.

An update that fails does not leave the PC without limits: the installer
keeps the previous version until the new one is running and puts it back
otherwise ([INSTALL.md](INSTALL.md#install-the-standalone-version-recommended)).
The failure is shown on the *PC Link update* entity and that version is not
tried again automatically.

### Code signature of an update

Before anything from a download runs, its Authenticode signatures are read
with Windows' own tools (`pclink/codesign.py`):

- **A signed copy stays with its publisher.** When the installed `pclink.exe`
  carries a valid signature, an update is only installed if `pclink.exe`,
  `pclink-agent.exe`, the installer script `install.ps1` (which the update
  runs as SYSTEM) and the package's catalog `pclink.cat` are validly signed
  by the same certificate (or, after a renewal, one with the same subject,
  issued by the same intermediate certificate authority - the same CA
  certificate, or one with the same name and key - and whose chain ends in
  the same root certificate: the name alone is not enough, any certificate
  authority could issue it, and neither is the root, which a public
  certificate authority shares with all its customers; a renewal from another
  issuing CA needs `updates.trusted_signers`),
  *and* every file in the package is exactly what that catalog lists. The
  catalog is what covers the bundled runtime (Python and its libraries),
  which comes from other publishers and cannot carry our signature. Whoever takes over the GitHub account can
  then no longer push code to the PC without the signing key. Nothing has to
  be configured for this.
- `updates.trusted_signers` (a list of certificate thumbprints) pins the
  certificate explicitly: only those may sign, whatever the installed copy is
  signed with.
- `updates.require_signature: true` refuses unsigned updates even when the
  installed copy is unsigned.
- A signature that is present but broken, or a package that differs from its
  catalog, is refused in every case.
- **Whatever cannot be read is a refusal.** A file whose signature could not
  be checked, a catalog that is not a catalog, PowerShell not answering: the
  update is not installed. Who signed the installed copy does not depend on
  what can be read at that moment either: it is written down at the first
  start (`signer.json` in the data folder) and after that only ever replaced
  by another valid signer - an executable that reads as unsigned or cannot be
  read never turns a signed installation back into one that takes unsigned
  updates. (To go back to unsigned builds on purpose, install one by hand and
  delete `signer.json`.) A self-signed certificate is its own root: after
  replacing one, list the new thumbprint in `updates.trusted_signers`.

An **unsigned** copy has nothing to compare against and accepts unsigned
updates (unless `require_signature` is set): the paragraph above about the
repository applies in full. The release workflow signs the executables and
makes the signed catalog when a code-signing certificate is configured for the
repository (`SIGN_PFX_BASE64` / `SIGN_PFX_PASSWORD`); without one the releases
are unsigned. Signing happens in a job of its own that only runs for a release,
in the GitHub environment `release-signing`, and that runs nothing from PyPI
or from the repository before the certificate is put away again: keep the two
secrets in that environment and give it required reviewers. The build installs
its tools from `packaging/requirements-lock.txt` (exact versions with hashes,
made by `packaging/make_lock.py`) when that file exists, with the pip that
comes with the pinned `setup-python` action (it is not upgraded from PyPI). A certificate that Windows does not trust (self-signed) counts
as not signed until it is installed as trusted on the PC. The result of the
last check is written to the service log and shown in the attributes of the
*PC Link update* entity (`signature`; a refusal also in `error`).

## The local API

PC Link connects to no broker or cloud service: the local API is its only
network surface (besides the outgoing update check and the mDNS announcement).

- Everyone who has the API token can control the PC: shut it down, run the configured `commands:`,
  start programs in a user's session (`launch`), and - with
  `api.allow_config_edit`, which is off unless you switch it on - rewrite
  `config.yaml`, which includes `commands:` that run as SYSTEM. Treat the
  token like an administrator password.
- **Separate everyday control from administration** with `api.admin_token`:
  the configuration, reloading it, the custom commands, installing a PC
  Link update and `launch` with a command line of its own (`"command"`: the
  agents run it through the shell in every active session, an administrator's
  too) then need that token, and the one Home Assistant holds
  (`api.token`) cannot do them ([API.md](API.md#authentication)). Starting an
  app configured under `apps:` by its name stays everyday control. Somebody who
  gets hold of the everyday token can then grant time (subject to the parent
  PIN) but not run programs of their choosing or remove the PIN.
- **Use HTTPS** when the API is open to the network (`pclink make-cert`,
  [API.md](API.md#https)): over plain HTTP every device on the network that
  can listen in reads the token. Home Assistant pins the certificate's
  fingerprint.
- Guessing a token is slowed down (ten wrong tokens from an address lock that
  address out for a minute, answered with 429 and `Retry-After`; the PC itself
  counts as one address, whichever `127.x` or `::1` it uses, and an IPv6
  address by its /64), but a short token is still a weak one: use the
  one `pclink api-token` prints.
- Connections are capped in all and per address (the PC itself again being
  one), and part of the places is kept for connections that show a valid
  token: a flood of connections without one cannot lock Home Assistant out.
- Nothing else on the PC can take the API's port: it is bound exclusively -
  next to `127.0.0.1` also on `[::1]` (or next to `0.0.0.0` on `[::]`), as
  `localhost` may lead there - and the firewall rule the installers make is for
  PC Link's program only (`scripts/install-service.ps1` makes none when it
  cannot find that program). (A program of a monitored user listening on that
  port would otherwise be handed Home Assistant's token.)
- A web page open in a browser on the PC cannot read the API: requests without
  a token are only answered under a host name that is this PC (an IP address,
  `localhost`, its own name, or one listed in `api.allowed_hosts`), cross-site
  WebSockets are refused, and the setup page cannot be framed. The setup page
  itself is served to whoever can reach the port; it holds no data and asks
  for the token, which it keeps for that browser tab only (session storage,
  gone when the tab is closed; *Forget token* removes it at once).
- Reads from the PC itself need no token by default: every user logged in on
  the PC can see who is logged in and what the others are doing. Set
  `api.read_token` to close that; it is a token that can only read, and once
  it exists a read on the PC needs it (or the main token) too
  ([API.md](API.md#authentication)).
- The Home Assistant integration never follows a discovery announcement to a
  new address for a PC that is already set up.
- With `time_limits.shared_budget`, usage reported by other PCs comes from
  Home Assistant (the `peer_usage` command, which needs the API token but not
  the parent PIN); malformed or negative values are ignored. Whoever holds the
  token can therefore
  understate what a user did on *other* PCs - never what was used on this
  one. Figures that stop arriving are kept, not forgotten.

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

0. The PC's clock and time zone decide which day it is. A standard user may
   change the **time zone** by default (not the clock). With
   `time_limits.time_zone` (for example `Europe/Budapest`) days, allowed
   hours and bedtimes are reckoned in that zone whatever Windows is set
   to. A configuration created by the installer or `init-config` has it set
   already, to the zone the PC was in at that moment (`check-config` prints
   it; when the zone could not be worked out the file says so and you have to
   fill it in). A configuration from an earlier version is never changed: add
   the line yourself. Alternatively remove *Users* from "Change the time zone" in the local
   security policy (`secpol.msc` > Local Policies > User Rights Assignment).
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
