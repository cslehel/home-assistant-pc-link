# Local REST / WebSocket API and setup page

An optional HTTP server inside the service. Off by default. It is what the **PC Link Home Assistant integration** talks to
(see [HOMEASSISTANT.md](HOMEASSISTANT.md)); `python -m pclink api-token`
prints a ready-made block with a random token.

```yaml
api:
  enabled: true
  host: 0.0.0.0            # 127.0.0.1 = this PC only; anything else needs a token
  port: 47392
  token: change-me         # required for every write; required for everything off loopback
  # read_token: other      # can only read; with it set, reads on the PC itself need a token too
  # admin_token: another   # with it set, only this token may touch the configuration, run the
  #                        # custom commands and install a PC Link update (see below)
  # allow_config_edit: true  # without an admin token: let api.token read and save config.yaml (default: no)
  # tls_cert: 'C:\ProgramData\PCLink\api-cert.pem'   # HTTPS (pclink make-cert writes both files)
  # tls_key: 'C:\ProgramData\PCLink\api-key.pem'
  # allowed_hosts: [gaming-pc.lan]   # further names of this PC for requests without a token (see below)
  advertise: true          # announce _pclink._tcp on the LAN (mDNS) so Home Assistant finds the PC
```

mDNS advertising needs the `zeroconf` package (in `requirements-windows.txt`)
and is skipped silently when the API is bound to loopback.

Open `http://127.0.0.1:47392/` on the PC for the **setup / status page**: users
with time left, presence and activity, rules and modes, quick actions (+15,
pause, focus, log off), recent events, PC facts and a config editor that
validates the YAML before saving and hot-reloads the service.

## Authentication

Send the token as `Authorization: Bearer <token>`, `X-Token: <token>` or
`?token=<token>`.

| Request | From loopback | From elsewhere |
|---|---|---|
| reads (`GET`) | no token needed, unless `api.read_token` is set | `token` or `read_token` |
| commands (`POST /api/command`), screenshots | `token` | `token` |
| admin: the configuration, `POST /api/reload`, custom commands, installing a PC Link update, `launch` with a `command` | `admin_token`; without one configured: `token` | the same |

Without `api.token` the API is read-only. Binding to a non-loopback address
without a token is rejected at config validation. Grants still go through the
parent PIN when one is configured (`"pin"` field in the command). The Home
Assistant integration needs the token: it checks at setup that it may write.

**The admin level.** Some requests amount to running programs as SYSTEM: the
configuration (it holds `commands:`), reloading it, running a configured
custom command (`{"action": "run"}`, the `cmd_...` buttons), installing a
PC Link update and `{"action": "launch", "command": "..."}` - a command line of
the caller's choosing, which the agents run through the shell in every active
session (an administrator's too). Launching an app configured under `apps:` by
its `name` (and the `launch_...` buttons) is everyday control. With **`api.admin_token`** set, only that token may do these
(it can do everything else as well), and `api.token` - the one Home Assistant
holds, and that crosses the network with every request - is left with everyday
control: limits, bonus time, pause, focus, power. Such a request with the
everyday token is answered with 403 `admin token required`. Without an admin
token `api.token` may do all of it as before, except that reading and saving
the configuration additionally needs `api.allow_config_edit: true` (off by
default; the setup page's editor answers 403 until it is set). With an admin
token configured `allow_config_edit` is not looked at: the admin token is the
permission.
`admin_token` must differ from the other two tokens.

`GET /api/auth` tells a client what its token may do:
`{"ok": true, "writable": true, "admin": false, "config_edit": false}` (401
without a valid token; `writable` is false for the read token).

**Wrong tokens** are throttled: after 10 of them from one address within a
minute, tokens from that address are not looked at for a minute (a correct one
has to wait too). Such a request is answered with **429** and a
`Retry-After` header (whole seconds until the address may try again, at least
1), the body `{"error": "too many wrong tokens; try again in N s"}`; a
WebSocket command whose `token` is not looked at gets
`{"ok": false, "error": "...", "retry_after": N}`. Every address of the PC
itself (`127.0.0.0/8`, `::1`) counts as one address, an IPv6 address by its
/64. Reads that need no token are not affected.

### HTTPS

Without it the token crosses your network in the clear with every request.
`pclink make-cert` (from an administrator prompt) writes a self-signed
certificate and its key into the data folder and prints the two `api:` lines
and the certificate's SHA-256 fingerprint; after a restart the API speaks
HTTPS only. Home Assistant does not need the certificate to be trusted by
anybody: it pins the fingerprint, which the PC also announces by mDNS (`tls=1`,
`fp=<fingerprint>`), so a discovered PC needs nothing typed in. A browser will
warn about the self-signed certificate. `make-cert --force` replaces the
certificate (Home Assistant then needs the new fingerprint).

Reads from loopback need no token by default, so every user logged in on the
PC can read the state (who is logged in, current app and website) from
`http://127.0.0.1:47392/`. Where that matters, set **`api.read_token`**: from
then on a read on the PC needs a token as well - the read token or the main
one - and only the setup page itself (which holds no data and asks for the
token) is still served without. The read token can do nothing but read: no
command, no configuration, no screenshot. It is also accepted from other
machines, which makes it the token to give a dashboard or a script that only
shows things. The Home Assistant integration keeps using `api.token`.
Changing either token needs a restart of the service.

Limits that protect the service: request bodies up to 2 MB, WebSocket messages
up to 4 MB, at most 32 WebSocket clients (8 of them without a token, so that
programs on the PC cannot take every place), at most 96 connections in all and
40 without a token from one address (the PC itself counts as one), 16 of the 96
kept for connections that show a valid token: once the others are taken, a new
connection must send a token with its first request within 15 s, otherwise it
is answered with 503 (`Retry-After: 5`) and closed. A connection that has shown
a valid token no longer counts against those without one. Idle HTTP connections are closed after 60 s, a request's
head has 15 s in all, and a WebSocket client that stops reading is dropped
instead of being waited for. WebSocket frames follow RFC 6455: frames from a
client must be masked, a ping may arrive between the fragments of a message.
User names in paths are URL-decoded (`/api/users/john%20doe`).

**Web pages cannot reach the API.** A WebSocket handshake that carries an
`Origin` header from another site is refused (403). Requests without a token
are only answered when the `Host` header is this PC: an IP address,
`localhost`, the PC's own name (also `<name>.local`), or a name listed in
`api.allowed_hosts`. A page that points
its own host name at 127.0.0.1 ("DNS rebinding") gets 403 instead of the
state. Requests with a valid token are not held to this, so Home Assistant
keeps working under whatever name your router gives the PC; `allowed_hosts`
(for example `[gaming-pc.lan]`) is only for reading without a token on the PC
itself under such a name. It is read when the service starts. The setup page is
sent with `X-Frame-Options: DENY` and a Content-Security-Policy that allows
only its own script.

The setup page (`GET /`) is served to everybody who can reach the port, also
from another machine without a token: it holds no data and asks for the token
itself. It keeps the token in the browser tab's session storage (gone when the
tab is closed, never in the browser's lasting storage); *Forget token* removes
it at once. Everything under `/api` keeps its rules.

The port is bound exclusively: no other program on the PC can listen on it
next to the service (and so be handed Home Assistant's token). With
`host: 127.0.0.1` (or `localhost`) the API listens on `[::1]` as well, with
`0.0.0.0` on `[::]`, and the other way round, because `localhost` may resolve
to either; on a PC without IPv6 only the configured address is used, but when
the port is taken on the other address by another program the API does not
start.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version, device, uptime_seconds, restart_needed}` |
| GET | `/api/auth` | what the token may do: `{ok, writable, admin, config_edit}` (a token is always required) |
| GET | `/api/status` | the PC payload (the `pc` part of the WebSocket state) |
| GET | `/api/users` | `{user: payload}` for every known user |
| GET | `/api/users/<user>` | one user's payload (404 if unknown) |
| GET | `/api/users/<user>/screenshot` | the last screenshot as `image/jpeg` / `image/png` (token always required; 404 if none) |
| GET | `/api/users/<user>/report?hours=10` | the **usage report** (below); `hours` defaults to `activity.report_hours` |
| GET | `/api/entities` | the **entity catalog** (below) |
| GET | `/api/events?n=50&user=kevin` | recent events, newest first; `since=<ts>` only newer ones; `announced=1` the events that were pushed to Home Assistant, in the shape they were pushed in |
| POST | `/api/command` | run a JSON command (below); returns `{ok, error, ...}` |
| POST | `/api/reload` | hot-reload `config.yaml` (admin) |
| GET | `/api/config` | `config.yaml` as text (admin) |
| PUT | `/api/config` | validate, write and reload the YAML in the body (admin) |
| GET | `/api/ws` | WebSocket (below) |

### Entity catalog

`GET /api/entities` describes every Home Assistant entity this PC offers, in a
the form the integration builds its entities from:

```json
{
  "device": {"identifiers": ["pclink_family_pc"], "name": "Family-PC", "model": "Windows PC", "sw_version": "0.3.0"},
  "version": "0.3.0",
  "users": {"kevin": {"name": "Kevin", "managed": true}, "dad": {"name": "dad", "managed": false}},
  "triggers": {"user": {"time_up": "ran out of time"}, "pc": {"started": "service started"}},
  "entities": [
    {"unique_id": "pclink_family_pc_cpu_percent", "key": "cpu_percent", "platform": "sensor", "name": "CPU usage",
     "value": "cpu_percent", "unit": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit", "enabled": true, "source": "pc"},
    {"unique_id": "pclink_family_pc_user_kevin_blocked", "key": "user_kevin_blocked", "platform": "switch",
     "name": "Kevin access paused", "value": "blocked", "command": "user_kevin_blocked", "source": "user", "user": "kevin"}
  ]
}
```

`platform` is the HA platform (sensor, binary_sensor, switch, number, button,
text, camera, update); `value` / `attributes` are dotted paths into the `pc`
payload (`source: pc`) or into `users[<user>]` (`source: user`); `command` is
the key to send with the `set` action. Optional fields: `unit`, `device_class`,
`state_class`, `icon`, `category` (config / diagnostic), `enabled`, `options`,
`precision`, `min` / `max` / `step` / `mode`, `password`, and
`unrecorded_attributes`: a list of attribute names (keys of the `attributes`
mapping; a list payload is one attribute named like its key, e.g. `history`)
that change on nearly every push - processes, sessions, idle seconds, the
"last N hours" report, the page title. Home Assistant keeps those out of its
recorder; the state and the other attributes are recorded as usual.

An `update` entry (the *PC Link update* entity) has `value` = the key of the
installed version plus `latest`, `in_progress`, `release_url`, `summary` (keys
into the `pc` payload), `attributes` (`pclink_update_info`, shown as the
entity's attributes) and `title`; `command` is present only when the PC can
install the update itself (standalone build with `updates.allow_install`).

The users listed in the catalog are exactly the users in the state payloads:
managed users, users logged in right now, and, with
`activity.track_unmanaged_users`, everybody seen since the service started.

The catalog changes when the configuration is reloaded, a new user logs in or
a rule is made or removed from Home Assistant; clients refresh it after the
`config_reloaded` / `started` / `logged_in` / `rules_changed` events.

### Commands

The body of `POST /api/command` is the same JSON the `pclink.command` action
sends (see [HOMEASSISTANT.md](HOMEASSISTANT.md#free-form-json-commands)),
for example:

```json
{"action": "add_minutes", "user": "kevin", "minutes": 20, "pin": "1234"}
{"action": "focus", "user": "kevin", "minutes": 45, "allow": ["docs.google.com"]}
{"action": "rule_enabled", "user": "kevin", "rule": "YouTube", "enabled": false}
{"action": "rule_limit", "user": "kevin", "rule": "YouTube", "scope": "weekly", "minutes": 300}
{"action": "rule_set", "user": "kevin", "rule": "Roblox", "apps": ["RobloxPlayerBeta.exe"],
 "daily_limit_minutes": {"weekdays": 30, "weekend": 60}, "allowed_hours": "16:00-19:00"}
{"action": "rule_remove", "user": "kevin", "rule": "Roblox", "pin": "1234"}
{"action": "display", "user": "kevin", "on": false}
{"action": "peer_usage", "peers": {"den": {"name": "Den", "users": {"kevin": {"date": "2026-09-28", "used_minutes": 50}}}}}
{"action": "time_request_answer", "user": "kevin", "approve": true, "minutes": 15, "pin": "1234"}
{"action": "set_profile", "user": "kevin", "profile": "holiday", "pin": "1234"}
{"action": "screenshot", "user": "kevin"}
{"action": "launch", "name": "Minecraft", "user": "kevin"}
{"action": "launch", "command": "notepad.exe", "user": "kevin"}   (a command line of its own: admin level)
{"action": "set", "entity": "user_kevin_limit_today", "value": 120}
```

`set` runs any entity command by its catalog key with the value the entity
would receive (`ON` / `OFF`, `PRESS`, a number, text); it honours the parent
PIN like every other command (`"pin"` may be passed inline).

Every command answers `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.
Not being able to do what was asked is an error, not a silent success: a wrong
PIN (`wrong pin`), a missing PIN (`parent pin required`), a power action
switched off in the configuration, a user without a session for `logoff` /
`lock` / `notify`, an unknown user, rule, reward or scope, an empty `user`
(which never means "everybody" - leave the field out for that) and values that
are not numbers (`invalid value: ...`). Some commands add fields to the answer:
`usage_report` returns `report`, `block_item` / `unblock_item` return the new
`items` list, `focus` returns `until`, `screenshot` returns the request `id`,
`rule_set` returns the rule's stored `definition` and whether it was `created`.

`time_request_answer` answers a child's request for more time (the user
payload's `time_request` holds the pending one: `minutes`, `at`, `expires`).
Approving is a grant, so it honours the parent PIN, and adds the requested
minutes as bonus unless `minutes` says otherwise; denying needs no PIN.
`set_profile` switches a user to one of their profiles (`profiles` in the user
payload; `normal` is no profile); without `user` it applies to every managed
user that has the profile. Switching to a profile that allows more needs the
PIN.

`peer_usage` is how the Home Assistant integration gives a PC with
`time_limits.shared_budget` the usage of the other PCs (the `pc` payload's
`shared_budget` tells it which PCs want that). Per PC id it carries the users'
`date`, `used_minutes` and, for weekly budgets, `history`, plus what that PC
granted: `bonus_local_minutes` and `bedtime_extension_local_minutes`. Usage
figures are merged into what the PC already knows and never go down within a
day: a PC or user that is not mentioned keeps its last figures.

### Usage report

`GET /api/users/<user>/report?hours=10`, or the command
`{"action": "usage_report", "user": "kevin", "hours": 10}`:

```json
{
  "user": "kevin", "hours": 10.0, "from": "2026-10-13T08:00", "to": "2026-10-13T18:25",
  "total_minutes": 212.5, "website_minutes": 96.0,
  "active_minutes": 190.0, "passive_minutes": 35.5, "idle_minutes": 22.0, "away_minutes": 148.0, "locked_minutes": 0.0,
  "apps": {"chrome": 120.5, "code": 61.0},
  "games": {"robloxplayerbeta": 31.0},
  "sites": {"youtube.com": 64.0, "wikipedia.org": 32.0}
}
```

Minutes are time in the foreground while the user was active or passive, most
used first; entries under half a minute are left out. Usage is stored per
clock hour (for 14 days), so the window starts at the full hour `hours` ago
(`from`), and `hours` can be 1 to 336. The same report for
`activity.report_hours` is part of every user payload as `usage_recent`
(refreshed once a minute), next to `idle_minutes_today`, `away_minutes_today`
and `locked_minutes_today`.

### Blocked apps and websites

`{"action": "block_item", "user": "kevin", "item": "youtube.com"}` and
`{"action": "unblock_item", "user": "kevin", "item": "youtube.com"}` maintain
a per-user list that is enforced like `blocked: true` rules (see
[HOMEASSISTANT.md](HOMEASSISTANT.md#blocking-applications-and-websites-from-home-assistant)).
`item` is a program (`roblox`, `roblox.exe`, a full path) or a website
(`youtube.com`, a full address); `unblock_item` also accepts `all`. The user
payload carries `blocked_count` and `blocked_details` (`items`, `apps`,
`sites`).

### PC Link update

The `pc` payload carries `pclink_installed_version`, `pclink_latest_version`,
`pclink_update_available`, `pclink_update_in_progress`,
`pclink_update_release_url`, `pclink_update_summary` and `pclink_update_info`
(`status`: idle / checking / downloading / installing / error, `error`,
`last_check`, `can_install`, `auto_install`, `repository`, `signature`: what
the last signature check of a download found). The commands
`pclink_update_check` and `pclink_update_install` (also available as entity
keys for `set`) trigger a check and the installation.

### WebSocket

Connect to `/api/ws` (with the token for a writable connection; the read token
gives a connection that receives state and events but cannot send commands).
The server sends:

```json
{"type": "hello", "writable": true, "admin": false, "status": {...}}
{"type": "state", "pc": {...}, "users": {...}}      // every api.push_interval_seconds
{"type": "event", "ts": 1.7e9, "user": "kevin", "event": "time_up", ...}
```

and accepts:

```json
{"type": "ping", "id": 1}                            -> {"type": "pong", "id": 1}
{"type": "status", "id": 2}                          -> {"type": "status", "pc": ..., "users": ...}
{"type": "entities", "id": 4}                        -> {"type": "entities", "device": ..., "entities": [...]}
{"type": "command", "id": 3, "action": "block", "user": "kevin", "token": "..."}
                                                     -> {"type": "result", "id": 3, "ok": true}
```

`type` defaults to `command`; a `token` field authenticates a single command on
an otherwise read-only connection (or, with the admin token, one admin command
on an everyday connection). `hello.writable` tells a client whether its
token was accepted for commands, `hello.admin` whether for the admin ones. A
command that fails inside the service is answered with
`{"type": "result", "ok": false, "error": ...}`; the connection stays. A
client that breaks the protocol is closed with code 1002 (1009 for a message
that is too large, 1013 when all places are taken). The `pc` part of a `state`
push is rebuilt at most every `telemetry.interval_seconds`.

Events a client may see besides the device triggers: `pclink_update_available`
(a newer release was found) and, per user, `blocklist_changed`.

## Hot reload

Independently of the API, the service watches `config.yaml` (every 10 s) and
applies changes on the fly; the integration refreshes the entity catalog so
new users, rules or apps appear immediately. The `Reload configuration` button
and the `reload` JSON action do the same.

These need a restart; they are listed in the `Service` sensor's
`restart_needed` attribute and in the reload result: the `storage`,
`logging` and `hardware_sensors` sections; `agent.enabled` and the agent's IPC
host / port; `api.enabled`, `api.host`, `api.port`, `api.token` and
`api.advertise` and `api.read_token`; `time_limits.block_method`; `device.id` and `device.name`;
`windows_update.enabled`, `updates.enabled`, `fleet.enabled`;
`telemetry.usb_devices`, `telemetry.printers` and `telemetry.gpu`.

A file that cannot be used (broken YAML, a wrong value type, a number out of
range) is rejected as a whole and the service keeps running on the previous
configuration; the reason is in the service log, in the event log
(`config_reload_failed`) and in the answer of `PUT /api/config` / `reload`.
`check-config` reports the same errors before you save. Values are checked
against their type (`tick_seconds: fast` is an error, `port: "1883"` is read as
a number) and the obvious ranges (ports, positive intervals, screenshot
quality).
